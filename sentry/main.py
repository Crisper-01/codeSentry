from __future__ import annotations

import logging
import shlex
from html import escape

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory

import llm
import review_graph
from container_manager import CommandResult, ContainerManagerError, run_build, run_tests
from github_handler import (
    GitHubHandlerError,
    RepoPermissions,
    _parse_repo,
    can_manage_repo,
)
from repo_config import RepoConfigError
from repository_store import (
    AddRepoResult,
    add_repo,
    delete_repo,
    disable_repo,
    enable_repo,
    get_all_repos,
    get_repo,
    load_state,
    set_repo_config,
    state_file,
)
from webhook import start_webhook_server

HELP_TEXT = """\
Commandes disponibles :

  add <owner/repo>                    Ajouter un dépôt (Rust ou C++ uniquement)
  delete <owner/repo>                 Supprimer un dépôt de la liste
  enable <owner/repo>                 Activer le monitoring (exige conf + droit clone)
  disable <owner/repo>                Désactiver le monitoring (retire le clone)
  list                                Lister les dépôts stockés
  set config <owner/repo> <chemin>    Associer un fichier de conf JSON au dépôt
  check build <owner/repo>            Lancer run_build dans le sandbox
  check test <owner/repo>             Lancer run_tests dans le sandbox
  review <owner/repo> <pr>            Revue agentique complète d'une pull request
  status                              Détail du dernier run de revue
  runs                                Lister les derniers runs de revue
  cancel <run_id>                     Annuler un run en cours
  help                                Afficher cette aide
  quit / exit                         Quitter

Tab : auto-complétion (commandes, dépôts stockés, chemins pour set config)
Format du dépôt : owner/repo (ex. owner/project)
Webhook GitHub : écoute sur SENTRY_WEBHOOK_HOST:SENTRY_WEBHOOK_PORT (défaut 0.0.0.0:8080)
LLM : SENTRY_LLM_PROVIDER (anthropic|openai) + SENTRY_LLM_MODEL
"""

_TOP_LEVEL_COMMANDS = (
    "add",
    "cancel",
    "check",
    "delete",
    "disable",
    "enable",
    "help",
    "list",
    "review",
    "runs",
    "set",
    "status",
    "quit",
    "exit",
)

_REPO_COMMANDS = frozenset({"delete", "enable", "disable", "add", "review"})
_CHECK_KINDS = ("build", "test")


