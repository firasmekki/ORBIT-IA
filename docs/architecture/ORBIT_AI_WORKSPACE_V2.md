# ORBIT AI — Workspace V2 : analyse de l'existant et architecture cible

> Statut : **proposition de design, rien n'est implémenté**.
> Périmètre : synchronisation de dossiers locaux volumineux, pipeline documentaire,
> OCR/Vision, RAG, agent, traçabilité de lecture.
> Hors périmètre (non touché) : Qwen3‑30B (port 8096), Qwen3‑VL (port 8097),
> PostgreSQL (moteur/rôles), Auth, conversion du modèle BF16.
> Analyse faite sur l'arbre de travail du 2026‑10‑08 (branche `main`, avec de
> nombreuses modifications non commitées : l'analyse porte sur ces fichiers tels
> qu'ils sont sur le disque).

Ordre de priorité appliqué à chaque arbitrage : **fiabilité → couverture →
performance → vitesse**, sans jamais sacrifier la traçabilité.

---

## Résumé exécutif

1. **La cause de fond des 422/500 et de la lenteur** : la synchronisation actuelle
   mélange les quatre phases (découverte, sélection, indexation, analyse) dans une
   seule boucle exécutée **par le navigateur**, au moment où le dossier est
   connecté, sur **tout** le dossier, et répétée **toutes les 10 secondes**.
   Pour 66 336 fichiers, chaque cycle relit et hache *l'intégralité des octets*
   (~9,9 Go) dans le navigateur, puis envoie un fichier par requête HTTP, et le
   serveur extrait (OCR compris) **dans la requête HTTP**, sur la boucle
   d'événements de l'API.
2. **Les 422 ne sont pas des erreurs de protocole** : ce sont des *résultats
   métier* (image sans texte OCR, PDF scanné illisible, fichier corrompu ou
   protégé, fichier vide) transformés en exceptions HTTP. Les 500 viennent
   d'exceptions non classées (stockage objet, caractères NUL dans le texte,
   erreurs SQL) qui ne sont pas rattachées à un fichier.
3. **Il n'existe aucun registre par fichier** : un fichier non supporté, ignoré
   ou en échec n'existe nulle part en base. Impossible aujourd'hui de prouver
   « ORBIT a lu ce document » ou « 100 % de la sélection est couverte ».
4. **Cible** : un *Source Connector* qui découvre l'arborescence sans ouvrir les
   fichiers, des **règles de sélection** (include/exclude par chemin) au lieu de
   listes de fichiers, un **registre de fichiers** avec un statut obligatoire
   par fichier (invariant `TOTAL = Σ statuts`, garanti par la base), une **file
   de travaux dans PostgreSQL** (pas de Redis, pas de Qdrant) traitée par un
   conteneur `worker` séparé, avec une concurrence bornée par type de ressource
   (CPU, OCR, GPU Vision = 1, embeddings).
5. **Décision structurante à valider** (§B.2) : pour les très gros dossiers
   situés sur la machine serveur, la découverte et la lecture doivent se faire
   **côté serveur**, via une racine déclarée par l'administrateur et montée en
   lecture seule (même mécanisme que le *watch folder* existant). Le mode
   « navigateur » actuel reste disponible pour les postes clients distants, mais
   réécrit en mode lot.

---

## A. Architecture actuelle

### A.1 Vue d'ensemble

```
Navigateur (Vite/React, :8081 via nginx)
  │  File System Access API (handle du dossier, conservé dans IndexedDB)
  │
  ├── /api/workspaces/*  ─┐
  ├── /api/documents/*   ─┤   Backend FastAPI (conteneur, :8095)
  └── /api/chat/*        ─┘     │
                                ├── PostgreSQL 16 + pgvector (documents, pages, chunks, audit)
                                ├── SeaweedFS (S3, fichiers originaux)
                                ├── Ollama (nomic-embed-text, 768 dim, GPU)
                                ├── MCP server (conteneur, outils ACL-filtrés)
                                ├── llama-server natif :8096  Qwen3-30B-A3B Q4_K_M (LLM_GPU_LAYERS=1 par défaut)
                                └── llama-server natif :8097  Qwen3-VL-8B Q4_K_M (--n-gpu-layers 99, optionnel)
Tâches de fond dans le processus API : run_folder_watcher, run_embedding_worker
```

### A.2 Composants existants (inventaire)

| Domaine | Fichier | Rôle |
|---|---|---|
| Workspace | [backend/app/models/workspace.py](../../backend/app/models/workspace.py) | `Workspace`, `WorkspaceMember`, workspace « Général » fixe |
| Workspace | [backend/app/policy/workspace.py](../../backend/app/policy/workspace.py) | Qui peut utiliser/gérer un workspace, `grant_in_workspace` (règles Director, dossiers restreints) |
| Workspace | [backend/app/routers/workspaces.py](../../backend/app/routers/workspaces.py) | CRUD, membres, dossiers partagés, `POST /sync/plan`, `POST /sync/file` |
| Sync | [backend/app/services/workspace_sync.py](../../backend/app/services/workspace_sync.py) | `plan_sync` (manifest → à envoyer / inchangés / retirés), `index_file` (extraction + stockage + pages) |
| Sync front | [frontend/src/lib/localFs.ts](../../frontend/src/lib/localFs.ts) | `scanWorkspace` (générateur récursif), lecture d'un fichier |
| Sync front | [frontend/src/lib/workspaceSync.ts](../../frontend/src/lib/workspaceSync.ts) | Boucle complète : scan → hash SHA‑256 → plan → upload |
| Sync front | [frontend/src/lib/workspaceSyncPolicy.js](../../frontend/src/lib/workspaceSyncPolicy.js) | Extensions supportées, dossiers ignorés, taille max |
| Sync front | [frontend/src/context/WorkspaceContext.tsx](../../frontend/src/context/WorkspaceContext.tsx) | Connexion du dossier, restauration du handle, **polling 10 s** |
| Sync front | [frontend/src/components/WorkspacePicker.tsx](../../frontend/src/components/WorkspacePicker.tsx) | Choix/création de workspace, sélection du dossier racine |
| Dossiers | [backend/app/services/folders.py](../../backend/app/services/folders.py), [frontend/src/components/FoldersPanel.tsx](../../frontend/src/components/FoldersPanel.tsx) | Arbre des dossiers **déjà indexés** (partage, restriction) |
| Extraction | [backend/app/rag/extract.py](../../backend/app/rag/extract.py) | Parseurs PDF/DOCX/XLSX/XLS/CSV/PPTX/TXT/MD/images, OCR Tesseract par page |
| Indexation | [backend/app/rag/ingest.py](../../backend/app/rag/ingest.py) | `write_pages` (texte immédiatement cherchable), `embed_document` (chunks 800/120 + embeddings par lots de 64) |
| Indexation | [backend/app/services/embedding_worker.py](../../backend/app/services/embedding_worker.py) | File = `documents.status='indexing'`, un document à la fois, dans le processus API |
| Indexation | [backend/app/rag/watcher.py](../../backend/app/rag/watcher.py) | Dossier surveillé côté serveur (montage Docker), ingestion synchrone |
| Upload | [backend/app/routers/documents.py](../../backend/app/routers/documents.py) | Upload manuel (fichier / dossier / texte) |
| RAG | [backend/app/rag/retriever.py](../../backend/app/rag/retriever.py) | Recherche hybride vecteur + lexical (regex) filtrée ACL en SQL, signal hors‑périmètre |
| Recherche exacte | [backend/app/services/text_search.py](../../backend/app/services/text_search.py), [backend/app/rag/search_keyword.py](../../backend/app/rag/search_keyword.py) | Comptage exact sur `document_pages` |
| Embeddings | [backend/app/rag/embeddings.py](../../backend/app/rag/embeddings.py) | Client Ollama `/api/embed` |
| LLM | [backend/app/agent/llm_client.py](../../backend/app/agent/llm_client.py), [model_provider.py](../../backend/app/agent/model_provider.py) | Client OpenAI‑compatible vers :8096, `complete_json` à grammaire contrainte |
| Vision | [backend/app/agent/vision_client.py](../../backend/app/agent/vision_client.py) | `describe_image` → JSON structuré (description, objets, mesures, texte, warnings, confidence) |
| Stats | [backend/app/agent/stat_engine.py](../../backend/app/agent/stat_engine.py) | Statistiques déterministes (≤ 10 000 lignes) |
| Agent | [backend/app/agent/orchestrator.py](../../backend/app/agent/orchestrator.py), [intent.py](../../backend/app/agent/intent.py) | Routage déterministe par intentions + outils MCP + LLM |
| Agent | [backend/app/agent/grounding.py](../../backend/app/agent/grounding.py) | Vérification que chaque nombre de la réponse provient d'une preuve |
| Agent | [backend/app/agent/chart.py](../../backend/app/agent/chart.py), [chart_dialog.py](../../backend/app/agent/chart_dialog.py) | Graphiques, dialogue de clarification |
| Agent | [backend/app/agent/local_files.py](../../backend/app/agent/local_files.py) | « Local File Agent » (lecture client à la demande) |
| MCP | [backend/app/mcp/server.py](../../backend/app/mcp/server.py) | 10 outils : search_documents, get_document, search_document_text, search_keyword, list_documents, get_document_section, read_spreadsheet_data, generate_chart, search_database, get_company_information |
| Sécurité | [backend/app/policy/engine.py](../../backend/app/policy/engine.py), [rules.py](../../backend/app/policy/rules.py) | RBAC/ABAC, `document_scope` |
| Audit | [backend/app/core/audit_logger.py](../../backend/app/core/audit_logger.py) | `log_event` (audit_logs en INSERT/SELECT seulement) |
| Schéma | [backend/app/core/migrations.py](../../backend/app/core/migrations.py) | Migrations idempotentes maison, rôles `orbitia_app`, sauvegarde avant migration |

