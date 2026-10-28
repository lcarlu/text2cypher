# Text2Cypher : discuter avec un knowledge graph Neo4j

Chatbot qui répond à des questions en langage naturel sur un (très gros) knowledge graph Neo4j.
Pour chaque réponse, il renvoie **la requête Cypher exécutée et son résultat complet** (table, graphe, JSON).

Inspiré de `neo4j_text2SQL` : même stack (agent LangChain, FastAPI, Streamlit, OpenAI / Azure OpenAI / Ollama).
L'agent interroge ici directement le graphe de données au lieu de générer du SQL.

Le fonctionnement détaillé (parcours d'une question, garde-fous, couche sémantique) est décrit dans
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
text2cypher/
├── docker-compose.yml        Neo4j + APOC en local (ports 7475 / 7688)
├── .env.example              configuration partagée backend / frontend
├── backend/                  API FastAPI (uv)
│   ├── data/
│   │   ├── schema_descriptions.yaml   descriptions métier des labels / relations / propriétés
│   │   └── business_glossary.yaml     glossaire métier (taxonomie)
│   ├── src/text2cypher_backend/
│   │   ├── api/              endpoints FastAPI
│   │   ├── agent/            agent LangChain + prompt
│   │   ├── graph/            execute_cypher, sérialisation, garde-fous, introspection du schéma
│   │   ├── semantic/         couche sémantique (recherche vectorielle sur le schéma + glossaire)
│   │   ├── tools/            tools de l'agent (+ tools/custom/ pour les tiens)
│   │   └── loader/           chargement des dummy data RH
│   └── tests/
└── frontend/                 chatbot Streamlit (uv)
```

## Démarrage rapide

Prérequis : [uv](https://docs.astral.sh/uv/), Docker (ou une instance Neo4j 5.13+, idéalement 2025+, avec APOC) et un accès LLM.

```bash
cp .env.example .env              # puis renseigner l'accès LLM (OpenAI, Azure ou Ollama)
docker compose up -d              # Neo4j : http://localhost:7475 (neo4j / password123)

cd backend
uv sync
uv run text2cypher-load           # dummy data RH complètes (~3 min), --limit 10000 pour un échantillon
uv run text2cypher-api            # http://127.0.0.1:8000/docs

cd ../frontend                    # dans un autre terminal
uv sync
uv run text2cypher-ui             # http://localhost:8501
```

## Les dummy data RH

`text2cypher-load` télécharge le dump h8 `employees` (déjà utilisé par `neo4j_text2SQL`), le parse sans PostgreSQL
et le charge en graphe. Il ajoute les données synthétiques du repo d'origine (enquête de satisfaction, candidats)
et d'autres données générées avec un seed fixe (compétences, projets, bureaux) pour enrichir le graphe.

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
Le chargeur refuse d'écrire dans une base non vide, sauf avec `--reset` (qui demande une confirmation).

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

- `columns`, `rows` : chaque valeur est sérialisée en JSON, quel que soit son type : nœud, relation, chemin, date / datetime /
  time / duration, point (cartésien ou WGS-84), vecteur, bytes, map, liste imbriquée, NaN / Infinity ;
- `graph` : tous les nœuds et relations trouvés dans le résultat, dédoublonnés (y compris dans les listes, maps et
  chemins), prêts pour la visualisation ;
- `summary` : type de requête, compteurs, temps d'exécution, notifications du serveur (label inconnu, dépréciation…) ;
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

## Ajouter tes propres tools

Dépose un module dans [`tools/custom/`](backend/src/text2cypher_backend/tools/custom/) qui expose
`create_tools(ctx) -> list[BaseTool]` : il est découvert automatiquement au démarrage. Copie
[`_template.py`](backend/src/text2cypher_backend/tools/custom/_template.py) (les fichiers préfixés par `_` sont ignorés) ;
[`hr_tools.py`](backend/src/text2cypher_backend/tools/custom/hr_tools.py) est un exemple métier à supprimer quand tu passes sur ton KG.

Passe par `ctx.run_cypher(...)` : les garde-fous s'appliquent et la requête est tracée. L'API la renvoie alors comme
résultat (`kind="answer"`) ou seulement dans la trace (`kind="helper"`).

## Brancher ton vrai knowledge graph

1. Dans `.env`, pointe `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` et `NEO4J_DATABASE` vers ton instance. Un utilisateur Neo4j
   en lecture seule est recommandé en plus des garde-fous.
2. Remplace `backend/data/schema_descriptions.yaml` et `business_glossary.yaml` par la description de ton domaine.
   C'est ce qui améliore le plus la qualité des requêtes.
3. Lance `uv run text2cypher-semantic` pour construire la couche sémantique : des nœuds `SchemaElement` et `GlossaryTerm`
   et deux vector index, créés dans la base cible. Il faut donc un utilisateur avec des droits d'écriture pour
   cette étape uniquement.
4. Supprime `tools/custom/hr_tools.py` et ajoute tes propres tools si besoin.

## Configuration principale (`.env`)

| Variable | Défaut | |
|---|---|---|
| `CYPHER_READ_ONLY` | `true` | refuse toute écriture |
| `CYPHER_MAX_ROWS` / `CYPHER_MAX_ROWS_FOR_LLM` | 1000 / 50 | lignes renvoyées à l'API / montrées au LLM |
| `CYPHER_TIMEOUT_SECONDS` | 30 | timeout par requête |
| `SCHEMA_SAMPLE_SIZE` | 1000 | taille d'échantillon de l'introspection |
| `SEMANTIC_TOP_K` / `SEMANTIC_MIN_SCORE` | 8 / 0.6 | recherche dans la couche sémantique |
| `LLM_TEMPERATURE` / `LLM_REASONING_EFFORT` | vide / `low` | laisser la température vide pour les modèles de raisonnement (gpt-5.x) |
| `AZURE_OPENAI_ENDPOINT`, `LOCAL_MODEL` | | choix du fournisseur LLM (Azure, sinon Ollama, sinon OpenAI) |

## Tests

```bash
cd backend && uv run pytest     # tests unitaires + intégration Neo4j (ignorés si la base est injoignable)
uv run ruff check src
```