class CodeSentryCompleter(Completer):
    """Tab completion for commands, stored repos, and config file paths."""

    def __init__(self) -> None:
        self._path_completer = PathCompleter(expanduser=True)

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        stripped = text.lstrip()
        if not stripped:
            yield from self._complete_words("", _TOP_LEVEL_COMMANDS)
            return

        try:
            parts = shlex.split(stripped)
        except ValueError:
            parts = stripped.split()

        ends_with_space = stripped.endswith(" ")
        cmd = parts[0].lower()

        # First token: command name
        if len(parts) == 1 and not ends_with_space:
            yield from self._complete_words(parts[0], _TOP_LEVEL_COMMANDS)
            return

        if cmd == "check":
            yield from self._complete_check(parts, ends_with_space)
            return

        if cmd == "set":
            yield from self._complete_set(parts, ends_with_space, complete_event)
            return

        if cmd == "cancel":
            if (len(parts) == 1 and ends_with_space) or (len(parts) == 2 and not ends_with_space):
                prefix = "" if len(parts) == 1 else parts[1]
                running = [r.run_id for r in review_graph.recent_runs(20) if r.status == "running"]
                yield from self._complete_words(prefix, running)
            return

        if cmd in _REPO_COMMANDS:
            if (len(parts) == 1 and ends_with_space) or (len(parts) == 2 and not ends_with_space):
                prefix = "" if len(parts) == 1 else parts[1]
                # add: no stored-repo list required, but completing known ones still helps
                yield from self._complete_words(prefix, self._repo_names())
            return

    def _complete_check(self, parts: list[str], ends_with_space: bool):
        if (len(parts) == 1 and ends_with_space) or (len(parts) == 2 and not ends_with_space):
            prefix = "" if len(parts) == 1 else parts[1]
            yield from self._complete_words(prefix, _CHECK_KINDS)
            return
        if (len(parts) == 2 and ends_with_space) or (len(parts) == 3 and not ends_with_space):
            if parts[1].lower() not in _CHECK_KINDS:
                return
            prefix = "" if len(parts) == 2 else parts[2]
            yield from self._complete_words(prefix, self._repo_names())

    def _complete_set(self, parts: list[str], ends_with_space: bool, complete_event):
        if (len(parts) == 1 and ends_with_space) or (len(parts) == 2 and not ends_with_space):
            prefix = "" if len(parts) == 1 else parts[1]
            yield from self._complete_words(prefix, ("config",))
            return
        if parts[1].lower() != "config":
            return
        if (len(parts) == 2 and ends_with_space) or (len(parts) == 3 and not ends_with_space):
            prefix = "" if len(parts) == 2 else parts[2]
            yield from self._complete_words(prefix, self._repo_names())
            return
        if (len(parts) == 3 and ends_with_space) or (len(parts) >= 4 and not ends_with_space):
            path_so_far = "" if len(parts) == 3 and ends_with_space else parts[3]
            yield from self._path_completer.get_completions(
                Document(path_so_far, len(path_so_far)),
                complete_event,
            )

    @staticmethod
    def _repo_names() -> list[str]:
        return sorted(r.name for r in get_all_repos())

    @staticmethod
    def _complete_words(prefix: str, words: tuple[str, ...] | list[str]):
        needle = prefix.lower()
        for word in words:
            if word.lower().startswith(needle):
                yield Completion(word, start_position=-len(prefix))


def _print_help() -> None:
    print(HELP_TEXT, end="")


def _print_error(message: str) -> None:
    """Print a failure line in red."""
    print_formatted_text(HTML(f"<ansired>{escape(message)}</ansired>"))


def _print_step(message: str) -> None:
    """Live progress line emitted by a review node."""
    print_formatted_text(HTML(f"  <ansicyan>·</ansicyan> {escape(message)}"))


def _format_permissions(perms: RepoPermissions) -> str:
    flags = []
    flags.append("clone" if perms.can_clone else "no-clone")
    flags.append("create-pr" if perms.can_create_pr else "no-create-pr")
    flags.append("modify-pr" if perms.can_modify_pr else "no-modify-pr")
    return ", ".join(flags)


def _handle_list() -> None:
    repos = get_all_repos()
    if not repos:
        print("Aucun dépôt stocké.")
        return

    name_w = max(len(r.name) for r in repos)
    name_w = max(name_w, len("nom"))
    lang_w = max(len(r.main_language.value) for r in repos)
    lang_w = max(lang_w, len("langage"))

    header = f"{'nom':<{name_w}}  {'langage':<{lang_w}}  {'enabled':<7}  permissions"
    print(header)
    print("-" * len(header))
    for repo in sorted(repos, key=lambda r: r.name):
        enabled = "oui" if repo.enabled else "non"
        try:
            perms_text = _format_permissions(can_manage_repo(repo.name))
        except GitHubHandlerError as exc:
            _print_error(
                f"{repo.name:<{name_w}}  "
                f"{repo.main_language.value:<{lang_w}}  "
                f"{enabled:<7}  (permissions indisponibles : {exc})"
            )
            continue
        print(
            f"{repo.name:<{name_w}}  "
            f"{repo.main_language.value:<{lang_w}}  "
            f"{enabled:<7}  "
            f"{perms_text}"
        )


def _print_command_result(label: str, result: CommandResult) -> None:
    summary = f"{label} - code de retour : {result.exit_code}"
    if result.ok:
        print(summary)
    else:
        _print_error(summary)
    if result.stdout.strip():
        print("--- stdout ---")
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print("--- stderr ---")
        print(result.stderr.rstrip())


