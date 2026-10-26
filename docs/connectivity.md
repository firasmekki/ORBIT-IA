# Connectivité ORBIT AI

## Connecteurs réellement implémentés

- **Recherche Web** réutilise les routes et le service Brave existants. Les réglages, le fournisseur, le déclenchement, le cache, les quotas et les sources restent gérés par le panneau d'administration Recherche Web. La page Connectivité en affiche le statut et ouvre ce panneau.
- **Email générique IMAP/SMTP** utilise IMAP et SMTP chiffrés avec TLS. Il permet la recherche par expéditeur, destinataire, sujet et période, la lecture d'un email et la préparation d'un brouillon. Les tools d'email ne sont proposés au modèle local que si le connecteur est activé, le tool sélectionné et le rôle autorisé pour ce tool.

- **Comptes email personnels (Gmail, Outlook)** : chaque utilisateur connecte ses propres boîtes par OAuth, en lecture seule, et peut les interroger dans le chat avec le modèle local. Voir [email-accounts.md](email-accounts.md). Yahoo Mail est préparé mais désactivé tant que Yahoo n'a pas accordé l'accès développeur.

Ce connecteur générique est une boîte **partagée** gérée par l'administrateur ; il est distinct des comptes personnels ci-dessus et n'est pas modifié par eux. Calendriers, stockages, API personnalisées, automatisation et outils industriels restent non configurés.

## Configurer un compte Email

Dans **Direction → Connectivité → Configurer** :

1. Renseigner le serveur/port IMAP et le serveur/port SMTP du fournisseur, l'adresse de connexion et le dossier (souvent INBOX).
2. Garder TLS activé. Pour un serveur SMTP STARTTLS sur le port 587, décocher l'option SMTP SSL/TLS ; le client démarre alors TLS avant l'authentification.
3. Enregistrer le mot de passe ou mot de passe d'application. La valeur est chiffrée avant stockage dans les paramètres runtime ; les réponses API ne la renvoient jamais.
4. Choisir séparément les rôles autorisés pour rechercher, lire et préparer des brouillons, puis ceux qui peuvent envoyer après confirmation.
5. Activer le connecteur et lancer **Tester la connexion**. Le test se connecte à IMAP et s'authentifie à SMTP, sans envoyer de message.

La connexion ne devient « Connectée » qu'après un test réel réussi. Pour une boîte Gmail ou Outlook personnelle, utilisez plutôt les comptes email personnels (OAuth, lecture seule).

## Envoi explicite

draft_email ne fait qu'émettre un brouillon et n'est jamais relié à une action d'envoi automatique. L'interface affiche le destinataire, l'objet et le contenu exact. Une confirmation séparée fournit un identifiant à usage unique valable dix minutes ; le backend vérifie que le brouillon n'a pas changé, recontrôle les autorisations et consomme la confirmation après envoi. Annulation, expiration ou changement de contenu empêche l'envoi.

Les journaux enregistrent le rôle, l'action, l'état et une référence, jamais le mot de passe, le sujet, les adresses ou le contenu du message. Les traces de conversation omettent le message utilisateur pour un tour qui utilise les tools Email.

## Données

Les paramètres utilisent la table runtime_settings déjà présente. Les opérations sont ajoutées au journal d'audit existant. Aucune table documentaire, conversation ou volume Docker n'est supprimé ou réinitialisé par cette fonctionnalité.
