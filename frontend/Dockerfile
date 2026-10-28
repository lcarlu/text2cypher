FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Optional extra CA certificates (corporate TLS proxy), see certs/README.md
COPY --from=certs . /tmp/certs/
RUN { cat /etc/ssl/certs/ca-certificates.crt 2>/dev/null; \
      find /tmp/certs -type f \( -name '*.pem' -o -name '*.crt' \) -exec cat {} +; } > /etc/ssl/certs/bundle.pem \
    && rm -rf /tmp/certs
ENV SSL_CERT_FILE=/etc/ssl/certs/bundle.pem \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/bundle.pem

WORKDIR /app/frontend

COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

ENV PATH="/app/frontend/.venv/bin:$PATH"
EXPOSE 8501
CMD ["text2cypher-ui", "--server.address", "0.0.0.0", "--server.port", "8501", "--server.headless", "true"]
