# Architecture et fonctionnement de Text2Cypher

Ce document décrit ce qui se passe entre une question posée dans le chat et la réponse affichée.
Pour l'installation et la configuration, voir le [README](../README.md).

## 1. Vue d'ensemble

```
┌──────────────────────────┐   HTTP (JSON)   ┌────────────────────────────────────────────┐
│  FRONTEND  (Streamlit)   │ ──────────────► │  BACKEND  (FastAPI)                        │
│  frontend/               │  POST /chat     │  backend/                                  │
│                          │                 │                                            │
│  • chat + historique     │ ◄────────────── │  api/main.py      endpoints                │
│  • Cypher affiché        │  réponse +      │  agent/agent.py   agent LangChain + prompt │
│  • table de résultat     │  cypher +       │  tools/           tools de l'agent         │
│  • graphe (neo4j-viz)    │  résultat       │  graph/           execute_cypher, schéma   │
│  • console Cypher        │                 │  semantic/        recherche vectorielle    │
└──────────────────────────┘                 └───────────┬───────────────────┬────────────┘
                                                         │ Bolt (lecture)    │ HTTPS
                                                         ▼                   ▼
                                    ┌──────────────────────────────┐  ┌────────────────────┐
                                    │  NEO4J                       │  │  LLM               │
                                    │  • données du KG             │  │  (OpenAI / Azure / │
                                    │  • couche sémantique :       │  │   Ollama)          │
                                    │    SchemaElement,            │  │  • modèle de chat  │
                                    │    GlossaryTerm + 2 vector   │  │  • embeddings      │
                                    │    index                     │  │                    │
                                    └──────────────────────────────┘  └────────────────────┘
```

Le frontend ne parle jamais directement à Neo4j ni au LLM : tout passe par l'API.

## 2. Parcours d'une question

Exemple : *« Quel est le salaire moyen actuel et la satisfaction sur la paie pour les hommes et les femmes ? »*

```
 Utilisateur    Frontend            API /chat           Agent (LLM)              Tools                 Neo4j
  │  question      │                    │                    │                      │                     │
  │───────────────►│                    │                    │                      │                     │
  │                │ POST /chat         │                    │                      │                     │
  │                │ {message, history} │                    │                      │                     │
  │                │───────────────────►│ ① crée ToolContext │                      │                     │
  │                │                    │   (vide, propre à  │                      │                     │
  │                │                    │   cette requête)   │                      │                     │
  │                │                    │───────────────────►│ ② lit prompt +       │                     │
  │                │                    │                    │   question + historique                    │
  │                │                    │                    │                      │                     │
  │                │                    │                    │ ③ search_graph_schema("salaire…")          │
  │                │                    │                    │─────────────────────►│ embedding question  │
  │                │                    │                    │                      │ puis vector search ►│
  │                │                    │                    │◄─────────────────────│ schéma utile        │
  │                │                    │                    │  Employee, Salary, SurveyResponse,         │
  │                │                    │                    │  HAS_SALARY, ANSWERED + glossaire          │
  │                │                    │                    │                      │                     │
  │                │                    │                    │ ④ écrit le Cypher    │                     │
  │                │                    │                    │   run_cypher(query)  │                     │
  │                │                    │                    │─────────────────────►│ execute_cypher ────►│
  │                │                    │                    │                      │ (garde-fous)        │
  │                │                    │                    │◄─────────────────────│ lignes (résumé)     │
  │                │                    │                    │                      │ trace enregistrée   │
  │                │                    │                    │ ⑤ rédige la réponse  │                     │
  │                │                    │◄───────────────────│   dans la langue     │                     │
  │                │                    │ ⑥ assemble :       │   de la question     │                     │
  │                │                    │  answer + cypher + │                      │                     │
  │                │                    │  result complet +  │                      │                     │
  │                │                    │  trace + tokens    │                      │                     │
  │                │◄───────────────────│                    │                      │                     │
  │  affichage     │ ⑦ rendu            │                    │                      │                     │
  │◄───────────────│                    │                    │                      │                     │
```

### ① Préparation

Fichier : [api/main.py](../backend/src/text2cypher_backend/api/main.py).

L'API crée un `ToolContext` neuf pour chaque question
([tools/context.py](../backend/src/text2cypher_backend/tools/context.py)). Il contient :
- le driver Neo4j ;
- le cache du schéma ;
- une liste vide de *traces*, qui recevra chaque requête Cypher exécutée.

Deux utilisateurs simultanés ne mélangent donc jamais leurs résultats.

### ② Lancement de l'agent

Fichier : [agent/agent.py](../backend/src/text2cypher_backend/agent/agent.py).

L'agent LangChain (`create_agent`) reçoit :
- le **prompt système** (`SYSTEM_PROMPT`), qui impose la méthode : découvrir le schéma, vérifier les valeurs,
  exécuter, répondre uniquement à partir des données ;
