# One-off runner for scripts/relocate_artifacts.py on a host without uv or
# pg_dump. PG_MAJOR must match the server's major version (pg_dump refuses a
# newer server). Build from the repository root.
ARG PG_MAJOR=16
FROM postgres:${PG_MAJOR}
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir pycrdt==0.12.50 sqlalchemy httpx 'psycopg[binary]'
COPY scripts/relocate_artifacts.py /app/relocate_artifacts.py
USER postgres
ENTRYPOINT ["/opt/venv/bin/python", "/app/relocate_artifacts.py"]