def _handle_repo_lifecycle(command: str, repo: str) -> None:
    try:
        _parse_repo(repo)
    except ValueError as exc:
        _print_error(f"Erreur : {exc}")
        return

    try:
        if command == "add":
            result = add_repo(repo)
            if result is AddRepoResult.ADDED:
                print(f"Dépôt {repo!r} ajouté.")
            elif result is AddRepoResult.ALREADY_EXISTS:
                _print_error(f"Erreur : le dépôt {repo!r} existe déjà.")
            elif result is AddRepoResult.UNSUPPORTED_LANGUAGE:
                _print_error(f"Erreur : {repo!r} n'est pas un projet Rust ou C++.")
        elif command == "delete":
            if delete_repo(repo):
                print(f"Dépôt {repo!r} supprimé.")
            else:
                _print_error(f"Erreur : dépôt {repo!r} introuvable.")
        elif command == "enable":
            if enable_repo(repo):
                print(f"Monitoring activé pour {repo!r}.")
            else:
                _print_error(f"Erreur : dépôt {repo!r} introuvable.")
        elif command == "disable":
            if disable_repo(repo):
                print(f"Monitoring désactivé pour {repo!r}.")
            else:
                _print_error(f"Erreur : dépôt {repo!r} introuvable.")
    except GitHubHandlerError as exc:
        _print_error(f"Erreur GitHub : {exc}")
    except RepoConfigError as exc:
        _print_error(f"Erreur config : {exc}")
    except ContainerManagerError as exc:
        _print_error(f"Erreur conteneur : {exc}")


def _handle_set_config(repo: str, path: str) -> None:
    try:
        _parse_repo(repo)
    except ValueError as exc:
        _print_error(f"Erreur : {exc}")
        return

    try:
        if set_repo_config(repo, path):
            bound = get_repo(repo)
            print(f"Config liée pour {repo!r} → {bound.config_path if bound else path}")
        else:
            _print_error(f"Erreur : dépôt {repo!r} introuvable.")
    except RepoConfigError as exc:
        _print_error(f"Erreur config : {exc}")


def _handle_check(kind: str, repo: str) -> None:
    try:
        _parse_repo(repo)
    except ValueError as exc:
        _print_error(f"Erreur : {exc}")
        return

    stored = get_repo(repo)
    if stored is None:
        _print_error(f"Erreur : dépôt {repo!r} introuvable.")
        return

    try:
        if kind == "build":
            result = run_build(stored)
            _print_command_result("Build", result)
        else:
            result = run_tests(stored)
            _print_command_result("Tests", result)
    except ContainerManagerError as exc:
        _print_error(f"Erreur conteneur : {exc}")
    except GitHubHandlerError as exc:
        _print_error(f"Erreur GitHub : {exc}")


def _print_run(record: review_graph.RunRecord, *, verbose: bool) -> None:
    line = (
        f"{record.run_id}  {record.repo}#{record.pr_number}  "
        f"{record.status:<12} {record.trigger}"
    )
    if record.status in {"ok"}:
        print(line)
    elif record.status == "running":
        print(line)
    else:
        _print_error(line)

    if not verbose:
        return

    if record.head_sha:
        print(f"  commit          : {record.head_sha[:12]}")
    print(f"  build / tests   : {record.build_exit} / {record.test_exit}")
    print(f"  verdict intent  : {record.intent_verdict or '-'}")
    if record.generated_tests:
        print(f"  tests générés   : {', '.join(record.generated_tests)}")
    if record.comment_url:
        print(f"  commentaire     : {record.comment_url}")
    if record.followup_pr_url:
        print(f"  PR de tests     : {record.followup_pr_url}")
    for error in record.errors:
        _print_error(f"  note            : {error}")


