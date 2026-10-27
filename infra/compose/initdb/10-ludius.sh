# Runs once, when the pgdata volume is first created. The Ludius store gets its own role and database;
# Temporal's databases are made by temporal-setup. Sourced or executed by the postgres entrypoint.
# Fail closed before psql if the password is unset or empty (compose already refuses, this covers other callers).
: "${LUDIUS_DB_PASSWORD:?LUDIUS_DB_PASSWORD must be set and non-empty}"
psql -v ON_ERROR_STOP=1 -v pw="$LUDIUS_DB_PASSWORD" -U "$POSTGRES_USER" -d postgres <<'SQL'
CREATE ROLE ludius LOGIN PASSWORD :'pw';
CREATE DATABASE ludius OWNER ludius;
SQL
