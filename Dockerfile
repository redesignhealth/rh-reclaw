# Multi-stage build per the RH container-security checklist
# (topics/09-security.md): pinned slim base, non-root runtime user,
# no secrets in the image.

FROM python:3.12-slim AS builder

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

FROM python:3.12-slim

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY --from=builder --chown=app:app /app /app

# The venv is fully baked at build time; run its interpreter directly so
# the container never re-contacts a package index at startup (the Tailscale
# sidecar runs in userspace mode with no outbound DNS — rh-mcp TECH-3923).
ENV PATH="/app/.venv/bin:$PATH"

USER app

EXPOSE 8080

CMD ["python", "main.py"]
