#!/bin/bash
# Least privilege: orders-api connects as a role that can only read orders.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE orders_app LOGIN PASSWORD '${ORDERS_APP_PASSWORD}';
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO orders_app;
GRANT SELECT ON orders TO orders_app;
SQL
