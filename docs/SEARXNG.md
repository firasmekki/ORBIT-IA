# SearXNG local

The optional SearXNG service is the `searxng` provider for the existing web-search service. It uses the official `searxng/searxng` image, exposes no host port, and enables the JSON search format. The backend reaches it at `http://searxng:8080` on the Compose network. The existing Brave and Google providers remain available.

Set a random `SEARXNG_SECRET` in the ignored `.env` file. `SEARXNG_URL` defaults to the local Compose service. Remote instances must use HTTPS and pass the page fetcher's public-address checks; local HTTP is restricted to the Compose service name. No search is enabled automatically: use Admin > Recherche Web to select SearXNG and set its trigger and enabled state.

Start only this service with `docker compose up -d searxng`; check it with `docker compose ps searxng`. To use the updated application code, rebuild/restart only the backend with `docker compose up -d --build --no-deps backend`. No existing database, document-store, or Ollama volume is changed.

SearXNG aggregates independent search engines. A healthy local instance does not guarantee every upstream engine responds; the admin connection test reports engines SearXNG declares unresponsive.
