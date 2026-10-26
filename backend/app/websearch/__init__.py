"""Web search for ORBIT AI - optional, off by default, answered by the local
model from what was really retrieved.

    providers.py  search APIs behind one interface (Brave Search API,
                  Google Programmable Search JSON API) - add a provider
                  without touching the orchestrator
    fetcher.py    reads a result page safely (SSRF guard, robots.txt,
                  size / type / redirect limits) - page text is untrusted data
    config.py     settings: .env defaults + administrator overrides
                  (runtime_settings); API keys never leave the server
    service.py    search + cache + audit + page reading, with honest statuses
    intent.py     LOCAL / WEB / HYBRID / WEB_DISABLED, and the minimal query
                  actually sent outside (never documents nor history)

Nothing here ever pretends: a search that did not run, failed or returned
nothing is reported as such, with no invented source.
"""
