: "${LUDIUS_DB_PASSWORD:?LUDIUS_DB_PASSWORD must be set and non-empty}"
psql -v ON_ERROR_STOP=1 -v pw="$LUDIUS_DB_PASSWORD" -U "$POSTGRES_USER" -d postgres <<'SQL'
CREATE ROLE ludius LOGIN PASSWORD :'pw';
CREATE DATABASE ludius OWNER ludius;
SQL