- l'**historique** : les `AGENT_HISTORY_MESSAGES` derniers messages, chaque réponse précédente étant accompagnée
  de son Cypher, pour permettre les questions de suivi (« Et seulement pour Sales ? ») ;
- la **liste des tools** construite par le registre ([tools/\_\_init\_\_.py](../backend/src/text2cypher_backend/tools/__init__.py)).

L'agent tourne ensuite en **boucle**. Le LLM décide d'appeler un tool, lit sa réponse, puis décide de l'étape
suivante, et ainsi de suite jusqu'à pouvoir répondre. La boucle est limitée à `AGENT_RECURSION_LIMIT` étapes.

### ③ Découverte du schéma

Le LLM ne connaît pas le graphe au départ. Il dispose de deux tools
([tools/graph_tools.py](../backend/src/text2cypher_backend/tools/graph_tools.py)) :

| Tool | Ce qu'il fait | Quand |
|---|---|---|
| `search_graph_schema` | Calcule l'embedding de la question, cherche dans les vector index les labels, relations et termes du glossaire proches, puis ajoute leurs voisins directs. Seul ce morceau du schéma est renvoyé, avec les descriptions métier et les valeurs possibles. | Par défaut, quand la couche sémantique existe. Indispensable sur un gros KG. |
| `get_graph_schema` | Renvoie le schéma complet : labels avec nombre de nœuds, propriétés, patterns, index fulltext/vector, notes de modélisation. Au-delà de 20 000 caractères, seulement une vue d'ensemble. | Quand l'agent a besoin de tout voir, ou s'il n'y a pas de couche sémantique. |

### ④ Écriture et exécution du Cypher

Le LLM écrit la requête et appelle `run_cypher`. Il peut aussi utiliser :
- `get_property_values` : vérifie l'orthographe exacte d'une valeur (`'Senior Engineer'` et non
  `'senior engineer'`) ;
- `validate_cypher` : teste une requête sans l'exécuter ;
- un **tool métier** de `tools/custom/`, par exemple `get_employee_profile`.

En cas d'erreur (syntaxe, mauvaise direction de relation, résultat vide), le message d'erreur revient au LLM.
Celui-ci corrige et réessaie, 3 tentatives au maximum.

### ⑤ Réponse du LLM

Le LLM ne reçoit qu'une version **compacte** du résultat (`CypherResult.to_llm_text`) :
- `CYPHER_MAX_ROWS_FOR_LLM` lignes au maximum (50 par défaut) ;
- listes longues (embeddings par exemple) et textes longs tronqués.

Cela économise des tokens. Le LLM rédige la réponse dans la langue de la question, sans y inclure le Cypher :
l'interface l'affiche séparément.

### ⑥ Assemblage de la réponse API

L'API récupère dans les traces la **dernière requête réussie de type `answer`** (`ToolContext.final_result`),
c'est-à-dire celle qui a produit la réponse. Elle renvoie son **résultat complet** : jusqu'à `CYPHER_MAX_ROWS`
lignes (1 000 par défaut), contre 50 pour le LLM. Les requêtes d'aide (`get_property_values`…) sont de type
`helper` : elles apparaissent dans la trace mais ne deviennent jamais le résultat principal.

| Champ | Contenu |
|---|---|
| `answer` | texte de la réponse (markdown) |
| `cypher` | la requête qui a produit la réponse |
| `result` | résultat complet : `columns`, `rows`, `graph {nodes, relationships}`, `summary`, `truncated` |
| `queries` | toutes les requêtes exécutées, y compris les erreurs et les requêtes d'aide |
| `tool_calls` | les appels de tools du LLM, avec arguments et réponses |
| `schema_matches` | ce que la couche sémantique a trouvé |
| `usage` | tokens consommés |
| `elapsed_ms` | durée totale |

### ⑦ Affichage

Fichiers : [app.py](../frontend/src/text2cypher_frontend/app.py) et
[rendering.py](../frontend/src/text2cypher_frontend/rendering.py).

- la réponse en markdown ;
- **Cypher query** : la requête exécutée ;
- **Result** : la table (nœuds et relations rendus lisibles), l'export CSV/JSON et les warnings de Neo4j ;
- **Queries & tools** (option ⚙️) : la trace complète ;
- **Usage** : tokens et durée ;
- à droite, le **graphe** dès que le résultat contient des nœuds ou des relations ; le bouton du modèle de
  données affiche les labels et les relations.

## 3. Le cœur : `execute_cypher`

Toute requête passe par
[graph/executor.py](../backend/src/text2cypher_backend/graph/executor.py), qu'elle vienne de l'agent, d'un tool
custom ou de la console Cypher (`POST /cypher`) :

