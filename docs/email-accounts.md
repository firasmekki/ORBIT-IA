# Comptes email personnels (Gmail, Outlook) — lecture seule

Chaque utilisateur ORBIT peut connecter **ses propres** boîtes mail. Il demande
ensuite à l'assistant, en langage naturel, de rechercher, afficher ou résumer
ses emails. Par exemple :

- « Donne-moi le dernier email reçu »
- « Résume les cinq derniers emails de mon Outlook »
- « Quels emails demandent une action de ma part ? »

| Fournisseur | Accès | Permission demandée | État |
|---|---|---|---|
| Gmail / Google Workspace | Gmail API, OAuth 2.0 + PKCE | `https://www.googleapis.com/auth/gmail.readonly` | disponible |
| Outlook / Hotmail / Microsoft 365 | Microsoft Graph, OAuth 2.0 + PKCE | `Mail.Read` délégué (+ `openid email profile offline_access`) | disponible |
| Yahoo Mail | IMAP + SASL OAUTHBEARER | `mail-r` (scope restreint) | **désactivé** : l'accès développeur Yahoo doit d'abord être accordé |

**Lecture seule, sans exception.** ORBIT n'envoie, ne répond, ne transfère,
ne déplace, ne marque comme lu et ne supprime jamais un email. Aucune
permission d'écriture n'est demandée et aucun adaptateur n'a de telle méthode.
La couche HTTP refuse toute requête qui n'est pas une lecture vers l'API mail
(`app/connectivity/email_accounts/providers/base.py`). Les tests
`tests/test_email_providers.py` vérifient ces garanties.

Le connecteur **IMAP/SMTP générique** (`app/connectivity/email.py`) est un
produit distinct, que cette fonctionnalité ne modifie pas :

- il s'agit d'une boîte **partagée**, configurée par l'administrateur et accessible selon le rôle ;
- il garde son brouillon et son envoi après confirmation explicite.

## Architecture

```
Chat ─▶ app/agent/email_turn.py ─┐            Page Connectivité ─▶ routers/email_accounts.py ─┐
        (avant le routeur, la                                                                    │
         recherche web et la boucle                                                              ▼
         d'outils)                  └────────▶ app/connectivity/email_accounts/service.py (seul accès à email_connections)
                                                        │  isolation par user_id · verrou de rafraîchissement · audit
                                                        ▼
                                  providers/gmail.py · providers/outlook.py · providers/yahoo.py (désactivé)
                                                        │  contrat commun : contract.py
                                                        ▼
                                  mime.py (texte, repli HTML, pièces jointes) · crypto.py (Fernet) · oauth.py (state/PKCE/cookie)
```

- **Contrat commun** (`contract.py`) : `EmailQuery`, `EmailSummaryItem`, `EmailMessage`,
  `EmailPage` (curseur de pagination opaque), `EmailThread` et `AttachmentMeta`.
  L'agent, les routes et le frontend ne manipulent que ces objets.
- **Taxonomie d'erreurs commune** (`errors.py`). Exemples :
  - OAuth : `oauth_state_invalid`, `oauth_browser_mismatch`, `oauth_denied` ;
  - permission : `permission_missing` ;
  - jeton : `token_expired`, `token_revoked`, `token_unreadable` ;
  - politique Microsoft : `tenant_policy_blocked` ;
  - quota et réseau : `provider_rate_limited`, `provider_unavailable`, `network_error`, `timeout`.

  Un message brut du fournisseur n'est jamais stocké ni affiché.
- **États d'une connexion** :
  - Non configuré, Configuration requise, Connexion en attente ;
  - Autorisation requise, Connecté (uniquement après une lecture test réussie) ;
  - Jeton expiré ou révoqué, Bloqué par une politique ;
  - Erreur temporaire, Compte déconnecté.

## Données

| Table | Contenu |
|---|---|
| `email_connections` | une ligne par boîte connectée et par utilisateur : fournisseur, adresse, nom donné par le fournisseur, identifiant externe, tenant, scopes **réellement accordés**, statut, dernière erreur (code), expiration et dernier rafraîchissement du jeton, dernière vérification, dates de connexion et de déconnexion, jetons **chiffrés** |
| `email_oauth_states` | autorisations en cours : **empreintes** du `state` et du cookie de liaison, vérificateur PKCE chiffré, `nonce`, expiration à 10 minutes, usage unique |
| `messages.email_results` / `email_results_expires_at` | cartes d'une réponse email dans l'historique du chat (faits + résumé IA, jamais le corps complet), avec date d'expiration |

