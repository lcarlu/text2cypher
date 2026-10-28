# Extra CA certificates (corporate proxy)

Behind a proxy that intercepts TLS, drop its CA certificate(s) here as `.pem` or `.crt` files
(e.g. `cp "$SSL_CERT_FILE" certs/corporate-ca.pem`: the full chain, root certificate included,
that your local Python already trusts). They are added to the trust store of the
Docker images, at build time (uv downloads) and at run time (dataset download, LLM API calls).
Certificate files are git-ignored. Leave the folder empty otherwise.
