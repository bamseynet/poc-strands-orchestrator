# TLS certificate expiring

**Owner:** identity · **Applies to:** auth-service, edge-gateway · **Tags:** tls, certificate, cert, expiry, renewal, acme

## Symptoms
- Certificate monitor warns with fewer than 14 days remaining, or ACME renewal failures in logs.

## Diagnose
1. Run `certctl status <subject>` to read the expiry and last renewal attempt.
2. For a `dns-01` failure, check that the `_acme-challenge` record is delegated correctly.

## Mitigate
1. Fix the DNS delegation and run `certctl renew <subject>`.
2. If renewal cannot be fixed within 48 hours of expiry, issue a manual certificate from `acme-internal-ca`.

## Verify
- `certctl status` shows at least 60 days remaining and clients handshake successfully.
