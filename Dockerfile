FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

WORKDIR /app

RUN addgroup --system --gid 10001 app && adduser --system --uid 10001 --gid 10001 app

RUN pip install --no-cache-dir agent-comms-mcp==0.1.1

COPY --chown=app:app entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["sh", "-c", "python -c \"import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('MCP_PORT', '8080') + '/health')\" || exit 1"]

CMD ["./entrypoint.sh"]
