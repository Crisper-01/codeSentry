# CodeSentry - Agentic CI/CD & QA Reviewer

Agent autonome d'analyse, de validation et de sécurisation de Pull Requests pour projets C++ et Rust.

L'interface principale est une **application shell** interactive. Un webhook GitHub complète le shell pour déclencher automatiquement la revue complète sur les PR ciblant les branches configurées.

---

## 1. Vision & Objectifs du projet

### Contexte & Problématique

La revue manuelle de Pull Requests (PR) sur des codebases bas niveau (C++/Rust) est chronophage et source d'erreurs :

* Vérification manuelle de la conformité du code vis-à-vis de l'issue GitHub associée.
* Risques de compilation cassée selon les toolchains locales.
* Manque de couverture de tests sur les nouveaux chemins d'exécution.
* Rédaction fastidieuse des release notes et rapports de changements.

### Solution

**CodeSentry** automatise le cycle de pré-review pour les PR (déclenchement manuel depuis le shell, ou automatique via webhook) :

1. Compilation et exécution des tests en environnement isolé (Docker sandbox), selon un **fichier de conf JSON** par dépôt (commandes de build/test et branches surveillées).
2. Analyse sémantique de l'adéquation entre l'issue et la PR.
3. Génération automatique de release notes structurées.
4. Génération de tests unitaires/intégration pour les cas limites non couverts, validés localement, et soumission autonome d'une PR complémentaire si la couverture est jugée insuffisante.

---

## 2. Architecture Technique

```
┌───────────────────────────────────────────────────────────┐
│                     Shell (prompt_toolkit)                │
│  add / enable / disable / set config / check / review      │
│  status / runs / cancel                                    │
└─────────────────────────────┬─────────────────────────────┘
                              │
                              ▼
┌───────────────────────────────────────────────────────────┐
│              Repository Store (persisté JSON)             │
│         (repos watchés + conf JSON par dépôt)             │
└───────┬─────────────────────────────┬─────────────────────┘
        │                             │
        ▼                             ▼
┌───────────────────┐     ┌───────────────────────────────┐
│  GitHub Handler   │     │     Container Manager         │
│  (REST + webhook) │     │  worktree / build / test      │
└─────────┬─────────┘     └───────────────────────────────┘
          │
          │ Webhook PR (opened / synchronize / reopened)
          │ filtré par branches de la conf
          ▼
┌───────────────────────────────────────────────────────────┐
│                 LangGraph Orchestrator                    │
│  fetch_context → checkout_pr → build_and_test →           │
│  validate_intent → release_note → test_generator →        │
│  post_feedback                                            │
└──────────────┬─────────────────────────────┬──────────────┘
               │ Appel direct                │ Appel direct
               ▼                             ▼
┌─────────────────────────────┐ ┌───────────────────────────┐
│      GitHub REST API        │ │   Container Manager       │
│      (github_handler)       │ │   (Module Python local)   │
├─────────────────────────────┤ ├───────────────────────────┤
│ • Read PR metadata & diff   │ │ • Load conf JSON (build)  │
│ • Fetch associated Issue    │ │ • Checkout PR (worktree)  │
│ • Post comment to PR        │ │ • Run build in container  │
│ • Create branch & Test PR   │ │ • Execute test suites     │
└─────────────────────────────┘ └───────────────────────────┘
```

### Décision d'architecture : REST plutôt que MCP

Le projet visait initialement le serveur **GitHub MCP** officiel pour les quatre
opérations GitHub de l'orchestrateur. L'intégration retenue est le **REST direct**
via `github_handler.py`, pour trois raisons :

* Les quatre opérations (`get_pull_request`, `get_issue`, `post_pr_comment`,
  `create_pull_request`) sont des appels déterministes du graphe, pas des outils
  exposés à un LLM. MCP ajouterait un sous-processus et une couche JSON-RPC
  au-dessus des mêmes appels REST, sans bénéfice.
