# Multi-stage build per the RH container-security checklist
# (topics/09-security.md): pinned slim base, non-root runtime user,
# no secrets in the image.

# Pinned by digest (not the mutable `3.12-slim` tag) so the base image is
# reproducible and can't drift underfoot between builds. Resolved via
# `docker buildx imagetools inspect python:3.12-slim` on 2026-08-11
# (tag was 3.12.13-slim-trixie at resolution time).
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS builder

WORKDIR /app

# Pin uv to match CI (.github/workflows/ci.yml). Pinning avoids
# build-behavior drift from an unpinned install.
RUN pip install --no-cache-dir uv==0.11.28

# Copy only deps first for layer caching. Flat-layout app: the project
# itself is never installed as a wheel (--no-install-project).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy service code
COPY . .

FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY --from=builder --chown=app:app /app /app
COPY --chown=app:app entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# The venv is fully baked at build time; run its interpreter directly so
# the container never re-contacts a package index at startup (the Tailscale
# sidecar runs in userspace mode with no outbound DNS — rh-mcp TECH-3923).
ENV PATH="/app/.venv/bin:$PATH"

USER app

EXPOSE 8080

# Distinguishes a crashed/unhealthy container from a healthy one during
# ECS Fargate rolling deploys, hitting the same /health endpoint the
# service exposes on MCP_PORT (default 8080, see main.py).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Runs pending Alembic migrations before starting the server (see
# entrypoint.sh) — no automated migration mechanism otherwise runs inside
# the container.
CMD ["./entrypoint.sh"]
