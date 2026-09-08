#!/bin/sh
# Linux/container equivalent of setup_database.ps1, for use inside the
# docker-compose "migrate" service (postgres image, psql available).
# Mirrors the same migration order and idempotency rules.
#
# POSIX sh only: the postgres:*-alpine image provides busybox ash, not bash.
set -eu

HOST_NAME="${OPUS_DB_HOST:-db}"
PORT="${OPUS_DB_PORT:-5432}"
ADMIN_USER="${OPUS_PG_ADMIN_USER:-postgres}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB_DIR="$PROJECT_ROOT/db"

if [ -z "${OPUS_PG_ADMIN_PASSWORD:-}" ]; then
  echo "Set OPUS_PG_ADMIN_PASSWORD before running setup." >&2
  exit 1
fi
if [ -z "${OPUS_DB_APP_PASSWORD:-}" ]; then
  echo "Set OPUS_DB_APP_PASSWORD before running setup." >&2
  exit 1
fi
if [ "${#OPUS_DB_APP_PASSWORD}" -lt 24 ]; then
  echo "OPUS_DB_APP_PASSWORD must contain at least 24 characters." >&2
  exit 1
fi

# Fail loudly if the migration files are missing. Previously these were bind
# mounted, and a deployment without a repository clone silently received an
# empty directory, so this script did nothing, exited 0, and the application
# started against a database with no schema.
if [ ! -f "$DB_DIR/00_bootstrap.psql" ]; then
  echo "Migration files not found under $DB_DIR." >&2
  echo "Expected them to be baked into the image; check the Dockerfile COPY." >&2
  exit 1
fi

PGPASSWORD="$OPUS_PG_ADMIN_PASSWORD"
export PGPASSWORD

psql_admin() {
  psql --no-psqlrc --set ON_ERROR_STOP=on \
    --host "$HOST_NAME" --port "$PORT" --username "$ADMIN_USER" "$@"
}

echo "Waiting for PostgreSQL at $HOST_NAME:$PORT..."
attempt=0
connected=0
last_error=""
while [ "$attempt" -lt 60 ]; do
  if last_error="$(psql_admin --dbname postgres --command 'SELECT 1' 2>&1 >/dev/null)"; then
    connected=1
    break
  fi
  # An authentication failure is not a startup delay: retrying for two
  # minutes only hides the real cause behind a misleading timeout. This
  # happens when OPUS_PG_ADMIN_PASSWORD is changed after the database volume
  # was first created, because the password is set once, at initialisation.
  case "$last_error" in
    *"password authentication failed"*|*"role \"$ADMIN_USER\" does not exist"*)
      echo "PostgreSQL rejected the administrator login for '$ADMIN_USER'." >&2
      echo "$last_error" >&2
      echo >&2
      echo "OPUS_PG_ADMIN_PASSWORD only takes effect when the database volume" >&2
      echo "is first created; an existing volume keeps its original password." >&2
      echo "Either restore the original value in .env, or discard the database" >&2
      echo "and start again with:  docker compose down -v" >&2
      echo "(docker compose down -v deletes all stored dashboard data.)" >&2
      exit 1
      ;;
  esac
  attempt=$((attempt + 1))
  sleep 2
done
if [ "$connected" -ne 1 ]; then
  echo "Timed out waiting for PostgreSQL at $HOST_NAME:$PORT." >&2
  if [ -n "$last_error" ]; then
    echo "Last connection error: $last_error" >&2
  fi
  exit 1
fi

app_password_b64="$(printf '%s' "$OPUS_DB_APP_PASSWORD" | base64 | tr -d '\n')"
{
  printf "\\set app_password_b64 '%s'\n" "$app_password_b64"
  cat "$DB_DIR/00_bootstrap.psql"
} | psql_admin --dbname postgres --file -

run_migration() {
  echo "Applying $2 migration ($1)..."
  psql_admin --dbname connect_logistics_ops --file "$1"
}

run_numbered_migration() {
  version="$1"
  path="$2"
  label="$3"
  applied="$(psql_admin --dbname connect_logistics_ops --tuples-only --no-align \
    --command "SELECT EXISTS (SELECT 1 FROM audit.schema_migrations WHERE version = '$version');" | tail -n1)"
  if [ "$applied" = "t" ]; then
    echo "Skipped $label migration $version; already applied"
    return 0
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
