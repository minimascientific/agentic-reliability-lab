# Application cannot reach its database

**Applies to:** orders-api and app-db (synthetic sandbox).
**Change type:** restart is a normal change; human approval required.

## Symptoms
- Users report errors or timeouts on the Orders page.
- `orders-api` `/health` returns 503 with `database: unreachable`.
- Application logs show `connection refused` or `could not connect to server`.

## Diagnose
1. Check `orders-api` health. A 503 naming the database points below the application.
2. Check TCP on `app-db` port 5432. Refused means the database process is not listening;
   a timeout points to the network path instead.
3. If TCP to `app-db` succeeds but health still fails, the cause is credentials, connection
   limits or a query problem. Do not restart; escalate to the database team.

## Fix
- If `app-db` is not listening: restart the `app-db` service (action `restart_service`,
  target `app-db`). Restart at most once per 30 minutes.

## Verify
- TCP to `app-db` accepts connections.
- `orders-api` `/health` returns 200 with `database: ok`.

## Escalate when
- The restart does not restore health within 30 seconds.
- The database is reachable but the application still fails.
- More than one restart would be needed.
