## Phases 1 & 2 - FAIT

Livré : shell `prompt_toolkit`, conf JSON, `run_build`/`run_tests`, webhook `:8080`, service compose `sentry`.
Écart reporté en phase 3 : le webhook n'checkout pas encore le `head_sha` de la PR (build/test sur le clone enabled tel quel). **Résorbé en phase 3.**

1) Mettre en place un shell (`prompt_toolkit`) avec des commandes pour :
    - Ajouter un repo dans la liste des repos watchés
    - Enable / disable un repo
    - Set la config d'un repo
    - `check build <owner/repo>` - déclenchement manuel de `run_build`
    - `check test <owner/repo>` - déclenchement manuel de `run_tests`

2) Stabiliser le câblage github_handler ↔ repository_store
3) Stabiliser le câblage container_manager ↔ repository_store
4) Connecter entièrement le shell au repository store

5) Fichier de conf JSON par repo (chemin fourni par l'utilisateur). Schéma attendu :
    - Branches à surveiller (pour les PR / webhook)
    - Mode de build (`cmake`, `gcc`, `cargo`, `custom` + commande)
    - Mode de test (`cargo test`, `cmake test`, `custom` + commande)

6) Container Manager - exécution sandbox :
    - `run_build(repo)` : lit la conf, lance la commande de build dans le conteneur, capture stdout/stderr + exit code
    - `run_tests(repo)` : idem pour la suite de tests

7) Bind chaque repo à son fichier de conf (spécifié par l'utilisateur)

8) Ajouter le sentry dans le docker-compose

9) Webhook GitHub sur les PR (`opened` / `synchronize`) :
    - Filtrer sur les repos *enabled*
    - Filtrer sur les branches cibles listées dans la conf JSON
    - Déclencher `run_build` puis `run_tests` (même chemin que les commandes shell)

---

## Phases 3 & 4 - FAIT

Livré : orchestrateur LangGraph (`review_graph.py`), prompts versionnés (`prompts/`),
couche LLM bi-fournisseur (`llm.py`), intégration GitHub complète (REST), registre
persisté, webhook asynchrone et idempotent, 117 tests.

### Écarts assumés par rapport au plan initial

1. **GitHub MCP → REST direct.** Les quatre opérations GitHub sont des appels
   déterministes du graphe, pas des outils exposés à un LLM : MCP aurait ajouté un
   sous-processus et une couche JSON-RPC au-dessus des mêmes appels REST. Le
   fallback prévu au point 7 disparaît donc avec lui - une seule implémentation,
   une seule surface de test. Justification complète dans `sentry/project.md` §2.
2. **Pas de couche LangChain.** LangGraph porte le graphe ; les nœuds appellent
   directement les SDK officiels `anthropic` et `openai` et leur sortie structurée
   native. Un nœud reçoit un modèle pydantic validé, jamais du texte à parser.
3. **Template de commentaire final non-LLM.** `prompts/feedback_comment.md` est
   rendu de façon déterministe depuis l'état du graphe : les données sont déjà
   structurées, un appel modèle pour les reformater serait du gaspillage et une
   source de variance. Le template reste versionné comme les autres.
4. **Tests générés proposés même sans `auto_test_pr`.** Le drapeau ne conditionne
   que l'ouverture de la PR ; les tests validés sont de toute façon cités dans le
   commentaire. Un test généré qui échoue n'est jamais soumis mais est cité
   intégralement avec sa sortie d'échec - l'arbitrage « défaut réel ou mauvais
   test » revient à un humain.
5. **Persistance du registre.** Hors plan initial : le store était un dict en
   mémoire, donc un redémarrage du daemon webhook perdait tous les dépôts
   surveillés. Écrit dans `SENTRY_STATE_FILE` à chaque mutation.

### Phase 3 - Orchestrateur LangGraph

0) Checkout PR dans le sandbox - **fait**
    - `git fetch origin pull/<n>/head` puis worktree jetable sous `/workspace/.runs/`
    - Le clone persistant n'est jamais muté ; verrou par dépôt ; worktree supprimé en fin de run

