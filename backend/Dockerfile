FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

# Optional extra CA certificates (corporate TLS proxy), see certs/README.md
COPY --from=certs . /tmp/certs/
RUN { cat /etc/ssl/certs/ca-certificates.crt 2>/dev/null; \
      find /tmp/certs -type f \( -name '*.pem' -o -name '*.crt' \) -exec cat {} +; } > /etc/ssl/certs/bundle.pem \
    && rm -rf /tmp/certs
ENV SSL_CERT_FILE=/etc/ssl/certs/bundle.pem \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/bundle.pem

# The project lives in /app/backend so that the dataset cache resolves to /app/data/cache
WORKDIR /app/backend

# Dependencies first: this layer is cached as long as uv.lock does not change
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

ENV PATH="/app/backend/.venv/bin:$PATH"
EXPOSE 8000
CMD ["text2cypher-api", "--host", "0.0.0.0", "--port", "8000"]
