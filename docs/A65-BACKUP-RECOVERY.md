# A65 — Backup & Recovery

Verified snapshots, honest drift detection, real restore.

## What A65 adds

- `forge/backup/manager.py` — backup lifecycle persisted in the
  plane database:
  - `create`: consistent database snapshot via SQLite's online
    backup API plus bounded project file contents (≤1000 files,
    ≤50 MB), all hashed into a manifest, zipped beside the
    database; ≤5 backups, oldest pruned;
  - `verify`: re-opens the archive and compares it against the live
    world — database hash byte-for-byte, files hash-for-hash — and
    reports drift (changed/missing) honestly;
  - `restore`: requires a stopped plane, checks the snapshot hash,
    consolidates the WAL, rewrites the database file atomically,
    and says plainly that file-tree rollback is out of scope.
- Control plane `backup_create/verify/restore/list/get` (audited);
  API at `/api/v1/backups*`.

## Security notes

- Restores rewrite plane state only and never touch project
  working files; the stopped-plane requirement prevents
  torn-state restores; the archive is hash-checked before it is
  trusted.

## Testing

`tests/test_a65_backup_recovery.py` (5): archive contents (db +
manifest + project files), clean verification and honest drift
detection on changed/deleted files, the 5-backup cap pruning the
oldest, restore requiring a stopped plane and provably restoring
the snapshot (fresh plane sees only pre-backup state), API flow.

A65 result: **5 new tests; full suite 1353 passed, 2 skipped** (A64
baseline: 1348 passed, 2 skipped).
