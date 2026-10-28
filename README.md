# Text2Cypher : discuter avec un knowledge graph Neo4j

[![CI](https://github.com/lcarlu/text2cypher/actions/workflows/ci.yml/badge.svg)](https://github.com/lcarlu/text2cypher/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Chatbot qui répond à des questions en langage naturel sur un (très gros) knowledge graph Neo4j.
Pour chaque réponse, il renvoie **la requête Cypher exécutée et son résultat complet** (table, graphe, JSON).

Stack : agent LangChain, API FastAPI, interface Streamlit, LLM OpenAI / Azure OpenAI / Ollama, uv pour Python.
Un jeu de données RH d'environ 3 millions de nœuds est fourni pour tester sans rien préparer.

Le fonctionnement détaillé (parcours d'une question, garde-fous, couche sémantique) est décrit dans
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Démarrage en une commande (Docker)

Prérequis : Docker (avec Compose v2.24+), 4 Go de mémoire pour Docker, et une clé LLM (OpenAI, Azure OpenAI) ou
Ollama installé localement.

```bash
git clone https://github.com/lcarlu/text2cypher.git && cd text2cypher
cp .env.example .env          # renseigner l'accès LLM (voir "Configuration")
docker compose up --build
```

Puis ouvrir **http://localhost:8501**.

Au premier lancement, le service `init` télécharge le jeu de données, le charge dans Neo4j et construit la couche
sémantique : environ 3 minutes pour le jeu complet, environ 30 s avec `DATASET_SIZE=small` dans `.env`. Les
lancements suivants réutilisent la base existante et démarrent tout de suite.

| Service | URL |
|---|---|
| Chatbot | http://localhost:8501 |
| API (documentation interactive) | http://localhost:8000/docs |
| Neo4j Browser (`neo4j` / `password123`) | http://localhost:7474 |

Arrêt : `docker compose down`. Pour repartir d'une base vide : `docker compose down -v`.

### Derrière un proxy d'entreprise

Si le proxy intercepte le TLS, le build échoue avec `invalid peer certificate` ou `CERTIFICATE_VERIFY_FAILED`.
Il faut alors déposer le certificat racine du proxy dans [`certs/`](certs/README.md), par exemple
`cp "$SSL_CERT_FILE" certs/corporate-ca.pem` : la chaîne complète déjà utilisée par le Python local. Les images
l'ajoutent à leur magasin de certificats. Ces fichiers ne sont jamais versionnés.

## Développement local (uv)

Prérequis : [uv](https://docs.astral.sh/uv/) et Docker pour Neo4j (ou une instance Neo4j existante).

```bash
cp .env.example .env
docker compose up -d neo4j                  # Neo4j seul

cd backend
uv sync
uv run text2cypher-load                     # jeu complet ; --size small pour 10k employés
uv run text2cypher-api --reload             # http://127.0.0.1:8000/docs

cd ../frontend                              # dans un autre terminal
uv sync
uv run text2cypher-ui                       # http://localhost:8501
```

## Structure

```
text2cypher/
├── docker-compose.yml        stack complète : neo4j, init (chargement), api, ui
├── .env.example              configuration partagée backend / frontend / Docker
├── certs/                    certificats de proxy d'entreprise optionnels (non versionnés)
├── backend/                  API FastAPI (uv)
│   ├── data/
│   │   ├── schema_descriptions.yaml   descriptions métier des labels / relations / propriétés
│   │   └── business_glossary.yaml     glossaire métier (taxonomie)
│   ├── src/text2cypher_backend/
│   │   ├── api/              endpoints FastAPI
│   │   ├── agent/            agent LangChain + prompt
│   │   ├── graph/            execute_cypher, sérialisation, garde-fous, introspection du schéma
│   │   ├── semantic/         couche sémantique (recherche vectorielle sur le schéma + glossaire)
│   │   ├── tools/            tools de l'agent (+ tools/custom/ pour les tools métier)
│   │   └── loader/           chargement du jeu de données RH
│   └── tests/
├── frontend/                 chatbot Streamlit (uv)
└── docs/ARCHITECTURE.md      fonctionnement détaillé
```

## Le jeu de données RH

Les données ne sont pas stockées dans le dépôt : elles sont **téléchargées et régénérées** à l'identique.

- Le dump `employees` de [h8/employees-database](https://github.com/h8/employees-database) (CC BY-SA 3.0) est
  téléchargé depuis la release [`data-v1`](https://github.com/lcarlu/text2cypher/releases/tag/data-v1) de ce dépôt,
  sinon depuis le dépôt h8 à un commit figé. Son empreinte SHA-256 est vérifiée. Il est parsé sans PostgreSQL et
  chargé en graphe.
- Les données synthétiques (enquête de satisfaction, candidats, compétences, projets, bureaux) sont générées avec un
  seed fixe : tout le monde obtient exactement le même graphe.

| Nœud | Volume | | Relation | Volume |
|---|---:|---|---|---:|
| Employee | 300 024 | | (:Employee)-[:HAS_SALARY]->(:Salary) | 2 844 047 |
| Salary | 2 844 047 | | (:Employee)-[:HAS_SKILL {level}]->(:Skill) | 566 385 |
| SurveyResponse | 1 000 | | (:Employee)-[:HAS_TITLE {fromDate, toDate, isCurrent}]->(:Title) | 443 308 |
| Project | 150 | | (:Employee)-[:WORKS_IN {fromDate, toDate, isCurrent}]->(:Department) | 331 603 |
| Skill | 33 | | (:Employee)-[:BASED_IN]->(:Office) | 300 024 |
| Candidate / JobRequisition | 12 / 12 | | (:Employee)-[:REPORTS_TO]->(:Employee) | ~240 000 |
| Department / Office / Title | 9 / 8 / 7 | | MANAGES, WORKS_ON, ANSWERED, APPLIED_TO, OPENED_IN, OWNED_BY | |

Les types Neo4j sont variés (DATE, DATETIME, POINT, FLOAT, BOOLEAN…), ce qui permet de tester la sérialisation.

Options du chargeur :
- `--size small|full` : taille du jeu ;
- `--limit N` : charge les N premiers employés ;
- `--if-empty` : ne fait rien si la base contient déjà des données ;
- `--reset` : vide la base avant le chargement, après confirmation.

Sans `--reset` ni `--if-empty`, le chargeur refuse d'écrire dans une base non vide.

## API

| Méthode | Endpoint | Rôle |
|---|---|---|
| POST | `/chat` | `{message, history?}` → réponse, `cypher`, `result` (résultat complet), `queries` (toutes les requêtes), `tool_calls`, `usage` |
| POST | `/cypher` | exécute directement une requête (`{query, parameters, max_rows}`) avec les mêmes garde-fous |
| POST | `/cypher/explain` | valide une requête sans l'exécuter (type de requête, warnings) |
| GET | `/schema` | schéma vu par l'agent (texte + JSON) ; `POST /schema/refresh` pour le recalculer |
| GET | `/schema/graph` | modèle de données sous forme de graphe (visualisation) |
| POST | `/schema/search` | debug de la couche sémantique |
| GET | `/tools`, `/health` | tools chargés, état de Neo4j et de la couche sémantique |

### `execute_cypher`

[`graph/executor.py`](backend/src/text2cypher_backend/graph/executor.py) est le point d'entrée unique, utilisé par
l'API et par tous les tools. Il renvoie un `CypherResult` :

- `columns`, `rows` : chaque valeur est sérialisée en JSON, quel que soit son type : nœud, relation, chemin,
  date / datetime / time / duration, point (cartésien ou WGS-84), vecteur, bytes, map, liste imbriquée,
  NaN / Infinity ;
- `graph` : tous les nœuds et relations trouvés dans le résultat, dédoublonnés (y compris dans les listes, maps et
  chemins), prêts pour la visualisation ;
- `summary` : type de requête, compteurs, temps d'exécution, notifications du serveur (label inconnu,
  dépréciation…) ;
- `truncated` : `true` si le résultat dépasse `CYPHER_MAX_ROWS`.

En mode lecture seule (par défaut), une écriture est bloquée à trois niveaux : analyse statique des clauses,
type de requête renvoyé par `EXPLAIN` (ce qui attrape aussi les procédures qui écrivent, comme `apoc.create.*`) et
session en `READ_ACCESS`. Un timeout serveur s'applique à chaque requête.

## Comment l'agent découvre le schéma (approche hybride)

- `get_graph_schema` : introspection échantillonnée (APOC), complétée par un échantillonnage par type de relation,
  sinon les relations rares disparaissent sur un gros graphe. Elle est fusionnée avec `schema_descriptions.yaml` et
  mise en cache. Si le schéma est trop gros, le tool renvoie une vue d'ensemble.
- `search_graph_schema` : il calcule l'embedding de la question et retrouve les labels, relations et termes du
  glossaire pertinents (vector index), puis élargit d'un saut. Seule la partie utile du schéma part dans le prompt,
  avec les valeurs catégorielles échantillonnées (titres, statuts…).
- `get_property_values` : vérifie l'orthographe exacte d'une valeur avant de filtrer dessus.
- `validate_cypher` / `run_cypher` : validation et exécution.

## Ajouter des tools métier

Déposer un module dans [`tools/custom/`](backend/src/text2cypher_backend/tools/custom/) qui expose
`create_tools(ctx) -> list[BaseTool]` : il est découvert automatiquement au démarrage. Partir de
[`_template.py`](backend/src/text2cypher_backend/tools/custom/_template.py) (les fichiers préfixés par `_` sont
ignorés). [`hr_tools.py`](backend/src/text2cypher_backend/tools/custom/hr_tools.py) est un exemple propre au jeu de
données RH.

Passer par `ctx.run_cypher(...)` : les garde-fous s'appliquent et la requête est tracée. L'API la renvoie alors
comme résultat (`kind="answer"`) ou seulement dans la trace (`kind="helper"`).

## Brancher un autre knowledge graph

Le code est générique : seuls les fichiers de description, le tool d'exemple et les questions d'exemple sont propres
au jeu RH.

Prérequis côté base :
- Neo4j **5.13+**, idéalement 2025+ ;
- APOC recommandé : sans APOC, l'introspection passe par `db.schema.*`, qui parcourt toute la base ;
- un utilisateur en lecture seule pour le chatbot ;
- des droits d'écriture une seule fois, pour construire la couche sémantique.

Étapes :
1. Dans `.env`, renseigner `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` et `NEO4J_DATABASE`. Ajouter les labels
   techniques à cacher à l'agent dans `SCHEMA_EXCLUDED_LABELS`.
2. Remplacer `backend/data/schema_descriptions.yaml` et `business_glossary.yaml` par la description du domaine :
   c'est ce qui améliore le plus la qualité des requêtes.
3. Lancer `uv run text2cypher-semantic` pour construire la couche sémantique. Elle crée des nœuds `SchemaElement`
   et `GlossaryTerm` et deux vector index dans la base cible.
4. Supprimer `tools/custom/hr_tools.py`, adapter `frontend/src/text2cypher_frontend/data/example_questions.json` et
   ajouter des tools métier si besoin.

Avec Docker, définir `COMPOSE_NEO4J_URI` (l'URI vue depuis les conteneurs) puis ne démarrer que `api` et `ui` :
`docker compose up --build --no-deps api ui`.

## Configuration (`.env`)

| Variable | Défaut | |
|---|---|---|
| `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL`, `OPENAI_EMBEDDING_MODEL` | | OpenAI (fournisseur par défaut) |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `OPENAI_API_VERSION` | | Azure OpenAI, utilisé si `AZURE_OPENAI_ENDPOINT` est défini |
| `LOCAL_MODEL`, `OLLAMA_CHAT_MODEL`, `OLLAMA_EMBEDDING_MODEL` | `false` | Ollama, utilisé si `LOCAL_MODEL=true` |
| `LLM_TEMPERATURE` / `LLM_REASONING_EFFORT` | vide / `low` | laisser la température vide pour les modèles de raisonnement (gpt-5.x) |
| `CYPHER_READ_ONLY` | `true` | refuse toute écriture |
| `CYPHER_MAX_ROWS` / `CYPHER_MAX_ROWS_FOR_LLM` | 1000 / 50 | lignes renvoyées à l'API / montrées au LLM |
| `CYPHER_TIMEOUT_SECONDS` | 30 | timeout par requête |
| `SCHEMA_SAMPLE_SIZE` | 1000 | taille d'échantillon de l'introspection |
| `SEMANTIC_TOP_K` / `SEMANTIC_MIN_SCORE` | 8 / 0.6 | recherche dans la couche sémantique |
| `DATASET_SIZE` | `full` | jeu chargé par `docker compose up` (`small` ou `full`) |
| `NEO4J_HEAP` / `NEO4J_PAGECACHE` | 1G / 1G | mémoire de Neo4j dans Docker |
| `COMPOSE_NEO4J_URI` | `neo4j://neo4j:7687` | Neo4j vu depuis les conteneurs `api` / `init` (base externe) |
| `NEO4J_HTTP_PORT`, `NEO4J_BOLT_PORT`, `API_PORT`, `UI_PORT` | 7474, 7687, 8000, 8501 | ports exposés |

## Tests

```bash
cd backend
uv run pytest            # tests unitaires + intégration Neo4j (ignorés si la base est injoignable)
uv run ruff check src && uv run ruff format --check src
```

La CI GitHub Actions lance le lint des deux projets, charge le petit jeu de données dans un vrai Neo4j, exécute tous
les tests du backend et vérifie le build des images Docker.

## Licence et crédits

Le code est distribué sous licence [MIT](LICENSE).

- Jeu de données employés : Dmitry Stropalov, [h8/employees-database](https://github.com/h8/employees-database),
  sous licence [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/). Il est dérivé de la base exemple
  MySQL employees de Fusheng Wang et Carlo Zaniolo.
- Projet inspiré de `neo4j_text2SQL` (couche sémantique Neo4j pour le Text2SQL), lui-même issu des travaux d'Alex
  Gilmore chez Neo4j (neocarta).
- Neo4j est une marque déposée de Neo4j, Inc.

Détails dans [NOTICE](NOTICE).
