# Orbitia — Private Enterprise AI Agent (MVP)

Application complète (pas une maquette) démontrant un assistant IA d'entreprise qui recherche dans des documents et données internes **en respectant strictement les permissions RBAC/ABAC de chaque utilisateur**, via un vrai serveur **MCP**, un vrai pipeline **RAG** (pgvector) et un **LLM local (Ollama)** qui ne quitte jamais le réseau privé.

Voir la note d'architecture complète pour le raisonnement détaillé (corrections apportées à l'idée initiale, comparaison des 3 architectures de confidentialité, modèle de sécurité, plan de développement).

## Démarrage rapide

Prérequis : Docker Desktop.

```bash
docker compose up -d --build
docker compose run --rm seed
```

Puis ouvrez **http://localhost:8081**.

Le premier démarrage télécharge l'image `ollama/ollama` (~9 Go, inclut les libs CUDA) puis les modèles (`llama3.2:1b` + `nomic-embed-text`, ~1,6 Go) via le service `ollama-init`. Tant que ça n'est pas terminé, l'app fonctionne déjà (login, documents, RBAC, audit) mais le chat répond "service momentanément indisponible" plutôt que d'halluciner ou de bloquer.

**Vous avez déjà Ollama qui tourne nativement sur votre machine ?** Sautez le téléchargement :

```bash
ollama pull nomic-embed-text   # si pas déjà présent
docker compose -f docker-compose.yml -f docker-compose.native-ollama.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.native-ollama.yml run --rm seed
```

Ajustez les noms de modèles dans `docker-compose.native-ollama.yml` selon ce que `ollama list` montre déjà chez vous.

## Déployer sur un serveur (au-delà du laptop local)

Le socle ne change pas — c'est toujours `docker compose up`. Ce qui change, c'est qu'un serveur peut être atteint par d'autres personnes, donc les secrets par défaut (bons pour du localhost) ne suffisent plus.

**1. Dimensionner la machine.** Minimum confortable : 4 vCPU, 8 Go RAM, 20 Go disque. Le plus gourmand est Ollama (inférence LLM) ; sans GPU ça tourne mais plus lentement — largement suffisant pour un test/une démo.

**2. Installer Docker + Docker Compose** sur le serveur (Docker Engine sur Linux, pas besoin de Docker Desktop).

**3. Transférer le projet** (`git clone`/`git push` vers le serveur, ou `rsync`/`scp` du dossier).

**4. Générer de vrais secrets** — ne jamais garder les valeurs par défaut dès qu'un serveur est accessible par quelqu'un d'autre que vous :

```bash
cp .env.example .env
# éditer .env : POSTGRES_PASSWORD, JWT_SECRET, INTERNAL_AUTH_SECRET, MINIO_ROOT_PASSWORD
# générer une valeur aléatoire pour chaque secret :
openssl rand -hex 32
```

**5. Exposer le frontend.** Par défaut, tout (y compris le frontend) n'écoute que sur `127.0.0.1` de l'hôte — volontairement injoignable de l'extérieur. Pour un serveur qu'on veut atteindre depuis un autre poste, dans `.env` :

```bash
FRONTEND_BIND=0.0.0.0
FRONTEND_PORT=8081   # ou 80/443 si vous mettez un reverse proxy TLS devant
```

Postgres, MinIO, Ollama, le backend et le serveur MCP restent, eux, systématiquement bindés sur `127.0.0.1` — ce n'est pas configurable, c'est le principe même de l'architecture (seul le frontend/nginx est un point d'entrée public).

**6. HTTPS si le serveur est exposé au-delà d'un LAN de confiance.** Mettez un reverse proxy (Caddy est le plus simple : TLS Let's Encrypt automatique) devant `FRONTEND_PORT`, ou passez par un tunnel/VPN si l'accès doit rester privé (cohérent avec l'esprit du projet).

**7. Lancer.**

```bash
docker compose up -d --build
docker compose run --rm seed
```

**8. Changer les mots de passe de démo** si le serveur est partagé au-delà d'un test solo — `Demo1234!` est documenté dans ce README, donc public. Modifiable par le Directeur depuis **Administration → Utilisateurs**, ou en resemant avec d'autres valeurs dans `backend/app/seed.py`.

## Comptes de démonstration

Mot de passe commun : `Demo1234!`

| Identifiant | Rôle | Départements accessibles | Niveau max. | Outils MCP |
|---|---|---|---|---|
| `directeur` | Directeur | HR, FINANCE, TECH, GENERAL, EXEC | SECRET | les 4 |
| `rh` | RH | HR, GENERAL | CONFIDENTIAL | search_documents, get_document, get_company_information |
| `comptable` | Comptable | FINANCE, GENERAL | CONFIDENTIAL | + search_database |
| `developpeur` | Développeur | TECH, GENERAL | INTERNAL | search_documents, get_document, get_company_information |
| `employe` | Employé | GENERAL | INTERNAL | search_documents, get_document, get_company_information |

## Scénario de démo (celui du brief)

