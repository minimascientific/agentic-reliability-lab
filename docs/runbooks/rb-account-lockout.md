# User account locked out

**Applies to:** iam service (synthetic sandbox). Planned for a later increment.

## Symptoms
- A user cannot sign in and sees "account locked".

## Diagnose
1. Look up the account's lock status and failed sign-in count.
2. Check whether the failed attempts came from the user's usual locations.

## Fix
- Unlock the account only after a human approves and the requester's identity is verified.

## Escalate when
- Failed attempts come from unfamiliar locations (possible attack).
- The account is disabled rather than locked.
