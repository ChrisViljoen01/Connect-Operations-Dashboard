#!/usr/bin/env bash
# Linux/container equivalent of setup_database.ps1, for use inside the
# docker-compose "migrate" service (postgres image, psql available).
# Mirrors the same migration order and idempotency rules.
set -euo pipefail

HOST_NAME="${OPUS_DB_HOST:-db}"
PORT="${OPUS_DB_PORT:-5432}"
ADMIN_USER="${OPUS_PG_ADMIN_USER:-postgres}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_DIR="$PROJECT_ROOT/db"

if [[ -z "${OPUS_PG_ADMIN_PASSWORD:-}" ]]; then
  echo "Set OPUS_PG_ADMIN_PASSWORD before running setup." >&2
  exit 1
fi
if [[ -z "${OPUS_DB_APP_PASSWORD:-}" ]]; then
  echo "Set OPUS_DB_APP_PASSWORD before running setup." >&2
  exit 1
fi
if [[ ${#OPUS_DB_APP_PASSWORD} -lt 24 ]]; then
  echo "OPUS_DB_APP_PASSWORD must contain at least 24 characters." >&2
  exit 1
fi

export PGPASSWORD="$OPUS_PG_ADMIN_PASSWORD"

psql_admin() {
  psql --no-psqlrc --set ON_ERROR_STOP=on \
    --host "$HOST_NAME" --port "$PORT" --username "$ADMIN_USER" "$@"
}

echo "Waiting for PostgreSQL at $HOST_NAME:$PORT..."
for _ in $(seq 1 60); do
  if psql_admin --dbname postgres --command 'SELECT 1' >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

app_password_b64="$(printf '%s' "$OPUS_DB_APP_PASSWORD" | base64 | tr -d '\n')"
{
  printf "\\set app_password_b64 '%s'\n" "$app_password_b64"
  cat "$DB_DIR/00_bootstrap.psql"
} | psql_admin --dbname postgres --file -

run_migration() {
  local path="$1" label="$2"
  echo "Applying $label migration ($path)..."
  psql_admin --dbname connect_logistics_ops --file "$path"
}

run_numbered_migration() {
  local version="$1" path="$2" label="$3"
  local applied
  applied=$(psql_admin --dbname connect_logistics_ops --tuples-only --no-align \
    --command "SELECT EXISTS (SELECT 1 FROM audit.schema_migrations WHERE version = '$version');" | tail -n1)
  if [[ "$applied" == "t" ]]; then
    echo "Skipped $label migration $version; already applied"
    return
  fi
  run_migration "$path" "$label"
}

run_migration "$DB_DIR/01_schema.sql" "Schema"
run_migration "$DB_DIR/02_connector_permissions.sql" "Connector permissions"
run_migration "$DB_DIR/03_checklist_detail.sql" "Checklist detail"
run_migration "$DB_DIR/04_repair_checklist_stage_mapping.sql" "Stage mapping"
run_migration "$DB_DIR/05_workflow_lifecycle_and_weekly_reset.sql" "Weekly reset"
run_numbered_migration "006" "$DB_DIR/06_raw_record_source_hash.sql" "Raw record source hash"
run_numbered_migration "007" "$DB_DIR/07_transport_root_baseline.sql" "Transport root baseline"
run_numbered_migration "008" "$DB_DIR/08_operational_analytics.sql" "Operational analytics"
run_numbered_migration "009" "$DB_DIR/09_stock_transit_dimensions.sql" "Stock and transit dimensions"

echo "Database setup completed."
echo "Host: $HOST_NAME"
echo "Port: $PORT"
echo "Database: connect_logistics_ops"
echo "Application role: connect_ops_app"
