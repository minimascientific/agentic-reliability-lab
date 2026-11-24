#!/bin/bash
# The agent connects as arl_agent: it may add audit events but never edit or delete them.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE arl_agent LOGIN PASSWORD '${ARL_AGENT_DB_PASSWORD}';
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO arl_agent;
GRANT USAGE, CREATE ON SCHEMA ops TO arl_agent;          -- CREATE: LangGraph checkpoint tables
GRANT CREATE ON SCHEMA public TO arl_agent;
GRANT SELECT, INSERT ON ops.audit_event TO arl_agent;     -- no UPDATE, no DELETE
GRANT SELECT, INSERT ON ops.approval TO arl_agent;
GRANT UPDATE (consumed_at) ON ops.approval TO arl_agent;  -- only the single-use marker
SQL
