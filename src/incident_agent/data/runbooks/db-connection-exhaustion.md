# DB connection pool exhaustion

**Owner:** data-platform · **Applies to:** payments-api, inventory-api, orders-db · **Tags:** database, pool, connections, timeout, exhausted

## Symptoms
- Clients log `connection pool exhausted` with `idle=0` and a growing `waiting` count.

## Diagnose
1. Compare the client's configured `db.pool.max_size` with its normal value (payments-api: 50).
2. Compare with `orders-db` `connections active` and `max`. If the database has spare capacity, the fault is client-side configuration, not the database.

## Mitigate
1. Restore the pool size, or roll back the release that changed it.
2. Do **not** restart `orders-db`. A restart turns a partial outage into a full one.

## Verify
- `waiting` returns to 0 and no new `connection pool exhausted` lines for 10 minutes.