Détails :

- **Plusieurs comptes** par utilisateur sont possibles, y compris du même fournisseur.
  Un index unique empêche de connecter deux fois le même compte.
- **Déconnexion** = suppression logique : le secret est effacé et la ligne est conservée.
- **Création** : les tables et colonnes sont ajoutées par `app/core/migrations.py`
  (`create_all` + `ALTER … ADD COLUMN IF NOT EXISTS`) au démarrage du backend.
  La migration privilégiée existante prend d'abord une sauvegarde de la base.
  Aucune donnée n'est supprimée.
- **Migration du premier connecteur Gmail** (`legacy.py`) : copie unique et
  idempotente de `runtime_settings["gmail_accounts"]`. La ligne d'origine
  **n'est jamais modifiée ni supprimée**. Un compte qui avait aussi autorisé
  l'envoi doit être reconnecté en lecture seule : ses jetons ne sont pas repris.

### Chiffrement

- **Clé** : `EMAIL_TOKEN_ENCRYPTION_KEY` contient une ou plusieurs clés Fernet séparées par des virgules.
  La première chiffre ; toutes déchiffrent, ce qui permet de changer de clé.
- **Liaison à la ligne** : chaque chiffré contient le contexte `connexion|utilisateur|fournisseur`.
  Copié sur une autre ligne, il devient illisible.
- **Clé absente ou invalide** : tous les fournisseurs restent « Non configuré ».
- **Changement de clé** : ajoutez la nouvelle **en tête** et gardez l'ancienne derrière.
  Remplacer la clé sans garder l'ancienne rend les connexions existantes illisibles
  (état « Jeton expiré ou révoqué », il faut se reconnecter).

### Rétention

- Les corps complets des emails ne sont **jamais** stockés.
- Les cartes d'une réponse email (faits + résumé IA local) restent dans
  l'historique `EMAIL_RESULTS_RETENTION_DAYS` jours (30 par défaut).
- Passé ce délai, `retention.py` remplace les cartes et le texte de la réponse
  par un marqueur « expiré ». La suppression d'historique existante s'applique aussi.

## Parcours de connexion (OAuth)

1. **Démarrage** : « Connecter Gmail » ou « Connecter Outlook » appelle
   `POST /api/connectivity/email-accounts/{provider}/oauth/start`. Le backend crée un
   `state`, un `nonce` et un couple PKCE S256, et pose un cookie `HttpOnly`,
   `SameSite=Lax`, valable 10 minutes, qui lie l'autorisation **à ce navigateur**.
2. **Consentement** : l'utilisateur s'authentifie chez Google ou Microsoft. ORBIT ne voit
   jamais son mot de passe.
3. **Callback** : `/api/connectivity/{gmail|outlook}/callback` vérifie le `state`
   (usage unique, expiration) **et** le cookie. Une URL de consentement transmise à
   quelqu'un d'autre est donc refusée (`oauth_browser_mismatch`).
4. **Échange et vérifications** : le code est échangé avec PKCE. Le backend vérifie
   ensuite que la permission de lecture a réellement été accordée, puis effectue une
   lecture test. Ensuite seulement, le compte est enregistré comme « Connecté ».
5. **Retour à l'interface** : `?email_oauth=<code>&provider=…` renvoie un code lisible
   par l'interface, sans aucun jeton ni message du fournisseur.

