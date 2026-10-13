# Postgres backup container with mc (MinIO Client) for off-site mirroring

# mc is copied from a pinned image instead of downloaded from a MinIO-hosted
# URL (dl.min.io/client/mc/... now returns 410 Gone). Keep this default in sync
# with MC_IMAGE in docker-compose.yml; override via the build arg / MC_IMAGE.
ARG MC_IMAGE=pgsty/mc:RELEASE.2026-09-16T00-00-00Z@sha256:cfc83108c3abb371f8fb84d99c1fdc88f8c237e022409b0081fb7c0a3be634dd
FROM ${MC_IMAGE} AS mc

FROM postgres:16-alpine

RUN apk add --no-cache bash curl
COPY --from=mc /usr/bin/mc /usr/local/bin/mc

# Copy backup scripts
COPY backup.sh /usr/local/bin/backup.sh
COPY restore.sh /usr/local/bin/restore.sh
COPY minio-mirror.sh /usr/local/bin/minio-mirror.sh
COPY restore-test.sh /usr/local/bin/restore-test.sh
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh \
              /usr/local/bin/minio-mirror.sh /usr/local/bin/restore-test.sh

# /backups is a bind-mount from the host; the host dir must be writable by the
# container user. The entrypoint handles graceful permission errors; this dir
# is created so the image works standalone for smoke tests.
RUN mkdir -p /backups

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Not switching to the image's built-in non-root "postgres" user (uid 70) here:
# /backups is a bind-mount from the prod host (/opt/relay/data/backups on
# relay-host), verified root:root 0755 — a non-root container user would
# silently lose write access and every nightly backup would fail quietly
# (entrypoint.sh only log_warns on backup.sh failure, it doesn't crash).
# Fixing this needs the host dir chowned to uid 70 by someone with prod
# write access (out of scope here — Team Relay prod access is read-only),
# then this can switch to `USER postgres`. Tracked: see PR description.
# nosemgrep: dockerfile.security.missing-user-entrypoint.missing-user-entrypoint
ENTRYPOINT ["/entrypoint.sh"]
