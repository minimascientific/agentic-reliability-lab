# Nightly integration pipeline failed

**Applies to:** integration-pipeline (synthetic sandbox). Planned for a later increment.

## Symptoms
- The nightly sync run is marked failed in the run log.
- Records are missing from the reporting database.

## Diagnose
1. Read the last run's status and error message.
2. Check whether the source API is reachable and healthy.
3. Count quarantined records and their reason codes.

## Fix
- If the source API was temporarily unavailable and is now healthy: re-run the sync.
  The upserts are idempotent, so a re-run is safe.
- If records failed validation: do not re-run; route to the data owner.

## Escalate when
- The source API is still down.
- More than 5% of records were quarantined.
