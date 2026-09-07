# Linux container image for the OPUS operations dashboard.
# Requires an external PostgreSQL database (see docker-compose.yml, which
# provisions one and runs migrations automatically).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPUS_APP_HOST=0.0.0.0 \
    OPUS_APP_SHOW=false

WORKDIR /app

# psycopg[binary] ships its own libpq; no extra system packages are required.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY opus_dashboard ./opus_dashboard
COPY .logos ./.logos
COPY assets ./assets

EXPOSE 8091

CMD ["python", "main.py"]
