
# Pinned to bookworm (not the floating python:3.11-slim, which currently
# resolves to trixie) so the PGDG apt repo below - keyed to a Debian
# codename - keeps working regardless of which Debian release the
# unpinned tag moves to next.
FROM python:3.11-slim-bookworm

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# postgresql-client-16, matching the pgvector/pgvector:pg16 server exactly
# (docker-compose.yml) - used by app.core.backup for pg_dump/pg_restore
# (see INCIDENTS.md). Debian's own repos only ship whatever their current
# default major version is (not necessarily 16), hence PGDG's own repo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gnupg curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y --auto-remove gnupg curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000 8090

# Overridden per-service in docker-compose.yml (api / mcp / seed all share this image)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