* `github_handler.py` existait déjà et portait la lecture des langages et des
  permissions ; la surface d'intégration reste dans un seul module.
* Une seule implémentation à tester, contre deux avec un fallback.

Si un besoin concret apparaît (réutiliser le serveur MCP dans un autre agent,
exposer ces outils à un LLM), `github_handler` est le point d'insertion : les
signatures y sont déjà celles des tools MCP.

### Déclenchement

| Source | Action |
|--------|--------|
| Shell `check build <repo>` | `run_build` sur le clone persistant |
| Shell `check test <repo>` | `run_tests` sur le clone persistant |
| Shell `review <repo> <pr>` | Graphe de revue complet, progression affichée nœud par nœud |
| Webhook GitHub PR (`opened` / `synchronize` / `reopened`) | Si le dépôt est *enabled*, la conf liée, la PR non-draft et la branche cible dans `watched_branches` → graphe de revue complet en tâche de fond |

Le webhook répond immédiatement (`202` logique) et exécute la revue sur un
thread : une revue dure des minutes, bien au-delà du délai que GitHub accorde.
Un même `(dépôt, head_sha)` n'est jamais revu deux fois.

### Configuration JSON (par dépôt)

Chaque dépôt watché est lié à un fichier de conf fourni par l'utilisateur (`set config <owner/repo> <chemin>`) :

```json
{
  "watched_branches": ["main", "develop"],
  "build": { "mode": "cargo" },
  "test": { "mode": "cargo test" },
  "auto_test_pr": false
}
```

* `watched_branches` : branches cibles surveillées pour les PR - ce sont les branches que CodeSentry protège
* `build` : mode (`cmake`, `gcc`, `cargo`, `custom`) + `command` optionnelle (obligatoire si `custom`)
* `test` : mode (`cargo test`, `cmake test`, `custom`) + `command` optionnelle (obligatoire si `custom`)
* `auto_test_pr` : si `true`, les tests générés qui passent sont poussés en PR de complétion. Défaut `false` : ils sont seulement proposés dans le commentaire.

Valeurs par défaut si `command` est omise : `cmake` → `cmake -S . -B build && cmake --build build` ; `gcc` → `make` ; `cargo` → `cargo build` ; `cargo test` → `cargo test` ; `cmake test` → `ctest --test-dir build --output-on-failure`.

### Isolation des runs

Le clone persistant (`/workspace/<repo>`) n'est jamais modifié par une revue.
Chaque run fetch `pull/<n>/head` et attache un **worktree git jetable** sous
`/workspace/.runs/`, supprimé en fin de run. Un verrou par dépôt sérialise les
runs qui partagent ce clone. Chaque commande sandbox a un budget temps
(`SENTRY_COMMAND_TIMEOUT`, défaut 1800 s) au-delà duquel elle est abandonnée et
remontée comme un échec.

### Lancement

* Local : depuis `sentry/`, `uv pip install -e ".[dev]"` puis `python main.py` (shell + webhook en arrière-plan sur `:8080`).
* Compose : `docker compose -f containers/docker-compose.yml up --build` (service `sentry` + sandboxes), puis `docker attach codesentry-sentry`.
* Webhook public : relayer vers `http://127.0.0.1:8080/webhook/github` via `smee` ou Cloudflared ; optionnel `GITHUB_WEBHOOK_SECRET`.

Le registre des dépôts est persisté (`SENTRY_STATE_FILE`, défaut `~/.codesentry/repos.json`) : les dépôts *enabled* et leurs confs survivent à un redémarrage.

---

## 3. Workflow des Nœuds LangGraph

1. **`node_fetch_context` :** métadonnées de la PR, diff unifié, fichiers touchés, et issue liée (mot-clé de fermeture `fixes #12`, sinon référence nue `#12`) via l'API REST GitHub.

