FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

WORKDIR /app

# UID/GID pinned so they're stable across base-image rebuilds — an unpinned
# uid can drift on a future base-image change and silently break volume
# ownership (e.g. EFS access points with fixed POSIX ownership).
RUN addgroup --system --gid 10001 app && adduser --system --uid 10001 --gid 10001 app

# agent-comms-mcp is published by this team from https://github.com/redesignhealth/agent-comms-mcp
# requirements.lock pins every transitive dep with sha256 hashes (--require-hashes).
# Migration continuity: the wheel's migrations/versions/ must match
# this repo's (verified by unpacking the wheel and diffing filenames). See docs/RELEASING.md.
# To upgrade: bump the version in pyproject.toml, regenerate requirements.lock
#   (see docs/RELEASING.md), verify migration continuity, then open a PR.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock

COPY --chown=app:app entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["sh", "-c", "python -c \"import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('MCP_PORT', '8080') + '/health')\" || exit 1"]

CMD ["./entrypoint.sh"]
