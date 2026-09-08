# Linux container image for the OPUS operations dashboard.
#
# The image is self-contained: it carries the application, the SQL migrations,
# and a psql client, so the same image runs both the dashboard and the
# database migration step. A deployment therefore needs nothing from this
# repository except docker-compose.yml and an .env file.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPUS_APP_HOST=0.0.0.0 \
    OPUS_APP_SHOW=false

WORKDIR /app

# psql is required by scripts/setup_database.sh: db/00_bootstrap.psql uses
# psql meta-commands, so a plain driver connection cannot replace it.
#
# This runs before the extra CA step on purpose: apt uses plain HTTP, so it
# needs no custom trust anchors, and installing packages can upgrade
# ca-certificates, whose post-install script regenerates the CA bundle and
# would discard certificates added before it.
#
# The Acquire options make the build survive caching HTTP proxies, which
# otherwise corrupt apt's index files and fail with "Hash Sum mismatch".
RUN printf '%s\n' \
      'Acquire::http::No-Cache "true";' \
      'Acquire::http::Pipeline-Depth "0";' \
      'Acquire::BrokenProxy "true";' \
      > /etc/apt/apt.conf.d/99fix-proxy \
    && for attempt in 1 2 3; do \
         apt-get update && break || { echo "apt-get update failed (attempt $attempt)"; rm -rf /var/lib/apt/lists/*; sleep 5; }; \
       done \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Optional: trust extra root CAs when building behind a TLS-inspecting proxy
# or firewall. certs/ holds only a README on a normal network, so this is a
# verified no-op by default. It runs after apt and before pip, so pip and the
# running application both benefit. See certs/README.md.
#
# Three failure modes are handled explicitly, because all three are silent:
#   * Certificates exported on Windows carry CRLF line endings, which
#     update-ca-certificates skips while still reporting success ("0 added")
#     and exiting 0. The CRs are stripped before installing.
#   * On this base image update-ca-certificates only creates CApath hash
#     links; it does not append to ca-certificates.crt, which is the single
#     file Python, requests and pip actually read.
#   * If a supplied certificate still is not trusted afterwards, the build
#     fails here rather than producing an image that cannot reach PyPI or the
#     OPUS API for reasons that only surface at runtime.
COPY certs/ /tmp/extra-certs/
RUN set -eu; \
    bundle=/etc/ssl/certs/ca-certificates.crt; \
    found=0; \
    for source in /tmp/extra-certs/*.crt; do \
      [ -e "$source" ] || continue; \
      name="$(basename "$source")"; \
      if ! openssl x509 -in "$source" -noout -subject >/dev/null 2>&1; then \
        echo "ERROR: $name is not a valid PEM certificate." >&2; \
        echo "Export it in Base-64 (PEM) form; see certs/README.md." >&2; \
        exit 1; \
      fi; \
      found=$((found + 1)); \
      tr -d '\r' < "$source" > "/usr/local/share/ca-certificates/$name"; \
    done; \
    update-ca-certificates; \
    for source in /usr/local/share/ca-certificates/*.crt; do \
      [ -e "$source" ] || continue; \
      name="$(basename "$source")"; \
      if ! openssl verify -no-CApath -CAfile "$bundle" "$source" >/dev/null 2>&1; then \
        openssl x509 -in "$source" >> "$bundle"; \
      fi; \
      if ! openssl verify -no-CApath -CAfile "$bundle" "$source" >/dev/null 2>&1; then \
        echo "ERROR: $name could not be added to $bundle." >&2; \
        exit 1; \
      fi; \
      echo "Trusted extra root CA: $name"; \
    done; \
    if [ "$found" -gt 0 ]; then echo "Installed $found extra root CA(s)."; fi; \
    rm -rf /tmp/extra-certs
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# psycopg[binary] ships its own libpq; no further system packages are needed.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY opus_dashboard ./opus_dashboard
COPY db ./db
COPY scripts/setup_database.sh ./scripts/setup_database.sh
COPY .logos ./.logos
COPY assets ./assets

# Run as an unprivileged user; the dashboard may be exposed beyond a trusted
# network, so it should not hold root inside the container.
# The credential directory is created here, owned by that user, because Docker
# initialises an empty named volume from the image's directory: without this
# the volume is created root-owned and the app cannot save the OPUS login.
RUN chmod +x /app/scripts/setup_database.sh \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /home/appuser/.connect-ops \
    && chown -R appuser:appuser /home/appuser \
    && chmod 700 /home/appuser/.connect-ops \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8091

# A plain TCP check works regardless of whether the access gate is enabled.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(0 if s.connect_ex(('127.0.0.1',8091))==0 else 1)"

CMD ["python", "main.py"]