2. **`node_checkout_pr` :** fetch de `pull/<n>/head` et worktree jetable aligné sur le `head_sha` réellement récupéré.

3. **`node_build_and_test` :** charge la conf JSON, déclenche `run_build` puis `run_tests` dans le worktree via le Container Manager, remonte logs et codes de retour. Un build rouge n'enchaîne pas sur les tests.

4. **`node_validate_intent` :** évalue si les modifications répondent aux exigences de l'issue. Verdict (`aligned` / `partially_aligned` / `misaligned` / `no_issue_linked`), confiance, justification, exigences non couvertes, défauts visibles dans le diff.

5. **`node_generate_release_note` :** niveau d'impact (`patch` / `minor` / `major`), résumé, points saillants, ruptures de compatibilité.

6. **`node_test_generator` :** détecte les cas limites non couverts par le diff, génère des tests (`cargo test`, ou GoogleTest / Catch2 selon ce que le dépôt utilise déjà), les écrit dans le sandbox et relance la suite. Les chemins proposés doivent être **nouveaux** et sous `tests/` ; tout le reste est écarté et signalé.

7. **`node_post_feedback` :** compose et publie un commentaire markdown récapitulatif sur la PR d'origine ; ouvre la PR de tests si `auto_test_pr` et les droits le permettent.

### Routage conditionnel

* Build ou tests en échec → court-circuit direct vers `node_post_feedback` : ni génération de tests, ni PR de complétion. Rien d'autre n'est fiable sur une branche qui ne compile pas.
* Pas d'issue liée → `node_validate_intent` bascule en revue du diff seul.
* Couverture jugée suffisante (la PR touche déjà ses tests, verdict `aligned`, aucun défaut relevé) → `node_test_generator` est sauté.

### Tests générés en échec

Un test généré qui échoue n'est **jamais** soumis. Il est cité intégralement dans
le commentaire, avec sa sortie d'échec : un test rouge signale soit un défaut réel
soit un test mal écrit, et cet arbitrage revient à un humain.

---

## 4. Stack Technologique

