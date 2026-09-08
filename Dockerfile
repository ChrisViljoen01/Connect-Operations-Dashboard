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

# Optional: trust extra root CAs when building behind a TLS-inspecting proxy
# or firewall. certs/ holds only a README on a normal network, so this is a
# verified no-op by default. It runs before apt and pip so that both benefit
# from the added trust anchors. See certs/README.md.
COPY certs/ /usr/local/share/ca-certificates/
RUN update-ca-certificates
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# psql is required by scripts/setup_database.sh: db/00_bootstrap.psql uses
# psql meta-commands, so a plain driver connection cannot replace it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

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
RUN chmod +x /app/scripts/setup_database.sh \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8091

# A plain TCP check works regardless of whether the access gate is enabled.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(0 if s.connect_ex(('127.0.0.1',8091))==0 else 1)"

CMD ["python", "main.py"]
