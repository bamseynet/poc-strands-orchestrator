# Planned database maintenance

**Owner:** data-platform · **Applies to:** orders-db · **Tags:** maintenance, change, upgrade, read-only, announcement, planned

## Before
1. Announce at least 48 hours ahead to every team with a direct or transitive dependency.
2. Pause `finance-eod-export` for the duration of the window.

## During
1. Set read-only mode, upgrade, then run the smoke tests.

## After
1. Confirm replication has caught up, then post the completion note.