* **Langages :** Python 3.12+, Rust et C++ (projets cibles).
* **Interface :** shell interactif (`prompt_toolkit`) - point d'entrée principal.
* **Framework Agentique :** LangGraph (graphe d'états). Les nœuds appellent directement les SDK officiels des fournisseurs, sans couche d'abstraction LangChain.
* **Protocoles & APIs :** GitHub REST API, webhook GitHub (PR).
* **Infrastructure locale :** Docker (Container Manager), docker-compose (sandboxes + sentry), relais local optionnel (`smee.io` / Cloudflared) pour le webhook.
* **LLM Engine :** au choix via `SENTRY_LLM_PROVIDER` - Anthropic (`claude-opus-5` par défaut, SDK `anthropic`) ou OpenAI (`gpt-5` par défaut, SDK `openai`). Modèle surchargeable par `SENTRY_LLM_MODEL`. Les deux passent par la sortie structurée native du fournisseur : un nœud reçoit un modèle pydantic validé, jamais du texte à parser.

Sans clé LLM, CodeSentry clone, compile, teste et rapporte quand même ; les nœuds d'analyse sont sautés et le commentaire le dit.

---

## 5. Spécification des Outils

### Shell

* `add` / `delete` / `enable` / `disable` : gestion des dépôts watchés
* `set config` : associer un fichier de conf JSON à un dépôt
* `list` : dépôts enregistrés avec leurs permissions GitHub live
* `check build|test <owner/repo>` : déclenchement manuel sur le clone persistant
* `review <owner/repo> <pr>` : revue agentique complète
* `status` / `runs` / `cancel <run_id>` : suivi et annulation des runs

### GitHub Handler (`github_handler.py`)

* `get_pull_request(repo, pr_number)` : métadonnées, refs base/head, `head_sha`, auteur
* `get_pull_request_diff(repo, pr_number)` : diff unifié
* `get_pull_request_files(repo, pr_number)` : chemins touchés
* `get_issue(repo, issue_number)` : exigences du ticket d'origine
* `find_linked_issue_number(pr)` : issue déclarée par la PR
* `post_pr_comment(repo, pr_number, body)` : bilan de validation
* `create_pull_request(repo, head, base, title, body)` : PR de tests générés
* `can_manage_repo(repo)` : droits de clone, de commentaire et de création de PR

### Container Manager (`container_manager.py`)

Module interne (pas de serveur MCP - overkill pour Docker).

* `clone_repo` / `remove_repo` / `is_repo_cloned` : cycle de vie du clone persistant
* `pr_workspace(repo, pr_number)` : worktree jetable sur le head de la PR
* `run_build(repo, sandbox)` / `run_tests(repo, sandbox)` : commandes de la conf, capture `stdout` / `stderr` / code de retour, budget temps
* `list_tracked_files` / `write_files` : lecture et écriture dans le sandbox
* `publish_branch` : commit et push de la branche de tests générés

Le token GitHub est retiré de toute sortie avant qu'elle n'atteigne les logs ou un commentaire de PR.

### Webhook GitHub

* Écoute `pull_request` : `opened` / `synchronize` / `reopened`.
* Filtre : dépôt *enabled*, conf liée, PR non-draft, branche cible dans `watched_branches`.
* Vérifie la signature `X-Hub-Signature-256` si `GITHUB_WEBHOOK_SECRET` est défini.
* Idempotent sur `(dépôt, head_sha)`.
* Réutilise exactement le même chemin d'exécution que `review` dans le shell.

### Garde-fous

* Les permissions sont lues avant d'agir : pas de droit de push sur le dépôt head → les tests sont proposés dans le commentaire au lieu d'être soumis ; pas de droit d'écriture → le rapport est rendu mais pas publié. Les deux cas sont écrits dans les notes du run.
* `auto_test_pr` vaut `false` par défaut : aucune PR n'est ouverte sans opt-in explicite.
* Un fichier généré hors de `tests/`, déjà existant, ou pointant hors du dépôt est écarté et signalé.

---

## 6. Plan d'Implémentation & Jalons

* **Phase 1 - Shell & Ingestion :** Shell `prompt_toolkit`, store de repos, conf JSON par dépôt, webhook GitHub filtré par branches configurées. **Fait.**
* **Phase 2 - Container Manager :** `run_build` / `run_tests` depuis la conf JSON, commandes shell `check build` / `check test`, capture des logs. **Fait.**
* **Phase 3 - Orchestrateur LangGraph :** graphe d'états, checkout PR isolé, prompts versionnés, génération de tests. **Fait.**
* **Phase 4 - Intégration GitHub & tests e2e :** lecture des issues, publication du commentaire, création autonome de la PR de tests, garde-fous de permissions, idempotence, timeouts. **Fait.**
* **Phase 5 - Dépôt de Démonstration & GIF Terminal :** `demo/ring-buffer` livré (crate Rust avec bug logique sur le chemin de wrap-around, non couvert par ses tests). Reste la captation vidéo.

Le détail opérationnel des phases 1–4 est dans [`plan.md`](../plan.md).

---

## 7. Tests

```bash
cd sentry && python -m pytest
```

117 tests, sans Docker, sans GitHub et sans clé LLM - toutes les dépendances externes sont simulées. Ils couvrent les quatre scénarios du plan :

* **A** - build OK, tests OK, issue alignée → commentaire positif avec release notes
* **B** - build ou tests KO → feedback avec logs, aucune génération de tests, aucune PR
* **C** - tests manquants + `auto_test_pr` → PR de complétion ouverte sur la branche head
* **D** - webhook sur branche non listée → no-op

plus la persistance du registre, les garde-fous de chemins générés, l'idempotence webhook, la vérification de signature, les permissions manquantes, l'annulation et les timeouts.
