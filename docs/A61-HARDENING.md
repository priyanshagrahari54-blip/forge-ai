# A61 — Security Hardening

Read-only, bounded security audits with honest findings.

## What A61 adds

- `forge/security/hardening.py` — three audits, one report:
  - **policy**: rule inventory (counts per effect, deny-by-default
    flag) plus structural findings (e.g. terminal ALLOW rules that
    fail to pin executable and exact args — defense in depth on top
    of constructor validation);
  - **sessions**: prunes expired sessions and counts active ones,
    flags an advisory bound;
  - **secrets**: bounded scan of every project repository for
    private-key / AWS-key / GitHub-token patterns — reports file,
    pattern, and line only, never the value.
- Control plane `hardening_report` (audited); API
  `GET /api/v1/hardening/report`. The report can only inform; it
  changes nothing by itself.

## Security notes

- Findings are structured and value-free; scanning is bounded
  (500 files / 256 KB per project, 20 hits). The audits add a
  second, independent line of defense on top of existing validation.

## Testing

`tests/test_a61_hardening.py` (5): clean plane reports ok with the
full inventory, planted secrets are flagged without exposing
values, unpinned terminal rules are flagged by the audit,
deny-by-default is reported, API flow.

A61 result: **5 new tests; full suite 1333 passed, 2 skipped** (A60
baseline: 1328 passed, 2 skipped).
