# Production image for the Streamable HTTP transport (see main.py:run_http).
# This replaces the previous mcp-proxy/Glama.ai scaffolding, which cloned
# from GitHub at build time instead of using the local source tree and ran
# the server over stdio behind a third-party proxy.

FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.7.8 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml README.md ./
COPY mcp_server_bwt/ ./mcp_server_bwt/

RUN uv sync --no-dev

FROM python:3.13-slim

RUN groupadd --system appuser \
    && useradd --system --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    MCP_TRANSPORT=http \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

USER appuser

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=2).status == 200 else 1)"

CMD ["mcp-server-bing-webmaster"]