def _handle_review(repo: str, pr_text: str) -> None:
    try:
        _parse_repo(repo)
    except ValueError as exc:
        _print_error(f"Erreur : {exc}")
        return

    try:
        pr_number = int(pr_text)
    except ValueError:
        _print_error(f"Erreur : numéro de PR invalide : {pr_text!r}")
        return

    stored = get_repo(repo)
    if stored is None:
        _print_error(f"Erreur : dépôt {repo!r} introuvable.")
        return
    if not stored.enabled:
        _print_error(f"Erreur : le dépôt {repo!r} est désactivé (enable d'abord).")
        return
    if not llm.is_configured():
        print(
            "Attention : aucune clé LLM configurée - build et tests seront exécutés, "
            "mais l'analyse d'intention, les release notes et la génération de tests "
            "seront ignorées."
        )

    print(f"Revue de {repo}#{pr_number} ({llm.describe()})")
    record = review_graph.review_pull_request(stored, pr_number, notify=_print_step)
    print()
    _print_run(record, verbose=True)


def _handle_status() -> None:
    record = review_graph.last_run()
    if record is None:
        print("Aucun run de revue pour l'instant.")
        return
    _print_run(record, verbose=True)


def _handle_runs() -> None:
    records = review_graph.recent_runs()
    if not records:
        print("Aucun run de revue pour l'instant.")
        return
    for record in records:
        _print_run(record, verbose=False)


def _handle_cancel(run_id: str) -> None:
    if review_graph.cancel_run(run_id):
        print(f"Annulation demandée pour le run {run_id} (effective au prochain nœud).")
    else:
        _print_error(f"Erreur : aucun run en cours avec l'identifiant {run_id!r}.")


def _dispatch(line: str) -> bool:
    """Parse and run one command. Returns False to exit the loop."""
    parts = shlex.split(line.strip())
    if not parts:
        return True

    command = parts[0].lower()
    if command in {"quit", "exit"}:
        return False
    if command == "help":
        _print_help()
        return True
    if command == "list":
        if len(parts) != 1:
            _print_error("Usage : list")
            return True
        _handle_list()
        return True
    if command == "status":
        if len(parts) != 1:
            _print_error("Usage : status")
            return True
        _handle_status()
        return True
    if command == "runs":
        if len(parts) != 1:
            _print_error("Usage : runs")
            return True
        _handle_runs()
        return True

    if command == "cancel":
        if len(parts) != 2:
            _print_error("Usage : cancel <run_id>")
            return True
        _handle_cancel(parts[1])
        return True

    if command == "set":
        if len(parts) != 4 or parts[1].lower() != "config":
            _print_error("Usage : set config <owner/repo> <chemin>")
            return True
        _handle_set_config(parts[2], parts[3])
        return True

    if command == "check":
        if len(parts) != 3 or parts[1].lower() not in {"build", "test"}:
            _print_error("Usage : check build|test <owner/repo>")
            return True
        _handle_check(parts[1].lower(), parts[2])
        return True

    if command == "review":
        if len(parts) != 3:
            _print_error("Usage : review <owner/repo> <pr_number>")
            return True
        _handle_review(parts[1], parts[2])
        return True

    if command not in {"add", "delete", "enable", "disable"}:
        _print_error(f"Commande inconnue : {command!r}. Tapez 'help' pour l'aide.")
        return True
    if len(parts) != 2:
        _print_error(f"Usage : {command} <owner/repo>")
        return True

    _handle_repo_lifecycle(command, parts[1])
    return True


def run_cli() -> None:
    session: PromptSession[str] = PromptSession(
        history=InMemoryHistory(),
        completer=CodeSentryCompleter(),
        complete_while_typing=False,
    )
    prompt = HTML("<ansigreen><b>&gt;</b></ansigreen> ")
    while True:
        try:
            line = session.prompt(prompt)
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break
        if not _dispatch(line):
            break


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    load_state()
    start_webhook_server()

    watched = sum(1 for repo in get_all_repos() if repo.enabled)
    print("CodeSentry - tapez 'help' pour la liste des commandes (Tab pour compléter).")
    print(f"Registre : {state_file()} ({watched} dépôt(s) actif(s))")
    if llm.is_configured():
        print(f"LLM : {llm.describe()}")
    else:
        print("LLM : non configuré - les nœuds d'analyse seront ignorés.")
    run_cli()


if __name__ == "__main__":
    main()