## Routes

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/connectivity/email-providers` | état de chaque fournisseur (problèmes de configuration nommés, jamais de valeur) |
| GET | `/api/connectivity/email-accounts` | comptes de l'utilisateur courant (sans secret) |
| POST | `/api/connectivity/email-accounts/{provider}/oauth/start` | URL d'autorisation + cookie de liaison (`?reconnect=<id>` pour réautoriser) |
| GET | `/api/connectivity/gmail/callback`, `/outlook/callback`, `/yahoo/callback` | fin du flux OAuth (URI à enregistrer telles quelles) |
| PATCH | `/api/connectivity/email-accounts/{id}` | activer/désactiver, compte principal, libellé |
| DELETE | `/api/connectivity/email-accounts/{id}` | révocation (Google) ou lien de révocation manuelle (Microsoft), effacement des secrets |
| POST | `/api/connectivity/email-accounts/{id}/test` | lecture test |
| POST | `/api/connectivity/email-accounts/{id}/refresh-status` | renouvellement si nécessaire, puis test |
| POST | `/api/connectivity/email-accounts/{id}/messages/search` | recherche, pagination (`cursor`) |
| POST | `/api/connectivity/email-accounts/{id}/messages/read` | lecture d'un message (référence signée) |
| POST | `/api/connectivity/email-accounts/{id}/threads/read` | lecture d'un fil |

Comportement commun à ces routes :

- une connexion ou une référence de message d'un autre utilisateur renvoie **404**,
  sans aucun appel au fournisseur ;
- les références de message sont opaques et signées (HMAC) ;
- les anciennes routes Gmail, dont `/gmail/send`, ont été supprimées.

## Assistant (chat)

`app/agent/email_turn.py` traite une question sur la boîte mail **avant** le
routeur, la recherche web et la boucle d'outils :

1. **Détection déterministe**, sans modèle (`email_intent.py`) : nombre d'emails,
   période dans le fuseau du navigateur, non lus, expéditeur, pièces jointes,
   « demandent une action », rapport, échéances.
2. **Choix du compte** :
   - fournisseur ou adresse cités dans la demande ;
   - sinon le compte principal ;
   - sinon une question courte avec un bouton par compte.
3. **Lecture** :
   - limite par défaut `EMAIL_DEFAULT_LIMIT` (10), annoncée dans la réponse, et maximum
     `EMAIL_MAX_LIMIT` (25) ;
   - au plus `EMAIL_MAX_SUMMARIZE` (10) résumés et 6 000 caractères par email ;
   - délai réseau de 20 s ; les erreurs 429 et 5xx sont retentées avec attente progressive.
4. **Résumé par le modèle texte local uniquement** (`LLM_BASE_URL`, Qwen) :
   - un appel par email, **sans aucun outil** ;
   - l'email est placé entre `<email>…</email>` et présenté comme une donnée non fiable ;
   - la réponse est un JSON contraint.
5. **Contrôle de la réponse du modèle** :
   - une échéance est gardée seulement si elle **cite l'email mot pour mot** ;
   - un champ qui mentionne une adresse absente de l'email est écarté.
6. **Cartes** : les faits (expéditeur, adresse, objet, date, pièces jointes, lien
   vers l'original) viennent du fournisseur. La synthèse IA et les suggestions sont
   affichées à part, avec leur libellé.

Si le modèle local est indisponible, les faits sont affichés seuls, avec un avis.

Garanties de ce parcours :

- Pendant un tour email, aucune recherche web, aucun outil documentaire et aucune IA
  distante ne sont appelés.
- Le modèle de vision (port 8097) n'est jamais sollicité.
- Une demande d'envoi, de réponse ou de suppression reçoit un refus explicite :
  ORBIT est en lecture seule.

Traces affichées dans « actions effectuées » : `email_list_recent`,
`email_search`, `email_get_message` et `email_summarize_local`. Elles ne
contiennent que le fournisseur, le nombre de résultats et le nom des filtres
utilisés, jamais leur valeur.

## Configuration

Variables (backend uniquement, voir `.env.example`) :

- **Clé et interface** : `EMAIL_TOKEN_ENCRYPTION_KEY` (obligatoire), `EMAIL_FRONTEND_REDIRECT_URI`.
- **Limites** : `EMAIL_DEFAULT_LIMIT`, `EMAIL_MAX_LIMIT`.
- **Rétention et fuseau** : `EMAIL_RESULTS_RETENTION_DAYS`, `EMAIL_DEFAULT_TIMEZONE`.
- **Google** : `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET`, `GMAIL_OAUTH_REDIRECT_URI`.
- **Microsoft** : `OUTLOOK_OAUTH_CLIENT_ID`, `OUTLOOK_OAUTH_CLIENT_SECRET`,
  `OUTLOOK_OAUTH_REDIRECT_URI`, `OUTLOOK_OAUTH_AUTHORITY`.

Générer la clé :

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Ou, sans Python sur l'hôte :

```
docker compose run --rm --no-deps -T backend python -c "..."
```

Après une modification de `.env`, recréez le backend pour qu'il relise ses
variables : `docker compose up -d --no-deps backend`.

### Google Cloud (Gmail)

1. Créer ou choisir un projet et activer **Gmail API**.
2. Configurer l'écran de consentement : nom, e-mail d'assistance, politique de confidentialité.
   - Type *External* en mode *Testing*, en ajoutant les comptes testeurs.
   - Ou type *Internal*, pour un usage limité à une organisation Google Workspace.
3. Dans *Data Access*, déclarer **uniquement** `.../auth/gmail.readonly`.
4. Créer un client OAuth de type **Web application** et y inscrire l'URI exacte
   `http://localhost:8081/api/connectivity/gmail/callback` (en production, l'URI HTTPS
   équivalente).