```
 requête Cypher
      │
      ▼
 nettoyage            retire ```cypher ... ``` et le ";" final souvent ajoutés par le LLM
      │
      ▼
 barrière 1           analyse statique (graph/guard.py) :
      │               CREATE, MERGE, SET, DELETE... hors chaînes et commentaires ?  → refus
      ▼
 barrière 2           EXPLAIN : Neo4j planifie sans exécuter et donne le type de requête
      │               "r" = lecture → OK ; "rw"/"w"/"s" → refus (attrape apoc.create.* par ex.)
      ▼               Les erreurs de syntaxe sont aussi détectées ici.
 barrière 3           exécution en session READ_ACCESS + timeout (CYPHER_TIMEOUT_SECONDS)
      │
      ▼
 lecture du flux      arrêt à CYPHER_MAX_ROWS lignes (truncated = true au-delà)
      │
      ▼
 sérialisation        nœud, relation, chemin, date, durée, point, vecteur, bytes, NaN... → JSON
      │               (graph/serialization.py) + collecte des nœuds et relations pour le graphe
      ▼
 CypherResult         columns, rows, graph, summary (type, temps, compteurs, notifications)
```

Erreurs possibles : `CypherWriteForbidden` (HTTP 403), `CypherSyntaxErrorDetail` et `CypherError` (HTTP 400).

## 4. Ce qui est préparé en amont

```
uv run text2cypher-load               (loader/cli.py)
   │
   ├─ télécharge le dump h8 → parse les blocs COPY → Employee, Department, Title, Salary...
   ├─ génère les données synthétiques (enquête, candidats, compétences, projets, bureaux)
   └─ lance la construction de la couche sémantique ─┐
                                                     │
uv run text2cypher-semantic   ◄──────────────────────┘   (semantic/layer.py, relançable seul, sur n'importe quel KG)
   │
   ├─ introspection du schéma (graph/schema.py)
   │    labels + propriétés  : apoc.meta.data, échantillon de SCHEMA_SAMPLE_SIZE nœuds par label
   │    relations            : échantillon par type de relation (pour ne pas rater les relations rares)
   ├─ fusion avec backend/data/schema_descriptions.yaml (descriptions métier, notes)
   ├─ échantillonnage des valeurs catégorielles (≤ 25 valeurs distinctes, hors identifiants)
   ├─ embeddings des éléments du schéma et de backend/data/business_glossary.yaml
   └─ écrit dans Neo4j : (:SchemaElement), (:GlossaryTerm) + vector index
                         schema_element_embedding et glossary_term_embedding
```

Modèle de la couche sémantique :

```
(:GlossaryTerm)-[:HAS_TERM]->(:GlossaryTerm)            taxonomie du glossaire
(:GlossaryTerm)-[:DESCRIBES]->(:SchemaElement)          terme métier → label ou relation
(:SchemaElement {kind:'relationship'})-[:FROM_LABEL]->(:SchemaElement {kind:'label'})
(:SchemaElement {kind:'relationship'})-[:TO_LABEL]->(:SchemaElement {kind:'label'})
```

Ces labels sont exclus de l'introspection présentée à l'agent (`SCHEMA_EXCLUDED_LABELS`).

Au **démarrage de l'API**, le schéma est introspecté une fois puis mis en **cache** (`SchemaCache`), ce qui prend
environ 2 s sur 3,5M de nœuds. `POST /schema/refresh` le recalcule.

## 5. Ajouter un tool

Créer `backend/src/text2cypher_backend/tools/custom/<nom>.py` exposant `create_tools(ctx) -> list[BaseTool]`.
Le module est découvert au démarrage ; les fichiers préfixés par `_` sont ignorés.
Point de départ : [\_template.py](../backend/src/text2cypher_backend/tools/custom/_template.py).

Règles :
- la docstring du tool est ce que lit le LLM pour décider quand l'appeler : elle doit être explicite ;
- exécuter le Cypher via `ctx.run_cypher(...)` pour bénéficier des garde-fous et de la trace :
  `kind="answer"` si le résultat répond à la question, `kind="helper"` sinon ;
- renvoyer une chaîne courte (`result.to_llm_text()`) et renvoyer les erreurs sous forme de texte plutôt que lever
  une exception.

## 6. Leviers pour améliorer les réponses

| Problème constaté | Levier |
|---|---|
| L'agent confond des concepts métier | enrichir `business_glossary.yaml`, puis `text2cypher-semantic` |
| Il interprète mal une propriété | la décrire dans `schema_descriptions.yaml` (ou lister ses `values`) |
| Il se trompe sur une règle de modélisation (historique, valeur « courante »…) | ajouter une ligne dans `notes:` de `schema_descriptions.yaml` |
| Une question fréquente est complexe | créer un tool dédié dans `tools/custom/` |
| Le comportement général ne convient pas | ajuster `SYSTEM_PROMPT` dans `agent/agent.py` |
| La recherche de schéma ramène trop ou pas assez | régler `SEMANTIC_TOP_K` / `SEMANTIC_MIN_SCORE` |

Après une modification des fichiers YAML : `uv run text2cypher-semantic`, puis `POST /schema/refresh`
(ou redémarrage de l'API).
