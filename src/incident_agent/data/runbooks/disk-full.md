# Host disk filling or full

**Owner:** platform-compute · **Applies to:** any host · **Tags:** disk, volume, space, spool, batch, no space left

## Symptoms
- `No space left on device`, or a volume above 90%.

## Diagnose
1. Find the largest paths: `du -xh /var/spool | sort -h | tail`.
2. Check whether the retention or cleanup task is running.

## Mitigate
1. Delete export files older than the 7-day retention window. Do not touch in-flight files.
2. Re-enable the retention cron if it has been disabled.
3. Re-run the failed job once the volume is below 70%.

## Verify
- Volume below 70% and the failed job completes.