5. Limites du mode *Testing* :
   - seuls les testeurs déclarés peuvent se connecter ;
   - un écran d'avertissement s'affiche ;
   - **les refresh tokens expirent au bout de 7 jours**, il faut donc se reconnecter.
6. **Ouverture au public** : `gmail.readonly` est un **scope restreint**. Il faut :
   - une vérification de la marque, puis du scope ;
   - une **évaluation de sécurité CASA** par un évaluateur agréé, puisque les données
     sont lues par un serveur, à renouveler tous les 12 mois ;
   - le respect de la *Limited Use policy* de Google.

   **L'application n'est pas vérifiée aujourd'hui.**

### Microsoft Entra ID (Outlook)

1. *App registrations* → nouvelle application. Choisir les types de comptes selon
   `OUTLOOK_OAUTH_AUTHORITY` : `common` = comptes Microsoft personnels et comptes
   professionnels ou scolaires.
2. Plateforme **Web**, avec l'URI de redirection exacte
   `http://localhost:8081/api/connectivity/outlook/callback`.
3. Créer un secret client et noter sa date d'expiration.
4. Permissions déléguées Microsoft Graph : `Mail.Read` (+ `openid`, `email`,
   `profile`, `offline_access`). Aucune permission d'écriture ou d'envoi.
5. **Politiques des organisations** :
   - un tenant peut interdire le consentement des utilisateurs ou l'autoriser seulement
     pour des éditeurs vérifiés ;
   - l'utilisateur voit alors « Bloqué par une politique Microsoft » et un administrateur
     doit approuver l'application (*admin consent workflow*) ;
   - une diffusion multi-organisations demande la **vérification de l'éditeur**.
6. **Révocation** :
   - Microsoft ne permet pas à une application de retirer seule une autorisation déléguée ;
   - la déconnexion efface donc les jetons locaux et indique à l'utilisateur où retirer
     l'accès : account.live.com/consent/Manage (compte personnel) ou myapps.microsoft.com
     (compte professionnel).
7. **Refresh tokens** : environ 90 jours, renouvelés à chaque utilisation. Le nouveau
   jeton est enregistré sous verrou.

### Yahoo Mail (préparé, désactivé)

- **Accès** : Yahoo n'offre que l'IMAP (`imap.mail.yahoo.com:993`) avec SASL
  OAUTHBEARER, et le scope `mail-r` est **restreint**.
- **Activation** : elle suppose d'abord une demande d'accès développeur auprès de Yahoo
  (« Request Developer Access to Yahoo Mail », contact : mail-api@yahooinc.com). Aucun
  délai d'instruction n'est publié.
- **Code en attendant** : `providers/yahoo.py` décrit les points d'accès et la liste
  blanche des commandes IMAP en lecture seule. Le fournisseur reste désactivé dans
  `docker-compose.yml` et aucun mot de passe d'application n'est proposé en repli.
- **À vérifier avant d'activer Yahoo** :
  - l'acceptation d'une redirection vers `localhost` ;
  - la prise en charge de PKCE ;
  - la durée de vie des refresh tokens ;
  - le comportement de la révocation.

## Tests

Tous utilisent des réponses simulées (`tests/email_support.py`, `httpx.MockTransport`)
et la base `orbitia_test`. Aucune vraie boîte n'est contactée.

| Fichier | Ce qui est couvert |
|---|---|
| `test_email_providers.py` | adaptateurs : liste, pagination, MIME, repli HTML, corps vide, pièces jointes, erreurs 401/403/404/429/5xx, délai dépassé, réseau, Yahoo désactivé, garanties de lecture seule |
| `test_email_accounts_service.py` | configuration absente, OAuth, `state` et PKCE, cookie navigateur, consentement refusé, permission manquante, isolation, rafraîchissement (dont rotation Microsoft), révocation, chiffrement, migration Gmail, rétention |
| `test_email_intent.py` | détection des demandes et des filtres |
| `test_email_turn.py` | parcours chat, choix du compte, contrôle du résumé, injection d'instructions, absence de web et d'IA distante, modèle local en panne, erreurs, suites (« suivants »), journaux |
| `test_email_routes.py` | routes, cookie, callbacks, 404 entre utilisateurs, absence de route d'envoi, persistance avec expiration |
