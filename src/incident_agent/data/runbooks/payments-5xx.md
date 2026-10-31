# Payments API elevated 5xx

**Owner:** payments · **Applies to:** payments-api · **Tags:** 5xx, 503, payments, checkout, errors

## Symptoms
- `POST /v1/charges` returning 502/503, checkout probe failing.

## Diagnose
1. Did a deployment land just before the errors started? Check the `deploy` log lines.
2. Look for `connection pool exhausted`. If present, follow **DB connection exhaustion**.
3. Confirm `orders-db` itself is healthy before blaming it.

## Mitigate
1. If a deployment correlates, roll back: `deployctl rollback payments-api --to previous`.
2. Page `#oncall-payments` if the 5xx rate stays above 5% for 10 minutes after rollback.

## Verify
- 5xx rate below 1% for 15 minutes and the synthetic checkout probe green.
