# Audit du routage de recherche Web

Audit effectué le 14 octobre 2026 sur le dépôt ORBIT AI et ses services en cours.

## Résultat

Le pipeline existant couvre le routage direct/local, les connaissances générales, les documents internes, le Web et les demandes hybrides. Les règles déterministes traitent les salutations, demandes documentaires reconnues et formulations d'actualité courantes. Pour les formulations moins évidentes, le routeur peut demander une classification JSON au modèle local ; ce modèle ne peut activer le Web que lorsque l'administrateur l'a activé en mode `auto`. Les modes `manual` (libellé « sur demande explicite » dans l'interface) et `disabled` empêchent une recherche automatique.

Les tests vérifient aussi que les requêtes Brave sont dérivées du message courant, sans historique ni extraits RAG, que les pages Web sont données au modèle comme contenu non fiable, et que les réponses et sources restent honnêtes en cas d'erreur fournisseur ou d'indisponibilité du modèle local.

## Exemples vérifiés

| Question | Route attendue | Vérification |
| --- | --- | --- |
| Bonjour | réponse directe, aucun outil | test de conversation sans outil |
| Différence entre courant alternatif et continu | connaissances générales locales | tests de routage et d'orchestration |
| Dernière version de Python | Web | test ajouté |
| Dernières nouveautés de Qwen | Web | tests de routage et d'intégration simulée |
| Dernière mise à jour d'ISO 9001 | Web | test ajouté |
| Responsable actuel d'un gouvernement | Web, autre domaine | test ajouté |
| État de maintenance de la bibliothèque `requests` | le classifieur local peut choisir Web en mode auto | test ajouté, puis refus du Web en mode manual |
| Maintenance prévue dans un contrat interne | RAG local | test existant |
| Comparaison d'un contrat à la réglementation actuelle | RAG + Web | test existant |

## Exécution des tests

L'image du service backend fixe son répertoire de travail à `/app` et copie `app/`, mais pas `tests/`. Le module de production est importable dans le conteneur (`import app` résout `/app/app/__init__.py`). Lancer un script de test situé dans `backend/tests` par son chemin peut toutefois omettre `backend/` de `sys.path`; lancer pytest dans le conteneur de service échoue aussi car les tests n'y sont pas copiés. Il faut monter le dossier backend et ajouter sa racine à `PYTHONPATH`, sans modifier les imports applicatifs.

Commande utilisée depuis la racine du dépôt :

```powershell
docker compose run --rm -T `
  -v "${PWD}/backend:/workspace/backend" `
  -e PYTHONPATH=/workspace/backend `
  -e TEST_DATABASE_URL=postgresql+psycopg://orbitia_test:orbitia_test@postgres:5432/orbitia_test `
  backend python -m pytest -q /workspace/backend/tests
```

La suite a réussi : **779 tests backend passés** (4 avertissements de dépréciation). Après ajout des cinq contrôles de routage ci-dessus, ces cinq tests ciblés passent également. Les **43 tests frontend** passent et `npm run build` réussit, y compris `tsc -b`.

## État des services et limite de validation

- Backend et frontend sains ; PostgreSQL sain ; les autres services de la pile sont actifs.
- Les trois volumes de données nommés du projet existent toujours : PostgreSQL, Ollama et stockage objet. Le catalogue Docker contient aussi des volumes orphelins ; aucun volume n'a été supprimé.
- Base applicative au contrôle initial : **5 311 documents et 262 819 chunks**. La base `orbitia_test` est distincte.
- La configuration effective est Brave activé, déclenchement `auto`, langue `fr`. Aucune clé Brave n'est présente dans l'environnement ou dans les secrets administrateur. L'état du fournisseur est donc `not_configured` ; aucun appel Brave en direct n'a été effectué. Les tests d'intégration Brave utilisent des réponses HTTP simulées.
- Le modèle conversationnel est le service local configuré. Les tests vérifient la transmission des résultats web au modèle local ; aucun cycle réel Brave → pages → modèle n'a pu être validé sans clé.