1) Dépendances & config LLM - **fait**
    - `langgraph`, SDK `anthropic` et `openai`
    - `SENTRY_LLM_PROVIDER`, `SENTRY_LLM_MODEL`, `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
    - Aucun secret en dur ; sans clé, build et tests tournent quand même

2) État du graphe (`ReviewState`) - **fait**

3) Nœuds du graphe - **fait**
    - `node_fetch_context`, `node_checkout_pr`, `node_build_and_test`,
      `node_validate_intent`, `node_generate_release_note`, `node_test_generator`,
      `node_post_feedback`

4) Routage conditionnel - **fait**
    - Build ou tests KO → court-circuit vers `node_post_feedback`
    - Pas d'issue liée → verdict `no_issue_linked`, revue du diff seul
    - Couverture suffisante (PR touchant déjà ses tests, verdict `aligned`, aucun défaut) → `node_test_generator` sauté

5) Intégration déclencheurs - **fait**
    - Shell : `review <owner/repo> <pr_number>`, progression affichée nœud par nœud
    - Webhook : invoque le graphe, mêmes filtres *enabled* + `watched_branches`

6) Prompts - **fait**
    - `prompts/intent.md`, `release_notes.md`, `test_generation.md`, `feedback_comment.md`
    - Contraintes : C++/Rust uniquement ; interdiction d'inventer des fichiers hors du diff ; chemins du dépôt cités

### Phase 4 - GitHub & e2e

7) Client GitHub - **fait en REST** (voir écart 1)
    - `get_pull_request`, `get_pull_request_diff`, `get_pull_request_files`,
      `get_issue`, `find_linked_issue_number`, `post_pr_comment`, `create_pull_request`

8) Permissions & garde-fous - **fait**
    - `can_create_pr` / `can_modify_pr` vérifiés avant commentaire ou PR
    - Pas de droits → log + mention explicite dans le feedback, jamais d'échec dur
    - `auto_test_pr: true|false` en conf JSON, défaut `false`
    - Chemin généré hors `tests/`, déjà existant, ou hors dépôt → écarté et signalé
    - Token GitHub retiré de toute sortie avant logs ou commentaire

9) Publication - **fait**
    - `node_post_feedback` → commentaire sur la PR d'origine
    - Tests générés validés + `auto_test_pr` → branche `codesentry/tests-<pr>` poussée sur le dépôt head, PR ouverte sur la branche head

10) Commandes shell complémentaires - **fait**
    - `review <owner/repo> <pr>`, `status`, `runs`, `cancel <run_id>`

11) Tests e2e - **fait** (117 tests, `cd sentry && python -m pytest`)
    - Scénario A : build OK / tests OK / issue alignée → commentaire positif + release notes
    - Scénario B : build ou tests KO → feedback avec logs, pas de PR de tests
    - Scénario C : tests manquants + `auto_test_pr` → follow-up PR créée
    - Scénario D : webhook sur branche non listée → no-op
    - Repo démo : `demo/ring-buffer` (crate Rust, bug de wrap-around non couvert par ses tests)

12) Durcissement - **fait**
    - Timeout par commande sandbox (`SENTRY_COMMAND_TIMEOUT`, défaut 1800 s)
    - Annulation d'un run depuis le shell (`cancel <run_id>`, effective entre deux nœuds)
    - Logs structurés par nœud, préfixés du `run_id`
    - Idempotence webhook sur `(dépôt, head_sha)`
    - Webhook non bloquant : réponse immédiate, revue sur un thread

---

## Phase 5 - Démonstration

- [x] Repo démo C++ ou Rust avec un bug logique - `demo/ring-buffer`
- [ ] Pousser le repo démo sur GitHub et jouer le scénario complet
- [ ] GIF terminal de la session (`add` → `set config` → `enable` → `review`)

---

## Validation en conditions réelles

Exécutée contre un vrai démon Docker et l'API GitHub live (anonyme), cible
`BurntSushi/byteorder` PR #225. Scripts dans le scratchpad de session.

### Vérifié (16 contrôles, 16 verts)

Lectures GitHub live :
- `get_repo_language_bytes`, `can_manage_repo` (public anonyme : `can_clone` vrai, `can_create_pr` faux)
- `get_pull_request`, `get_pull_request_diff`, `get_pull_request_files`

Sandbox Rust :
- `add_repo` / `set_repo_config` / `enable_repo` → vrai `git clone` dans le conteneur
- `run_build` / `run_tests` → vrais `cargo build` / `cargo test`, exit 0
- `pr_workspace` → vrai `git fetch origin pull/225/head` + `worktree add`, head `a220f7d1f837`
- `write_files` → vrai `docker exec -i`, relecture conforme
- `list_tracked_files` → vrai `git ls-files`
- Worktree supprimé en sortie de contexte, `/workspace/.runs` vide
- **Clone persistant intact après la revue** (`git status --porcelain` vide)

Graphe complet, sans clé LLM ni droits d'écriture : statut `ok`, build et tests à 0,
et les quatre dégradations remontées en notes du run sans faire échouer la revue.

`publish_branch`, contre un dépôt bare local tenant lieu d'origin :
- `checkout -B`, `add`, `commit` sous l'identité CodeSentry, `push` de la branche
- La branche poussée contient bien le test généré, `main` reste intacte
- Publier sans rien de staged lève bien `ContainerManagerError`

Sandbox C++ :
- Commandes par défaut `cmake` et `cmake test` sur un projet CMake réel, `100% tests passed`

Dépôt de démo, avec un vrai `cargo` :
- Suite livrée verte (3 tests) ; le test de wrap-around échoue sur
  `left: [10], right: [10, 20]` - le bug est réel et invisible pour la suite existante

### Reste non validé, faute de credentials

- `post_pr_comment` et `create_pull_request` (écriture GitHub : token absent)
- L'URL de push authentifiée de `publish_branch` (la mécanique git est validée ; seule la construction de l'URL avec token ne l'est pas)
- Un appel LLM réel sur chacun des quatre prompts (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` absentes)