1. Connectez-vous en `rh`. Chat → *« Trouve-moi la politique de congés »* → réponse sourcée, extraits du document RH/GENERAL cités.
2. Même session → *« Donne-moi le salaire des employés »* → l'agent explique qu'il n'y a pas accès. Le refus vient du **serveur** (la requête RAG et la recherche financière ne contiennent structurellement aucun document/enregistrement `FINANCE`/`SECRET` pour ce rôle - le modèle ne les a jamais reçus), pas d'une instruction dans le prompt.
3. Onglet **Documents** (toujours en `rh`) → la liste est déjà filtrée. Essayez de charger un document `FINANCE` par son UUID (copiez-le en vous connectant en `directeur`) via l'encart "tester un accès direct" → `403`.
4. Connectez-vous en `directeur` → **Journal d'audit** : vous verrez la ligne `DENY` correspondant à la tentative de l'étape 3, avec la raison exacte (`role HR has no access to department FINANCE`).
5. **Permissions** (Directeur uniquement) → matrice RBAC/ABAC telle qu'appliquée par le backend ; **Utilisateurs** → comptes, accès personnalisés par employé ; **Alertes** → notifications en temps réel des tentatives hors périmètre.

## Ce qui est réellement implémenté (pas juste documenté)

- **Auth** : JWT maison (`app/core/security.py`), interface volontairement compatible OIDC/Keycloak plus tard (un seul point d'entrée : `get_current_user`).
- **RBAC/ABAC** : `app/policy/rules.py` + `app/policy/engine.py` — une seule source de vérité, appelée indépendamment par l'API documents, le retriever RAG et **chaque tool MCP** (défense en profondeur réelle, pas juste documentée). Un Directeur peut en plus accorder des accès ponctuels par employé (départements/niveau/outils supplémentaires) sans changer son rôle.
- **RAG à permissions** : `app/rag/retriever.py` filtre `department`/`confidentiality_rank` **dans la requête SQL pgvector elle-même**, avant tout calcul de similarité - jamais un post-filtrage après que le LLM ait vu les données. Détecte aussi le cas silencieux (une info existe mais hors périmètre) via un second signal métadonnées-only, pour déclencher une alerte plutôt qu'un simple "rien trouvé".
- **Serveur MCP réel** : SDK officiel `mcp` (FastMCP, transport streamable-HTTP), process séparé (`python -m app.mcp.server`), **jamais exposé** au-delà du réseau Docker interne. Les 4 tools demandés (`search_documents`, `get_document`, `search_database`, `get_company_information`) n'ont **aucun paramètre d'identité** dans leur schéma - l'identité voyage dans un header HTTP (`X-Internal-Auth`) contenant un JWT interne signé, minté côté backend, jamais fourni par le LLM.
- **Agent** : boucle de function-calling réelle via l'API `tools` d'Ollama, plus une passe de "grounding" déterministe (recherche documentaire + financière systématique avant de répondre) qui garde le système fiable avec un petit modèle local qui n'invoque pas toujours les tools lui-même - voir *Limites* ci-dessous.
- **Upload de fichiers** : PDF/Excel/Word/texte via `POST /api/documents/upload` — texte extrait pour l'indexation RAG, fichier original stocké tel quel dans **MinIO** et re-téléchargeable, même contrôle de permission sur la lecture du texte que sur le téléchargement du fichier.
- **Alertes** : le Directeur est notifié (cloche + page dédiée) à chaque tentative d'accès hors périmètre, via le chat ou en accès direct à un document.
- **Audit** : table append-only, une entrée par login, par lecture de document, par appel d'outil MCP - avec la raison exacte de chaque refus.

## Limites connues (honnêteté MVP)

- **Précision du petit modèle local** : `llama3.2:1b`/`qwen2.5:1.5b` sont assez petits pour tourner sur un laptop sans GPU, mais moins fiables qu'un modèle 7B+ pour du function-calling spontané ou du raisonnement fin. Le "grounding" déterministe (recherche systématique avant réponse) compense la plupart des cas, mais le texte généré peut occasionnellement être imprécis - **la garantie de sécurité, elle, ne dépend jamais du modèle** : elle est appliquée par le serveur MCP et la requête SQL, avant que le modèle ne voie quoi que ce soit.
- **Auth** : JWT maison pour le MVP, pas encore Keycloak/OIDC (voir la note d'architecture pour le plan de bascule).
- **Policy engine** : règles déclaratives en Python (`app/policy/rules.py`) plutôt que Casbin/OPA - même modèle RBAC+ABAC, implémentation volontairement lisible de bout en bout pour ce prototype.

## Structure

```
backend/app/
  core/        config, DB, sécurité JWT, stockage MinIO, audit logger, alertes
  models/      SQLAlchemy (users, documents+chunks, finance, conversations, audit, alerts)
  policy/      RBAC/ABAC - source de vérité unique (grant effectif = rôle + overrides par utilisateur)
  routers/     auth, documents (+upload), chat, audit, admin (users/access/policy/alerts)
  rag/         embeddings (Ollama), extraction (pdf/xlsx/docx), ingestion/chunking, retriever filtré
  agent/       orchestrateur, client LLM, client MCP
  mcp/         serveur MCP réel (tools + extraction d'identité)
  seed.py      comptes + documents + données financières de démo
frontend/src/
  pages/       Login, Dashboard, Chat, Documents, Profile, Settings, Admin, Permissions, Alerts, AuditLogs
  layout/      sidebar repliable + shell applicatif
  api/, context/  client HTTP typé, contexte d'auth
docker-compose.yml                socle complet (Postgres+pgvector, Ollama, MinIO, MCP, backend, frontend)
docker-compose.native-ollama.yml  overlay optionnel pour un Ollama déjà en route sur l'hôte
.env.example                      secrets/ports à personnaliser avant un déploiement serveur
```

## Commandes utiles

```bash
docker compose logs -f backend        # logs API
docker compose logs -f mcp            # logs serveur MCP
docker compose run --rm seed          # (ré)indexer les documents (idempotent)
docker compose down -v                # tout arrêter et effacer les volumes (reset complet)
```