### A.3 Architecture actuelle de Workspace Sync (flux réel)

```
Connexion du dossier (WorkspaceContext.connectFolder)
  └─ runSync immédiat, sur TOUT le dossier — pas d'étape de sélection
       1. folderSnapshot(handle)          scan récursif complet n°1 (getFile() par fichier)
       2. syncWorkspaceFolder :
          a. scanWorkspace                scan récursif complet n°2
          b. pour chaque fichier supporté : file.arrayBuffer() + SHA-256  ← lit TOUS les octets
          c. POST /sync/plan              UN seul JSON contenant tout le manifest
          d. pour chaque fichier à envoyer : POST /sync/file (multipart, concurrence 3)
                 └─ serveur : extract() (+OCR) → S3 → write_pages → status "indexing"
       3. embedding_worker (dans l'API) : embeddings document par document
Puis toutes les 10 s (FOLDER_POLL_MS) : folderSnapshot complet ; si différent → runSync complet
```

### A.4 Problèmes identifiés (classés par gravité)

**Bloquants pour 66 336 fichiers**

| # | Problème | Où | Effet |
|---|---|---|---|
| P1 | Aucune phase de sélection : connecter = synchroniser tout | [WorkspaceContext.tsx:217-251](../../frontend/src/context/WorkspaceContext.tsx#L217-L251) | Impossible de choisir « Client A + Client C/Audit » |
| P2 | Le navigateur lit et hache **tous** les octets de tous les fichiers supportés à chaque synchro | [workspaceSync.ts:27-30](../../frontend/src/lib/workspaceSync.ts#L27-L30), [workspaceSync.ts:57-76](../../frontend/src/lib/workspaceSync.ts#L57-L76) | ~9,9 Go relus à chaque cycle ; pics RAM (4 × 50 Mo en `arrayBuffer`) |
| P3 | Polling toutes les 10 s avec scan complet, **sans garde contre le chevauchement** (seul `syncingRef` est vérifié, pas un scan en cours) | [WorkspaceContext.tsx:158-187](../../frontend/src/context/WorkspaceContext.tsx#L158-L187) | Sur 66 k fichiers un scan dure plus de 10 s → scans concurrents qui s'empilent, onglet saturé |
| P4 | Deux scans complets par synchro (snapshot + sync) | [WorkspaceContext.tsx:108-109](../../frontend/src/context/WorkspaceContext.tsx#L108-L109) | Temps × 2 |
| P5 | Une requête HTTP par fichier, extraction/OCR **dans** la requête | [workspaces.py:447-467](../../backend/app/routers/workspaces.py#L447-L467) | 66 k requêtes ; timeouts nginx (180 s) sur gros PDF scannés |
| P6 | `async def sync_file` appelle du code **bloquant** (pypdf, Tesseract, S3, SQL) | [workspaces.py:448](../../backend/app/routers/workspaces.py#L448), idem [documents.py:599](../../backend/app/routers/documents.py#L599) | La boucle d'événements uvicorn est gelée pendant chaque OCR : **toute l'API** (chat compris) se fige |
| P7 | Manifest monolithique (1 JSON) et `plan_sync` charge tous les documents du workspace en mémoire | [workspace_sync.py:115-118](../../backend/app/services/workspace_sync.py#L115-L118) | Mémoire et latence O(n) par requête |
| P8 | Aucun index ANN sur `document_chunks.embedding` (ni HNSW ni IVFFlat) ; la recherche lexicale fait un `regex` sur `unaccent(lower(chunk_text))` + un `COUNT` par terme | [migrations.py](../../backend/app/core/migrations.py), [retriever.py:106-126](../../backend/app/rag/retriever.py#L106-L126) | Chaque question = parcours séquentiel de tous les chunks. Acceptable à 1 000 documents, inutilisable à plusieurs millions de chunks |

**Fiabilité / traçabilité**

| # | Problème | Où | Effet |
|---|---|---|---|
| R1 | Résultats métier transformés en HTTP 422 : `ExtractionError` (image sans texte OCR, PDF sans texte même après OCR, classeur vide, fichier corrompu, PDF chiffré), fichier vide, chemin invalide | [workspace_sync.py:179-208](../../backend/app/services/workspace_sync.py#L179-L208), [errors.py:21-22](../../backend/app/services/errors.py#L21-L22) | **Origine des 422 observés.** Une photo de chantier sans texte = 422 |
| R2 | Exceptions non rattrapées → 500 sans contexte : `upload_file` (S3) n'est pas protégé dans `index_file` (il l'est dans l'upload manuel), erreurs SQL, texte contenant `\x00` (PostgreSQL refuse NUL dans `text` ; typique de TXT UTF‑16 décodés en UTF‑8 et de certains PDF) | [workspace_sync.py:210-211](../../backend/app/services/workspace_sync.py#L210-L211), [extract.py:86-87](../../backend/app/rag/extract.py#L86-L87) | **Origine probable des 500** — hypothèse à confirmer dans les logs backend (Phase 0) |
| R3 | Pas de registre par fichier : un fichier non supporté/ignoré/en erreur n'est stocké nulle part (seulement dans l'état React, perdu au rechargement) | — | Impossible de prouver la couverture |
| R4 | `plan_sync` retire (`is_active=false`, `removed`) tout document absent du manifest. Or le manifest exclut aussi les fichiers **illisibles au moment du hash** (ex. Excel ouvert et verrouillé sous Windows), vides, ou passés au‑dessus de 50 Mo | [workspace_sync.py:135-151](../../backend/app/services/workspace_sync.py#L135-L151), [workspaceSync.ts:70-72](../../frontend/src/lib/workspaceSync.ts#L70-L72) | Un fichier ouvert dans Excel peut faire disparaître son document de l'IA. Avec la sélection, « désélectionné » serait aussi confondu avec « supprimé » |
| R5 | Détection incrémentale par SHA‑256 uniquement (pas de pré‑filtre taille+mtime) | [workspace_sync.py:121-126](../../backend/app/services/workspace_sync.py#L121-L126) | Oblige à relire tous les octets pour savoir que rien n'a changé |
| R6 | OCR « best effort » silencieux : une page non lue n'est signalée nulle part | [extract.py:181-192](../../backend/app/rag/extract.py#L181-L192) | Un PDF de 40 pages dont 12 illisibles apparaît comme « indexé » |
| R7 | Le watcher apparie par nom de fichier (`ILIKE '%nom'`) toutes origines confondues et ingère de façon synchrone sans `workspace_id` | [watcher.py:73](../../backend/app/rag/watcher.py#L73), [watcher.py:145](../../backend/app/rag/watcher.py#L145) | Collisions possibles entre deux `rapport.pdf` de dossiers différents |

**Doublons / incohérences**

| # | Constat |
|---|---|
| D1 | **Trois listes d'extensions** divergentes : `SYNC_EXTENSIONS` backend ([workspace_sync.py:44](../../backend/app/services/workspace_sync.py#L44)), `SUPPORTED_EXTENSIONS` extraction ([extract.py:44](../../backend/app/rag/extract.py#L44)), `SYNC_EXTENSIONS` frontend ([workspaceSyncPolicy.js:1](../../frontend/src/lib/workspaceSyncPolicy.js#L1)). **PPTX est extractible mais refusé par la synchro.** |
| D2 | **Trois chemins d'ingestion** : upload manuel, watcher, sync workspace — même extraction mais statuts, déduplication, audit et embeddings (synchrone vs file) différents. |
| D3 | **Deux modèles d'accès aux fichiers locaux** : la synchro (indexation serveur) et le « Local File Agent » (lecture client à la demande via `workspace_index`/`pending_client_action`). Le frontend actuel n'envoie plus `workspace_index` (seuls `localFs.ts` et `types.ts` le référencent) : la branche orchestrateur correspondante ([orchestrator.py:394-418](../../backend/app/agent/orchestrator.py#L394-L418)) est de fait inactive. |
| D4 | Deux limites de taille identiques mais dupliquées (front/back), et `MAX_UPLOAD_BYTES` dans `documents.py`. |
| D5 | Le tableur est aplati en texte « a \| b \| c » pour le RAG ([extract.py:225-241](../../backend/app/rag/extract.py#L225-L241)) *et* relu à la demande par `read_spreadsheet_data` : deux représentations sans lien de version. |

### A.5 Architecture RAG actuelle

```
extract() → ExtractedPage[] (page_no | section, line_offset, text)
  → documents.content (texte complet)  + document_pages (non chevauchantes, FTS + trigram GIN)
  → chunk_text(800 car., overlap 120) → nomic-embed-text (Ollama, lots de 64)
  → document_chunks(embedding vector(768), document_page_id)
Requête : embed(query) → ORDER BY cosine_distance (seq scan) ∪ regex lexicale → score mélangé
          → top_k=5 → contexte LLM (fenêtre 4 096 tokens)
```

Points forts à conserver : filtrage ACL **dans** le SQL (rien hors droits n'est
lu), lien exact chunk → page, recherche exacte séparée du RAG, signal
hors‑périmètre sans fuite de contenu, `grounding.verify_answer`.

Limites : pas d'index ANN (P8) ; pas de préfixes de tâche nomic
(`search_document:` / `search_query:`) dans [embeddings.py](../../backend/app/rag/embeddings.py) — perte de qualité connue pour ce modèle ;
nomic‑embed‑text v1.5 est surtout anglophone (corpus français) ; pas de
version de modèle d'embedding stockée (impossible de ré‑embedder
progressivement) ; métadonnées `sheet`/`slide` absentes (slide = `page_no`,
sheet = `section`) ; fenêtre LLM de 4 096 tokens
([config.py:54](../../backend/app/core/config.py#L54)), trop courte pour des rapports multi‑documents.

Débit d'embedding mesuré par le projet lui‑même : « 50 Mo de CSV ≈ 100 000
chunks ≈ 25 min » ([workspace_sync.py:45-48](../../backend/app/services/workspace_sync.py#L45-L48)), soit **≈ 65 chunks/s**.
→ 1 million de chunks ≈ 4 h 15 de GPU. C'est le vrai goulot pour 66 k fichiers,
d'où l'importance de la sélection, de la déduplication et d'un cache
d'embeddings.

### A.6 Architecture Vision actuelle

- `vision_client.describe_image` est **solide** (validation format/pixels,
  bombe de décompression, JSON normalisé, contenu jamais journalisé).
- Elle n'est utilisée **que** dans le chat, pour une image jointe à un message
  ([chat.py:277-291](../../backend/app/routers/chat.py#L277-L291)), avec un marquage « contexte visuel non vérifié ».
- L'indexation d'images = **OCR Tesseract seul** ; une image sans texte →
  `ExtractionError` → 422 (R1). Aucune image n'est jamais décrite par
  Qwen3‑VL à l'indexation.
- Aucun contrôle de concurrence ni de VRAM côté backend (le `--parallel 1` du
  serveur llama sérialise, mais les requêtes s'accumulent jusqu'au timeout de
  180 s).

### A.7 Architecture Agent actuelle

`run_agent_turn` ([orchestrator.py:371](../../backend/app/agent/orchestrator.py#L371)) est un **routeur déterministe** :

```
intention de recherche → _run_term_search (MCP search_document_text)
fichier nommé          → _run_read_document
mot-clé                → search_keyword
lister                 → list_documents
section                → get_document_section
graphique              → generate_chart / dialogue de clarification
sinon                  → _ground_with_search (RAG) → _ground_with_database → 1 appel LLM
                         → verify_answer (grounding des nombres)
```

Constat important : `chat_completion` **ignore le paramètre `tools`** et renvoie
toujours `tool_calls: None` ([llm_client.py:333-340](../../backend/app/agent/llm_client.py#L333-L340)). La boucle
`for _ in range(MAX_TOOL_ITERATIONS)` ([orchestrator.py:522](../../backend/app/agent/orchestrator.py#L522)) ne fait donc qu'un tour :
il n'y a pas aujourd'hui d'agent au sens « le modèle choisit ses outils ».
C'est un choix défendable (fiabilité, 4 k de contexte) qu'il faut assumer et
étendre, pas remplacer brutalement.

Absents : route Vision sur documents indexés, route statistique sur gros
tableaux (au‑delà de 10 000 lignes), connecteur données industrielles
(InfluxDB / API ORBIT — `search_database` interroge une table finance de
démonstration), génération de rapport multi‑étapes, déclaration de couverture
vérifiable.

### A.8 Flux frontend → backend (actuel)

| Étape | Appel | Volume pour 66 336 fichiers |
|---|---|---|
| Connexion | `showDirectoryPicker` → IndexedDB | 1 |
| Scan | aucun appel serveur, mais `getFile()` × 66 k, deux fois | 0 HTTP / ~132 k accès disque |
| Hash | lecture complète des fichiers supportés | jusqu'à ~9,9 Go lus |
| Plan | `POST /api/workspaces/{id}/sync/plan` | 1 requête de plusieurs Mo |
| Upload | `POST /api/workspaces/{id}/sync/file` | 1 par fichier supporté et modifié (dizaines de milliers) |
| Suivi | `GET /api/workspaces` toutes les 5 s tant que `pending_count > 0` | — |
| Veille | rescan complet toutes les 10 s | permanent |

### A.9 Modèle de données actuel (pertinent)

`workspaces`, `workspace_members`, `documents` (status `indexing|indexed|removed`,
`content_hash`, `relative_path` unique par workspace, `origin`),
`document_pages`, `document_chunks`, `document_access_rules`,
`restricted_folders`, `audit_logs` (immuable).
Manquent : racine source, résultat de scan, règles de sélection, registre
par fichier, travaux, exécutions de synchro, provenance par page.

---

## B. Architecture cible

### B.1 Principes

1. **Quatre phases séparées**, chacune avec son propre état persistant :
   `DISCOVERY` (système de fichiers seul) → `SELECTION` (règles) →
   `INDEXATION` (file de travaux) → `AI ANALYSIS` (à la question).
   Aucun appel LLM/OCR/GPU avant la confirmation de l'utilisateur.
2. **Un fichier = une ligne de registre = exactement un statut**, toujours.
   L'issue d'un fichier est une **donnée**, jamais une erreur HTTP.
3. **La base est la file** (`SELECT … FOR UPDATE SKIP LOCKED`) : pas de
   nouvelle infrastructure ; reprise après redémarrage gratuite.
4. **Ressources bornées par type** : CPU parse, CPU OCR, GPU vision (=1),
   GPU embeddings (lots). L'API ne fait plus aucun travail lourd.
5. **Additif et réversible** : nouvelles tables et colonnes nullable, ancien
   chemin conservé derrière un drapeau jusqu'à parité.
6. `documents` reste l'unité RAG/ACL : tout le code existant (MCP, retriever,
   policy) continue de fonctionner sans modification de contrat.

### B.2 Décision structurante : où lire les fichiers ?

Le backend tourne dans Docker et, par principe documenté, « ne lit jamais le
disque de l'utilisateur ». Deux connecteurs derrière une même interface :

| | **Connecteur `server_root`** (recommandé pour les gros dossiers) | **Connecteur `browser`** (existant, réécrit) |
|---|---|---|
| Cas d'usage | Dossier sur la machine qui héberge ORBIT (cas de `C:\Users\ORBIT\Desktop\projets`) | Dossier sur un autre poste du réseau |
| Accès | Racine déclarée **par l'administrateur** (`.env` + montage Docker `:ro`), jamais un chemin saisi dans l'UI | Handle File System Access accordé par l'utilisateur |
| Découverte | `os.scandir` dans le worker, sans ouvrir les fichiers | Web Worker, envoi par lots de 2 000 entrées |
| Lecture pour indexation | Le worker lit directement (0 requête HTTP par fichier) | Upload groupé des seuls fichiers sélectionnés + modifiés |
| 66 k fichiers | Quelques dizaines de requêtes HTTP au total | ~33 requêtes de découverte + uploads groupés |
| Veille | Re‑scan incrémental planifié / manuel | Manuel, ou planifié avec garde anti‑chevauchement |

Le connecteur serveur **réutilise un mécanisme déjà accepté par le projet**
(le *watch folder* monté dans `backend.volumes`), avec en plus : lecture
seule, liste blanche de racines, refus des liens symboliques/jonctions qui
sortent de la racine.

> Point à mesurer en Phase 0 : le débit de `scandir` à travers un montage
> Docker Desktop sur Windows (couche WSL2) est nettement inférieur au natif.
> Si la découverte de 66 k entrées dépasse ~2 min, prévoir un petit
> *scanner natif* Windows (même code Python, hors conteneur) qui pousse
> l'inventaire par lots — l'interface `SourceConnector` le permet sans
> changer le reste.

### B.3 Vue cible

```
                 ┌───────────────────────── Frontend ─────────────────────────┐
                 │ Assistant 8 étapes · Arbre paresseux virtualisé · Preview  │
                 │ Tableau de bord du registre · « Preuve de lecture »        │
                 └──────────────┬─────────────────────────────────────────────┘
                                │ REST (paginé, par lots)
┌───────────────────────────────▼──────────────────────────────────────────────┐
│ API FastAPI (légère, aucune extraction)                                       │
│  sources · scans · tree · selection · preview · sync-runs · files · proof     │
└───────┬──────────────────────────────────────────────────────┬───────────────┘
        │ INSERT jobs / lit le registre                         │ chat
┌───────▼─────────────── PostgreSQL + pgvector ──────────────┐  │
│ source_roots · scan_runs · source_entries · selection_rules │  │
│ sync_runs · file_records · file_events · ingest_jobs        │  │
│ documents · document_pages · document_chunks · doc_tables   │  │
│ embedding_cache · audit_logs (immuable)                     │  │
└───────▲─────────────────────────────────────────────────────┘  │
        │ SKIP LOCKED                                             │
┌───────┴──────────── Conteneur worker (même image) ──────────┐ ┌▼──────────────────┐
│ scan (1) · classify (CPU) · parse (pool N) · ocr (pool 2)   │ │ Orchestrateur      │
│ vision (1, GPU) · embed (1, lots, GPU) · finalize           │ │ (routeur + outils) │
│ Gouverneur de ressources : RAM, VRAM, chat prioritaire      │ └──┬─────┬─────┬────┘
└──────┬───────────────┬───────────────┬──────────────────────┘    │     │     │
       │               │               │                            │     │     │
   Racine :ro      Ollama embed   Qwen3-VL :8097             Qwen3 :8096  MCP  stats
```

---

## C. Workspace Discovery

### C.1 Contrat

- Entrée : une racine (`source_root`) + éventuellement un sous‑chemin.
- Sortie : `source_entries` (dossiers **et** fichiers) avec : chemin relatif,
  chemin parent, nom, type, extension, taille, mtime, profondeur, identifiant
  de scan. **Aucun fichier n'est ouvert.** Pas de hash, pas de MIME sniffing,
  pas de GPU, pas de LLM.
- Agrégats par dossier calculés en fin de scan, en une requête SQL
  ascendante : `file_count_total`, `size_total`, `ext_histogram` (JSONB),
  `supported_count`, `max_mtime`. C'est ce qui permet d'afficher
  « Client A — 1 240 fichiers » instantanément sans recompter.

### C.2 Algorithme (connecteur serveur)

```
scan_run = INSERT scan_runs(status='running')
pile = [racine]
tampon = []
tant que pile :
    d = pile.pop()
    pour e in os.scandir(d)  (follow_symlinks=False)
        si lien/jonction/reparse point → entrée kind='link', statut IGNORED(reason='symlink'), pas de descente
        si dossier exclu par défaut (node_modules, .git, …) → entrée dossier marquée excluded, pas de descente
        sinon tampon.append(stat minimal) ; si dossier → pile.push
        si len(tampon) >= 5 000 → COPY/UPSERT par lot ; mise à jour progression
UPSERT final ; marquer « vu » (last_seen_scan_id)
Entrées non vues par ce scan → disparues (state='missing'), jamais supprimées
Calcul des agrégats dossiers ; scan_run.status='done'
```

- Erreurs d'accès (`PermissionError`, chemin trop long > 260 car. sans
  préfixe `\\?\`) : entrée enregistrée avec `scan_error`, jamais d'arrêt du scan.
- Mémoire constante (pile + tampon de 5 000), quel que soit le volume.
- Annulable (drapeau `cancel_requested` lu entre deux lots).

### C.3 Algorithme (connecteur navigateur)

- Le `FileSystemDirectoryHandle` est transmis à un **Web Worker dédié**
  (handle sérialisable) : l'onglet reste fluide.
- Même parcours, mêmes champs (taille et `lastModified` via `getFile()`, qui
  ne lit pas le contenu), envoyés par lots de 2 000 :
  `POST /api/sources/{id}/scans/{scan_id}/entries` → ~33 requêtes pour 66 k.
- Plus de SHA‑256 côté client. Le serveur hache ce qu'il reçoit.

### C.4 Veille / re‑scan

- Supprimer le polling 10 s systématique.
- Serveur : re‑scan planifié (ex. toutes les 15 min, réglable) ou bouton
  « Re‑scanner » ; un seul scan à la fois par racine (verrou consultatif
  PostgreSQL). Le re‑scan est un *diff* sur `(size, mtime)`.
- Navigateur : re‑scan manuel ; planifié seulement si la racine fait moins
  de N fichiers (ex. 5 000), avec garde anti‑chevauchement.

---

## D. Folder Selection

### D.1 Représentation : des règles, pas des listes

```
selection_rules(workspace_id, source_root_id, path, mode ∈ {include, exclude}, created_by, created_at)
```

Sémantique : pour un fichier, **la règle de l'ancêtre le plus profond gagne** ;
sans règle → non sélectionné.

| Intention | Règles |
|---|---|
| ☑ Client A | `include "Client A"` |
| ☑ Client C sauf Factures | `include "Client C"`, `exclude "Client C/Factures"` |
| ☑ Client C/Audit + Rapports seulement | `include "Client C/Audit"`, `include "Client C/Rapports"` |
| Tout sélectionner | `include ""` (racine) |
| Tout désélectionner | suppression de toutes les règles |

Avantages : quelques règles au lieu de milliers de chemins ; un nouveau
fichier ajouté dans `Client A/` est automatiquement inclus au re‑scan ; le
calcul est simple en SQL.

### D.2 État tri‑state d'un dossier

`checked` si le dossier est inclus et qu'aucune règle `exclude` n'existe
sous lui ; `unchecked` s'il est exclu et qu'aucune règle `include` n'existe
sous lui ; sinon `indeterminate`. Calculé côté serveur dans la réponse de
l'arbre (les règles sont peu nombreuses), donc le client n'a jamais besoin
de connaître toute l'arborescence.

### D.3 Normalisation des règles

À l'enregistrement, le serveur simplifie : supprime une règle identique à
l'état hérité, fusionne « tous les enfants cochés » en une règle parent
(optionnel). Les chemins sont validés comme aujourd'hui
(`validate_relative_path`) et les motifs `LIKE` sont **échappés** (`%`, `_`
apparaissent dans de vrais noms de dossiers).

### D.4 Versionnement

Chaque modification incrémente `selection_version`. Un `sync_run` fige la
version qu'il exécute : on peut répondre « qu'est‑ce qui était sélectionné
lors de l'analyse du 12/10 ? ». Chaque modification est auditée
(`SELECTION_CHANGED`, règles avant/après).

### D.5 Désélection ≠ suppression

Un fichier désélectionné passe `IGNORED (reason=not_selected)` et son document
est retiré du périmètre IA (`is_active=false`, `status='deselected'`), **sans**
être confondu avec un fichier supprimé du disque (`missing`). Re‑sélectionner
réactive sans retraitement si `(size, mtime, sha256)` n'ont pas changé.

---

## E. Document Pipeline

### E.1 Étapes

```
Sélection confirmée → sync_run
 1. MATERIALIZE   source_entries ∩ règles → file_records (DISCOVERED)          SQL set-based
 2. CLASSIFY      extension + taille + règles → SUPPORTED | UNSUPPORTED | IGNORED   SQL, sans I/O
 3. CHANGE CHECK  (size, mtime) == dernière version traitée → SKIPPED_UNCHANGED    SQL
 4. ENQUEUE       ingest_jobs(kind='parse') pour le reste                     QUEUED
 5. FETCH+HASH    lecture des octets, SHA-256 serveur
                  hash identique → SKIPPED_UNCHANGED (mtime seul a changé)
                  hash déjà indexé ailleurs dans le workspace → DUPLICATE (cf. E.4)
 6. SNIFF         signature réelle (magic bytes) vs extension ; chiffrement ; zip bomb
                  → PASSWORD_PROTECTED | CORRUPTED | parse
 7. PARSE         parseur dédié au format, dans un sous-processus avec timeout
                  par page : texte natif → sinon job OCR → sinon job Vision (si politique)
 8. PERSIST       documents + document_pages (+ provenance par page) + S3
                  statut provisoire : INDEXED / OCR_INDEXED / VISION_INDEXED / PARTIAL
 9. EMBED         chunking → cache d'embeddings → Ollama par lots → document_chunks
10. FINALIZE      vérification des comptes, embedding_state='done', événement
```

### E.2 Parseurs par format

| Format | Parseur | Unité adressable | Particularités |
|---|---|---|---|
| PDF | PyMuPDF (`fitz`, déjà installé) pour le texte, pypdf en secours | page | Ouvrir le PDF **une seule fois** (aujourd'hui `_ocr_pdf_page` le rouvre à chaque page) ; détection `is_encrypted` → PASSWORD_PROTECTED ; page sans texte → OCR de cette page |
| DOCX | python‑docx | section (titres) | Tableaux, en‑têtes/pieds ; images incluses → OCR optionnel |
| DOC | LibreOffice headless → DOCX → python‑docx | section | Conversion dans le worker, timeout, concurrence 1 ; sans LibreOffice → UNSUPPORTED (`legacy_converter_missing`) |
| XLSX / XLS | openpyxl (lecture seule) / xlrd + **moteur tabulaire** (§E.3) | feuille | Le RAG reçoit un résumé + en‑têtes + échantillon, pas 100 000 lignes de texte |
| CSV | `csv` + détection encodage/délimiteur (existant) + moteur tabulaire | feuille unique | Idem |
| PPTX | python‑pptx (existant) | slide | **À ajouter à la synchro** (D1). Notes du présentateur à inclure |
| PPT | LibreOffice headless → PPTX | slide | Idem DOC |
| TXT / MD | décodage avec détection BOM UTF‑8/UTF‑16 puis cp1252 | section / bloc | Suppression des `\x00` (R2) |
| JSON | `json` stdlib → lignes `chemin.vers.cle: valeur` | bloc | Seulement si l'analyse réussit ; JSON invalide → CORRUPTED ; profondeur/taille bornées |
| JPG/JPEG/PNG/WEBP | Pillow (validation) → OCR → Vision selon politique (§F) | image | Plus jamais d'échec « pas de texte » |
| Autres | — | — | UNSUPPORTED, enregistré, jamais envoyé |

Une **source de vérité unique** : un registre Python
`FORMAT_REGISTRY = {ext: FormatSpec(parser, max_bytes, needs_converter, …)}`
exposé par `GET /api/ingest/formats`. Le frontend ne maintient plus sa propre
liste (D1, D4).

### E.3 Moteur tabulaire (Excel/CSV)

À l'indexation, par feuille :
- détection de la ligne d'en‑tête, types de colonnes, nombre de lignes ;
- profil par colonne numérique (count, manquants, min, max, moyenne,
  médiane, écart‑type, quantiles, tendance première→dernière, nombre
  d'anomalies IQR) — le calcul existe déjà dans [stat_engine.py](../../backend/app/agent/stat_engine.py) ;
- stockage dans `document_tables` (JSONB) ; le fichier original reste dans S3.

Pour le RAG : un chunk « fiche de la feuille » (nom, colonnes, unités,
profil) + quelques lignes d'exemple. À la question, la route statistique
recalcule sur les vraies données. Au‑delà de 10 000 lignes, lecture en
flux colonne par colonne (ou, en option, DuckDB embarqué — dépendance
nouvelle, à valider) au lieu de charger toutes les lignes en dictionnaires.
Le LLM ne reçoit que les résultats.

### E.4 Doublons

`DUPLICATE` = même SHA‑256 qu'un fichier déjà indexé **dans le même
workspace**. Attention à la sécurité : les droits dépendent du chemin
(dossiers restreints, règles Director). Un doublon conserve donc **son propre
`Document`** (son chemin, ses ACL) mais réutilise le texte et les vecteurs du
document canonique (copie depuis le cache d'embeddings : 0 seconde de GPU).
Le registre indique `duplicate_of_file_id`.

### E.5 Cache d'embeddings

`embedding_cache(text_sha256, model_id, vector)` : un chunk identique (doublon,
nouvelle version d'un document dont seules quelques pages changent, gabarits
répétés) n'est jamais ré‑embeddé. Avec ~65 chunks/s, c'est le levier de
performance le plus important après la sélection.

---

## F. OCR / Vision

### F.1 Chaîne pour une image

```
Image ── validation Pillow (format réel, pixels ≤ 20 M, bombe) ── KO → CORRUPTED
   │
   ├─ OCR Tesseract fra+eng (CPU, pool OCR) → texte + confiance moyenne (image_to_data)
   │
   ├─ Politique VISION_INDEX_MODE :
   │    off            → OCR seul ; sans texte → PARTIAL(reason='no_text_vision_off')
   │    text_poor      → Vision si texte OCR < seuil (photo, schéma)   ← défaut recommandé
   │    all            → Vision pour toutes les images
   │    on_demand      → rien à l'indexation ; Vision quand une question cible l'image
   │
   └─ Qwen3‑VL (file 'vision', concurrence 1) → JSON normalisé existant
        { description, text[], objects[], measurements[], warnings[], confidence }
        + champ ajouté : anomalies[] (« possibles », jamais affirmées)
```

Stockage :
- `document_pages` : une page `extraction_method='vision'` contenant une
  version texte de l'observation (pour la recherche et le RAG), préfixée
  « Observation automatique non vérifiée ».
- `document_annotations(document_id, page_id, kind='vision', payload JSONB,
  model, model_version, prompt_version, confidence, created_at)` : le JSON
  complet, pour l'affichage et l'agent.

Statut : `OCR_INDEXED` (texte OCR exploitable), `VISION_INDEXED` (description
Vision obtenue), `PARTIAL` (ni l'un ni l'autre, ou Vision en attente) —
jamais `ERROR` ni 422 pour une image sans texte.

### F.2 PDF scannés et PPTX avec images

Par page : texte natif → sinon OCR (dpi 200, confiance mesurée) → si
confiance < seuil et `VISION_INDEX_MODE ∈ {text_poor, all}` → Vision sur le
rendu de la page. Chaque page enregistre sa méthode et sa confiance. Une page
sans aucun texte reste listée (`pages_unreadable`) → le fichier est `PARTIAL`
et la liste des pages manquantes est consultable.

### F.3 Débit et budget

Estimation à mesurer en Phase 0 : Qwen3‑VL‑8B Q4 sur RTX 3060 avec
`--image-min-tokens 1024` → de l'ordre de 10 à 30 s par image. 552 images
(exemple de la demande) ≈ 1,5 à 4,5 h ; 10 000 images ≈ plusieurs jours.
Conséquences :
- `text_poor` par défaut, `on_demand` pour les très gros lots d'images ;
- la preview (§N) affiche le **temps Vision estimé** avant confirmation ;
- la file Vision est priorisable (dossiers choisis d'abord) et suspendable.

---

## G. RAG

### G.1 Ce qui ne change pas

PostgreSQL + pgvector (pas de Qdrant), `document_scope` dans le SQL, lien
chunk → page, recherche exacte sur `document_pages`, `grounding`.

### G.2 Évolutions

1. **Index ANN** : `CREATE INDEX CONCURRENTLY … USING hnsw (embedding
   vector_cosine_ops)` sur `document_chunks`. Le filtrage ACL après un index
   HNSW peut faire perdre des résultats : utiliser les *iterative index scans*
   de pgvector ≥ 0.8 (`SET hnsw.iterative_scan = relaxed_order`) — version de
   pgvector à vérifier dans l'image `pgvector/pgvector:pg16` en Phase 0.
2. **Filtres dénormalisés sur les chunks** : `workspace_id`,
   `confidentiality_rank`, `department`, `is_active` copiés dans
   `document_chunks` (mis à jour avec le document) pour que le filtre se fasse
   sans jointure ; index B‑tree composite.
3. **Lexical indexé** : colonne `tsvector` générée sur `chunk_text`
   (configuration `simple` + `unaccent`) avec index GIN, à la place du regex
   en parcours séquentiel ; fusion vecteur/lexical par *Reciprocal Rank
   Fusion* (robuste, sans calibrage de scores). Les IDF ne sont plus
   recalculés par requête.
4. **Métadonnées** : chaque chunk porte (par jointure ou dénormalisation)
   `workspace, source_root, relative_path, document, page, section, sheet,
   slide, extraction_method, file_mtime, indexed_at, source (sync/upload/watch)`.
   Ajout de `sheet` et `slide` explicites sur `document_pages`.
5. **Version d'embedding** : colonne `embedding_model` sur `document_chunks` ;
   les préfixes nomic (`search_document:` / `search_query:`) et un éventuel
   modèle multilingue (ex. bge‑m3, 1 024 dim) se déploient **dans une nouvelle
   colonne/table**, ré‑embeddée en arrière‑plan, basculée par drapeau, avec
   l'ancienne conservée jusqu'à validation. Aucun changement de dimension en
   place.
6. **Budget de contexte** : avec 4 096 tokens, garder top‑k 5 et des extraits
   courts ; les rapports passent par un *map‑reduce* (résumé par document puis
   synthèse) plutôt que par un contexte géant. Augmenter `--ctx-size` de
   Qwen3 est une recommandation séparée (§M.5), **hors de ce plan**.

---

## H. Agent

### H.1 Routeur à deux étages (on garde le déterministe en premier)

```
Message
 ├─ Étage 1 — règles déterministes existantes (intent.py) : recherche, section, liste, graphique…
 └─ Étage 2 — si rien ne correspond : classification par Qwen3 via complete_json
              schéma contraint : {route ∈ [chat, rag, vision, stats, industrial, report], cibles[], besoin_de_clarification}
              → validation serveur (une route non autorisée pour le rôle est refusée)
```

| Route | Exécution | Moteur |
|---|---|---|
| `chat` | Question générale sans document | Qwen3‑30B |
| `rag` | `search_documents` + sections → réponse → `verify_answer` | pgvector + Qwen3‑30B |
| `vision` | Image jointe → `describe_image` ; image indexée → `document_annotations`, ou Vision à la demande (mode `on_demand`) | Qwen3‑VL |
| `stats` | `document_tables` → `stat_engine` / lecture tabulaire → le LLM ne rédige que l'interprétation | Python |
| `industrial` | Nouvel outil MCP `query_timeseries` (InfluxDB / API ORBIT) **si configuré** ; sinon réponse explicite « source non connectée » | Connecteur |
| `report` | Plan d'étapes fixe : périmètre → collecte (rag/stats/vision) → synthèse par section → assemblage → section « Couverture » | Combinaison, en tâche de fond |

Chaque route reste un outil MCP ou une fonction serveur, **jamais** un accès
direct du LLM à la base : la frontière de sécurité actuelle est conservée.

### H.2 Règle « jamais sans preuve »

- Toute phrase de couverture (« tous les documents », « l'ensemble du
  dossier », « 100 % ») est **générée par le serveur** à partir du registre,
  jamais rédigée par le LLM. Exemple : « Périmètre : 4 580 fichiers
  sélectionnés — 4 312 lus (dont 340 par OCR, 52 par Vision), 120 non
  supportés, 18 protégés par mot de passe, 9 corrompus, 81 partiels. »
- `verify_answer` est étendu : une affirmation de couverture totale dans la
  réponse du LLM est retirée si le registre ne la justifie pas (même mécanisme
  que la suppression d'un nombre non prouvé aujourd'hui).
- Les sources citées renvoient à `file_record` → page → méthode
  d'extraction, ce qui rend chaque citation vérifiable.

---

## I. Database Model

Toutes les tables sont **nouvelles** ou reçoivent des colonnes **nullable** ;
aucune table existante n'est supprimée ni renommée. Migrations dans
[migrations.py](../../backend/app/core/migrations.py) (`IF NOT EXISTS`, sauvegarde automatique avant migration).

```sql
-- Racine déclarée (serveur) ou connectée (navigateur)
source_roots(
  id uuid PK, workspace_id uuid FK, kind text CHECK (kind IN ('server_root','browser')),
  label text,                 -- affiché ; jamais de chemin hôte absolu côté UI
  server_root_key text NULL,  -- clé de la liste blanche admin (connecteur serveur)
  created_by uuid, created_at timestamptz, archived_at timestamptz NULL)

scan_runs(
  id uuid PK, source_root_id uuid FK, status text, started_at, finished_at,
  entries_seen int, dirs_seen int, errors int, cancel_requested bool, error text NULL)

-- Inventaire (découverte) : dossiers ET fichiers
source_entries(
  id bigserial PK, source_root_id uuid FK, relative_path text, parent_path text,
  name text, kind text CHECK (kind IN ('dir','file','link')), extension text,
  size bigint, mtime timestamptz, depth int,
  state text CHECK (state IN ('present','missing')), last_seen_scan_id uuid,
  scan_error text NULL,
  -- agrégats (dossiers uniquement)
  file_count_total int, size_total bigint, supported_count int, ext_histogram jsonb,
  UNIQUE (source_root_id, relative_path))
  INDEX (source_root_id, parent_path)                       -- arbre paresseux
  INDEX (source_root_id, relative_path text_pattern_ops)    -- préfixes LIKE

selection_rules(
  id uuid PK, workspace_id uuid, source_root_id uuid, path text,
  mode text CHECK (mode IN ('include','exclude')), created_by uuid, created_at,
  UNIQUE (source_root_id, path))
workspaces.selection_version int DEFAULT 0      -- colonne ajoutée

sync_runs(
  id uuid PK, workspace_id uuid, source_root_id uuid, selection_version int,
  requested_by uuid, status text, started_at, finished_at,
  counts jsonb,               -- instantané Σ par statut à la fin
  invariant_ok bool)

-- LE REGISTRE : une ligne par fichier connu de la racine
file_records(
  id uuid PK, workspace_id uuid, source_root_id uuid, source_entry_id bigint,
  relative_path text, size bigint, mtime timestamptz, sha256 char(64) NULL,
  status text NOT NULL CHECK (status IN (
    'DISCOVERED','SUPPORTED','QUEUED','PROCESSING',            -- transitoires
    'INDEXED','OCR_INDEXED','VISION_INDEXED','PARTIAL',
    'UNSUPPORTED','CORRUPTED','PASSWORD_PROTECTED','DUPLICATE',
    'IGNORED','ERROR','SKIPPED_UNCHANGED')),
  reason text NULL,           -- not_selected, excluded_dir, too_large, empty, symlink, timeout…
  parser text NULL, parser_version text NULL, pipeline_version int,
  document_id uuid NULL FK documents, duplicate_of uuid NULL FK file_records,
  pages_total int, pages_text int, pages_ocr int, pages_vision int, pages_unreadable int[],
  chars_extracted int, chunk_count int, embedded_chunk_count int,
  embedding_state text CHECK (embedding_state IN ('n/a','pending','done','failed')),
  attempts int DEFAULT 0, last_error_code text NULL, last_error_message text NULL,
  trace_id uuid NULL, last_sync_run_id uuid, processed_at timestamptz, updated_at timestamptz,
  UNIQUE (source_root_id, relative_path))
  INDEX (workspace_id, status)

-- Historique ajouté seulement (append-only), pas audit_logs
file_events(id bigserial PK, file_record_id uuid, sync_run_id uuid, at timestamptz,
  from_status text, to_status text, detail jsonb)

-- File de travaux
ingest_jobs(
  id bigserial PK, kind text CHECK (kind IN ('scan','parse','ocr','vision','embed','finalize')),
  file_record_id uuid NULL, sync_run_id uuid NULL, priority int DEFAULT 100,
  state text CHECK (state IN ('queued','running','done','failed','cancelled')),
  attempts int DEFAULT 0, max_attempts int DEFAULT 3, run_after timestamptz,
  locked_by text NULL, locked_until timestamptz NULL, payload jsonb, last_error text NULL)
  INDEX (kind, state, priority, run_after)

-- Provenance par page (colonnes ajoutées)
document_pages.extraction_method text NULL   -- native | ocr | vision | converted
document_pages.ocr_confidence real NULL
document_pages.sheet text NULL, document_pages.slide int NULL

document_annotations(id uuid PK, document_id uuid, document_page_id uuid NULL, kind text,
  payload jsonb, model text, model_version text, prompt_version text, confidence real NULL, created_at)

document_tables(id uuid PK, document_id uuid, sheet text, header jsonb, n_rows int, n_cols int,
  column_profiles jsonb, created_at)

embedding_cache(text_sha256 char(64), model_id text, vector vector(768), PRIMARY KEY (text_sha256, model_id))

document_chunks : + workspace_id, confidentiality_rank, department, is_active, embedding_model (nullable, remplis par backfill)
```

**Invariant de couverture** (exigence `UNCLASSIFIED = 0`) :
- garanti **structurellement** : `status NOT NULL` + `CHECK` sur une liste
  fermée → un fichier sans statut ne peut pas exister ;
- vérifié **arithmétiquement** à la fin de chaque `sync_run` :
  `COUNT(*) = Σ COUNT(*) GROUP BY status` sur le périmètre, et
  `COUNT(file_records) = COUNT(source_entries WHERE kind='file' AND state='present')` ;
- « Ready for AI » exige en plus : **0 fichier dans un état transitoire**
  (`DISCOVERED/SUPPORTED/QUEUED/PROCESSING`) et 0 `embedding_state='pending'`.

> Ajout par rapport à la liste demandée : `QUEUED` et `PROCESSING`, états
> transitoires indispensables pour qu'un fichier en cours de traitement ait
> lui aussi un statut. Les 13 statuts demandés restent les seuls statuts finaux.

**Réponse à « ORBIT a‑t‑il réellement lu ce document ? »** :
`GET /api/workspaces/{id}/files/{file_id}/proof` renvoie : statut, SHA‑256 des
octets **lus par le serveur**, taille et mtime au moment de la lecture,
parseur et version, pages totales / lues par texte / OCR / Vision / illisibles
(numéros), caractères extraits, chunks créés et embeddés, modèle d'embedding,
horodatages, historique (`file_events`), identifiant de `sync_run`. Si le
SHA‑256 actuel du fichier diffère de celui stocké, la réponse le dit
(« la version lue n'est plus la version actuelle »).

---

## J. Queue / Workers

### J.1 File PostgreSQL

Prise d'un travail :

```sql
UPDATE ingest_jobs SET state='running', locked_by=$worker, locked_until=now()+interval '10 min',
                       attempts=attempts+1
WHERE id = (SELECT id FROM ingest_jobs
            WHERE kind=$kind AND state='queued' AND run_after<=now()
            ORDER BY priority, id
            FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```

- Bail (`locked_until`) prolongé par battement de cœur ; un worker mort →
  bail expiré → travail repris.
- Réessais avec délai exponentiel ; au‑delà de `max_attempts` → fichier
  `ERROR` avec la dernière cause (jamais de boucle infinie).
- Idempotence : chaque étape vérifie `(file_record.sha256, pipeline_version)`
  avant d'écrire (même principe que le `still_wanted()` actuel de l'embedding
  worker).

### J.2 Conteneur `worker`

Même image que le backend, commande différente (`python -m app.worker`), sans
port exposé. Pools :

| Pool | Concurrence initiale | Ressource | Remarque |
|---|---|---|---|
| `scan` | 1 par racine | disque | verrou consultatif par racine |
| `parse` | `min(4, cœurs − 2)` processus | CPU / RAM | sous‑processus avec timeout (ex. 120 s) et limite mémoire ; protège contre un PDF pathologique |
| `ocr` | 2 | CPU | Tesseract est mono‑thread par appel |
| `vision` | **`VISION_CONCURRENCY=1`** | GPU | gouverneur (§J.3) |
| `embed` | 1, lots de 64 | GPU (Ollama) | cache d'embeddings d'abord |
| `finalize` | 1 | DB | comptes, invariant, événements |

L'`embedding_worker` actuel quitte le processus API (il y reste en secours
derrière un drapeau pendant la transition). L'API ne fait plus que des
lectures/écritures légères : le chat n'est plus jamais bloqué par une
indexation (P6).

### J.3 Gouverneur de ressources

- **RAM** (32 Go, dont ~18 Go pour Qwen3‑30B Q4 largement en RAM avec
  `LLM_GPU_LAYERS=1`) : chaque pool réserve un budget ; un fichier > seuil
  est traité seul ; `psutil` dans le worker suspend la prise de nouveaux
  travaux sous un seuil de RAM libre.
- **VRAM** (12 Go) : lecture via NVML/`nvidia-smi` (soit donner l'accès GPU au
  conteneur worker en lecture seule, soit une petite sonde côté hôte lancée
  par `start-orbit-ai.ps1` exposant `memory.used/total`). Vision suspendue si
  VRAM libre < seuil.
- **Priorité au chat** : pendant une requête de chat active (compteur en
  base ou verrou consultatif), les pools `vision` et `embed` terminent leur
  lot en cours puis attendent. L'utilisateur interactif passe toujours avant
  l'indexation de fond.
- Le scanner et le classifieur **n'utilisent jamais le GPU** (ni OCR, ni
  modèle).

---

## K. Error Handling

### K.1 Taxonomie : l'issue d'un fichier n'est pas une erreur HTTP

| Situation | Aujourd'hui | Cible |
|---|---|---|
| Extension non supportée | filtrée côté client, sinon 422 | `UNSUPPORTED`, jamais envoyé |
| Image sans texte | 422 | `OCR_INDEXED` / `VISION_INDEXED` / `PARTIAL` |
| PDF sans texte après OCR | 422 | `PARTIAL` (pages illisibles listées) |
| PDF / Office chiffré | 422 (message générique) | `PASSWORD_PROTECTED` |
| Fichier corrompu / signature ≠ extension | 422 | `CORRUPTED` (cause conservée) |
| Fichier vide | 422 | `IGNORED (reason=empty)` |
| Trop volumineux | ignoré client, puis retiré à tort (R4) | `IGNORED (reason=too_large)`, document existant **conservé** |
| Fichier verrouillé (Excel ouvert) | retiré à tort (R4) | réessai différé ; si persistant `ERROR (reason=locked)`, document existant conservé |
| Timeout du parseur | 504 nginx | `ERROR (reason=timeout)` après réessais |
| S3 indisponible | 500 | travail réessayé ; `ERROR (reason=storage_unavailable)` si persistant |
| Texte avec `\x00` | 500 probable | nettoyé à l'extraction |
| Exception inattendue | 500 sans contexte | `ERROR` + `trace_id` + journal structuré |

Les codes HTTP restent réservés aux erreurs **de requête** : 401/403 (droits),
404 (ressource), 409 (sync déjà en cours), 413 (lot trop gros), 422 (corps de
requête mal formé). Dans le connecteur navigateur, l'upload d'un lot répond
**200** avec le statut de chaque fichier.

### K.2 Traçabilité des 500

Tout échec non prévu produit une ligne de log JSON et met à jour le registre :

```json
{"event":"file_processing_failed","trace_id":"…","workspace_id":"…","sync_run_id":"…",
 "relative_path":"Client C/Audit/rapport.pdf","stage":"parse","parser":"pymupdf",
 "parser_version":"1.24.x","exception":"RuntimeError","message":"…","cause":"…",
 "attempt":2,"elapsed_ms":4312}
```

Le même `trace_id` est stocké dans `file_records.trace_id` et affiché dans
l'UI (« copier l'identifiant ») : une erreur signalée par un utilisateur se
retrouve en une recherche dans les logs. Un middleware ajoute aussi un
`trace_id` à toute réponse 5xx de l'API.

### K.3 Audit

`audit_logs` reste **en insertion seule** (aucune modification de ligne
existante). Pour éviter 66 000 lignes d'audit par synchronisation : un
événement par action humaine et par exécution (`SOURCE_ROOT_ADDED`,
`SELECTION_CHANGED`, `SYNC_RUN_STARTED`, `SYNC_RUN_FINISHED` avec les comptes
par statut et `invariant_ok`) ; le détail par fichier vit dans `file_records`
et `file_events`. Les retraits de documents et les erreurs d'accès restent
audités individuellement comme aujourd'hui.

---

## L. Security

1. **Racines serveur en liste blanche** : déclarées dans la configuration
   (`SOURCE_ROOTS=projets:/sources/projets`), montées en `:ro`. L'UI ne propose
   que ces clés ; aucun chemin absolu n'est accepté d'un client ni renvoyé au
   client.
2. **Confinement des chemins** : `realpath(candidate)` doit rester sous
   `realpath(root)` ; `follow_symlinks=False` ; liens, jonctions NTFS et
   *reparse points* non suivis (`IGNORED reason=symlink`). Validation
   existante `validate_relative_path` conservée.
3. **Droits** : seul le propriétaire du workspace (règle actuelle
   `can_manage_workspace`) crée une racine, modifie la sélection et lance une
   synchro. Ajouter une racine serveur nécessite en plus le rôle Director (ou
   admin), car elle expose un dossier de la machine.
4. **ACL inchangées** : `document_scope`, dossiers restreints et règles
   Director s'appliquent aux documents issus de la V2 exactement comme
   aujourd'hui (même table `documents`, même `relative_path`). Les doublons
   gardent leur propre document pour ne pas contourner une restriction de
   dossier (§E.4).
5. **Classification par dossier** (optionnelle) : pouvoir dire « Client C/
   Factures → FINANCE/CONFIDENTIAL » au moment de la sélection, toujours
   plafonné aux droits de celui qui sélectionne (règle existante de
   `index_file`).
6. **Fichiers hostiles** : limite de taille décompressée pour DOCX/XLSX/PPTX
   (zip), `MAX_IMAGE_PIXELS`, parseurs en sous‑processus avec timeout et
   mémoire bornée, LibreOffice sans macros et sans réseau.
7. **Injection de prompt par les documents** : le contenu extrait est une
   donnée ; l'observation Vision reste marquée « non vérifiée » ; les routes de
   l'agent sont validées côté serveur, le LLM ne peut pas élargir son périmètre.
8. **Tests** : conformément à `CLAUDE.md`, vérifier `DATABASE_URL` avant toute
   exécution de tests ; utiliser une arborescence synthétique générée dans un
   dossier temporaire, **jamais** le dossier réel de 66 336 fichiers.

---

## M. Performance

### M.1 Ordres de grandeur pour 66 336 fichiers / 9 453 dossiers / 9,9 Go

| Phase | Actuel | Cible (à confirmer par les mesures de Phase 0) |
|---|---|---|
| Découverte | 2 scans navigateur + lecture complète des octets à chaque cycle | 1 scan des métadonnées : secondes en natif, à mesurer à travers le montage Docker |
| Requêtes HTTP | 1 plan + 1 par fichier modifié | ~10–50 au total (serveur) ; ~33 + lots d'upload (navigateur) |
| Re‑synchro sans changement | relecture de 9,9 Go | comparaison `(size, mtime)` en SQL, 0 octet lu |
| Recherche vectorielle | parcours séquentiel de tous les chunks | HNSW, de l'ordre de la dizaine de ms |
| Embeddings | ~65 chunks/s, sans cache | même débit GPU, mais cache + doublons + sélection réduisent le volume |
| Vision | non utilisée | 1 image à la fois, ~10–30 s/image (à mesurer) |

### M.2 Navigateur

Arbre **paresseux** (enfants chargés à l'ouverture, `GET /tree?parent=…&cursor=…`,
pages de 500) et **virtualisé** (un dossier de 5 000 enfants n'en rend que
~30) ; aucun état global de 66 k entrées en mémoire. Découverte navigateur
dans un Web Worker.

### M.3 PostgreSQL

Insertion de l'inventaire par `COPY`/lots de 5 000 ; mise à jour de la
sélection en une poignée d'`UPDATE` ensemblistes (préfixes indexés en
`text_pattern_ops`) ; agrégats calculés une fois par scan ; jamais de
`SELECT` sans pagination dans l'UI ; `VACUUM/ANALYZE` après un gros scan.

### M.4 Mesures permanentes

Durée par étape et par format, files d'attente, débit embeddings, temps
Vision, RAM/VRAM : exposés sur `GET /api/sync-runs/{id}` et dans l'UI
(estimation du temps restant fondée sur les débits observés, pas sur une
constante).

### M.5 Recommandations hors périmètre (pour mémoire, rien n'est modifié ici)

- `LLM_GPU_LAYERS=1` ([start-orbit-ai.ps1:35](../../start-orbit-ai.ps1#L35)) laisse Qwen3‑30B presque
  entièrement sur CPU. Pour un MoE, placer l'attention sur GPU et les experts
  sur CPU (`--n-cpu-moe` / `--override-tensor` de llama.cpp) accélère
  fortement la génération à VRAM modérée. À évaluer avec le budget VRAM de
  Qwen3‑VL (~6 Go) et d'Ollama.
- `--ctx-size 4096` limite le RAG et les rapports ; 16 k avec cache KV
  quantifié serait un bon compromis.
Ces deux points touchent le serveur 8096 : à décider séparément.

---

## N. UX

### N.1 Parcours en 8 étapes

```
1 Racine   2 Explorer   3 Sélectionner   4 Aperçu   5 Confirmer   6 Synchroniser   7 Indexer   8 Prêt pour l'IA
```

**Étape 1 — Racine** : liste des racines serveur autorisées (« projets —
machine ORBIT ») ou « Choisir un dossier sur ce poste » (navigateur).
Lancement immédiat de la découverte, barre de progression « 12 400 éléments
découverts… », annulable.

**Étapes 2–3 — Explorer / Sélectionner** :

```
┌──────────────────────────────────────────────────────────────┐
│ 📁 projets                  Découverte terminée · il y a 2 min │
│ [Tout sélectionner] [Tout désélectionner]   🔍 Filtrer…        │
│                                                              │
│ ☑ ▸ Client A                     1 240 fichiers   410 Mo     │
│ ☐ ▸ Client B                       850 fichiers   220 Mo     │
│ ◪ ▾ Client C                     2 430 fichiers   980 Mo     │
│     ☑ ▸ Audit                    1 200 fichiers   500 Mo     │
│     ☐ ▸ Factures                   300 fichiers    80 Mo     │
│     ☑ ▸ Rapports                   930 fichiers   400 Mo     │
│ ⊘   node_modules (exclu par défaut)                          │
│                                                              │
│ Sélection : 3 chemins · 3 370 fichiers · 1,3 Go              │
│ [Annuler]                               [Voir l'aperçu →]    │
└──────────────────────────────────────────────────────────────┘
```
`◪` = sélection partielle. Les compteurs viennent des agrégats (pas de calcul
côté client). La sélection est enregistrée (brouillon) à chaque clic.

**Étape 4 — Aperçu** (`POST /selection/preview`, calculé en SQL) :

```
Chemins sélectionnés : 3          Fichiers : 3 370       Taille : 1,3 Go
Par format :  PDF 820 · DOCX 150 · XLSX 97 · PPTX 30 · JPG 340 · PNG 212 · CSV 42
Seront traités : 2 190     Inchangés (déjà indexés) : 1 060
Non supportés : 120 (voir la liste)     Ignorés : 0 vides · 3 trop volumineux
Estimation : OCR probable ~180 fichiers · Vision ~150 images (~1 h) · embeddings ~25 min
```

**Étape 5 — Confirmer** : rappel de la classification par défaut et du mode
Vision ; bouton « Analyser la sélection ».

**Étapes 6–7 — Synchroniser / Indexer** : tableau de bord en direct par
statut (compteurs qui se remplissent), file par type (parse, OCR, Vision,
embeddings), erreurs cliquables avec cause et `trace_id`, bouton
« Suspendre / Reprendre ». Le chat reste utilisable : les documents déjà
indexés sont interrogeables immédiatement.

**Étape 8 — Prêt pour l'IA** : affiché seulement si l'invariant est vérifié
et qu'aucun fichier n'est en état transitoire :

```
✅ 3 370 / 3 370 fichiers classés (0 non classé)
   INDEXED 2 731 · OCR_INDEXED 180 · VISION_INDEXED 150 · SKIPPED_UNCHANGED 1 060*
   PARTIAL 12 · UNSUPPORTED 120 · PASSWORD_PROTECTED 4 · CORRUPTED 2 · ERROR 0
[Exporter le rapport CSV]   [Voir les fichiers non lus]
```
(*la somme exacte est garantie par le serveur ; chiffres illustratifs)

### N.2 Fiche fichier « Preuve de lecture »

Depuis la liste des documents et depuis chaque source citée dans le chat :
statut, méthode par page, pages illisibles, empreinte SHA‑256, date de
lecture, parseur, chunks, historique. Bouton « Retraiter ce fichier ».

### N.3 Écrans existants

`FoldersPanel` (partage/restriction des dossiers indexés) est conservé ;
l'arbre de sélection est un composant distinct (il travaille sur
l'inventaire, pas sur les documents indexés). Le bandeau de synchro de
`DocumentsPage` affiche l'état du dernier `sync_run`.

---

## O. Migration Plan

Chaque phase est livrable seule, derrière un drapeau, avec ses tests, et ne
casse pas la précédente. Les noms de drapeaux sont indicatifs.

| Phase | Contenu | Drapeau | Critère de sortie |
|---|---|---|---|
| **0 — Mesurer et stabiliser** | Logs structurés + `trace_id` sur `/sync/file` (identifier les vrais 422/500 dans les logs actuels) ; extraction déplacée hors de la boucle d'événements (`def` ou `run_in_threadpool`) ; nettoyage `\x00` ; protection de `upload_file` ; garde anti‑chevauchement du polling et polling désactivé au‑delà de N fichiers ; R4 (ne plus retirer un document dont le fichier est illisible/ignoré) ; PPTX ajouté. Bancs d'essai : scan via montage Docker, débit OCR/Vision/embeddings sur un échantillon, version de pgvector. | — | Plus de gel de l'API ; chaque 500 a une cause identifiée ; chiffres de débit réels |
| **1 — Modèle de données + découverte** | Tables `source_roots`, `scan_runs`, `source_entries` ; connecteur serveur (montage `:ro` + liste blanche) ; API `tree` paginée ; découverte navigateur en Web Worker par lots | `SYNC_V2_DISCOVERY` | Découverte de 100 000 fichiers synthétiques sans dépassement mémoire, < objectif fixé en Phase 0 |
| **2 — Sélection + aperçu** | `selection_rules`, tri‑state, aperçu SQL, UI étapes 1–5 | `SYNC_V2_SELECTION` | Sélection « Client A + Client C/Audit » exacte, testée sur l'arbre synthétique |
| **3 — Registre + file + worker** | `file_records`, `file_events`, `ingest_jobs`, `sync_runs` ; conteneur `worker` ; `extract.py` réutilisé par le pool `parse` ; embedding worker déplacé ; invariant vérifié en fin d'exécution ; UI étapes 6–8 | `SYNC_V2_PIPELINE` | Invariant vrai sur 100 % des exécutions de test ; zéro 422/500 pour des issues de fichier |
| **4 — Intelligence documentaire** | Registre de formats unique ; détection chiffrement/corruption ; PyMuPDF ; JSON ; DOC/PPT via LibreOffice ; moteur tabulaire + `document_tables` ; provenance par page ; doublons + cache d'embeddings | `INGEST_V2_FORMATS` | Jeu de fichiers de test par statut (un fichier pour chacun des 13 statuts) |
| **5 — Vision à l'indexation** | Politique `VISION_INDEX_MODE`, pool `vision` (=1), gouverneur RAM/VRAM, priorité au chat, `document_annotations` | `VISION_INDEX_MODE=off` par défaut | Aucune dégradation du temps de réponse du chat pendant l'indexation Vision |
| **6 — RAG à l'échelle** | Index HNSW (`CONCURRENTLY`), filtres dénormalisés sur les chunks, tsvector + RRF, colonne `embedding_model` ; puis, séparément, ré‑embedding versionné (préfixes nomic / modèle multilingue) | `RAG_V2_INDEX`, `EMBEDDING_MODEL_V2` | Rappel ≥ ancien sur un jeu de questions de référence ; latence p95 mesurée |
| **7 — Agent** | Routeur étage 2 (`complete_json`), routes stats/vision/report, outil `query_timeseries` si une source industrielle est fournie, phrase de couverture serveur, endpoint `proof` | `AGENT_V2_ROUTER` | Aucune affirmation de couverture non justifiée sur le jeu de test |
| **8 — Convergence** | Upload manuel et watcher passent par le même pipeline ; ancien `/sync/plan` + `/sync/file` retirés seulement après une période de double fonctionnement et accord explicite | — | Parité fonctionnelle constatée |

Données existantes : un *backfill* crée les `file_records` des documents déjà
synchronisés (`origin='sync'`) avec statut `INDEXED` et leur `content_hash`
comme SHA‑256 ; aucun document existant n'est modifié ni ré‑embeddé.

Le dossier `C:\Users\ORBIT\Desktop\projets` ne sera synchronisé qu'à la fin
de la Phase 3, sur décision explicite, en commençant par une petite sélection.

---

## P. Rollback Plan

| Niveau | Mécanisme |
|---|---|
| Fonctionnel | Chaque phase est derrière un drapeau ; le désactiver rétablit le flux précédent (ancien `/sync/plan` + `/sync/file`, embedding worker dans l'API) sans redéploiement de schéma. |
| Schéma | Migrations **uniquement additives** (tables nouvelles, colonnes nullable, index créés `CONCURRENTLY`). L'ancien code ignore ces objets. Un index HNSW se supprime sans effet sur les données. Sauvegarde `pg_dump` automatique avant chaque migration (déjà en place dans `run_privileged_migration`). |
| Données | Aucun `UPDATE` destructif sur `documents`/`document_chunks` existants ; le ré‑embedding se fait dans une colonne/table nouvelle, l'ancienne reste la source tant que le drapeau n'est pas basculé. `audit_logs` n'est jamais modifié. |
| Workers | Arrêter le conteneur `worker` met la file en pause sans perte (les travaux restent `queued` ou sont repris à l'expiration du bail). |
| Modèles | Aucun changement sur Qwen3‑30B (8096), Qwen3‑VL (8097) ni sur le modèle BF16 source. `VISION_INDEX_MODE=off` coupe toute utilisation de la Vision à l'indexation. |
| Connecteur serveur | Retirer la racine de la liste blanche et le montage `:ro` ; les documents déjà indexés restent (retirables via la sélection, avec audit). |

Critère de retour arrière, par phase : régression du temps de réponse du
chat, invariant de couverture faux, ou taux d'`ERROR` supérieur au seuil
défini en Phase 0.

---

## Annexe — Décisions à valider avant de coder

1. **Connecteur serveur** pour `projets` (montage Docker en lecture seule
   d'une racine déclarée par l'admin) : oui / non. Recommandation : oui.
2. **Mode Vision par défaut** : `text_poor` (recommandé) ou `on_demand`.
3. **LibreOffice headless** dans l'image worker pour DOC/PPT (≈ +300 Mo
   d'image) : oui / plus tard.
4. **Dépendances nouvelles** acceptables : `psutil` (gouverneur), éventuellement
   DuckDB (gros tableaux). Aucune nouvelle base de données.
5. **Ajout des états transitoires** `QUEUED` et `PROCESSING` à la liste des
   statuts.
6. **Classification par dossier** au moment de la sélection : dans le
   périmètre de la Phase 2, ou plus tard.
