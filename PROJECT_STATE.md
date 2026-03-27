# Project State

## Goal

- Keep this fork usable against current OpenAI registration/login flow while preserving a clean upstream-sync path and local account-recovery tooling.

## Current Status

- Branch: `develop`
- Last custom commit: `fda1321 feat: add csv account recovery export flow`
- Upstream sync point on `main`: `8712237`
- Working tree expectation: code/docs clean; local `csv/`, `csvoutput/`, `data/`, `logs/` are ignored and may exist.
- Focus area: registration/login resilience, CSV account recovery, repository maintenance docs.

## Done Recently

- Added CSV-to-CPA export flow with per-account refresh and Outlook relogin fallback.
- Added complete CSV export fields for Outlook recovery material.
- Stored Outlook recovery credentials into `accounts.extra_data.outlook_recovery` so CSV export still works after mailbox-service deletion.
- Added Web UI support for CSV-to-CPA export from file list or upload.
- Separated repository workflow:
  `main` for upstream sync, `develop` for fork-specific work.

## In Progress

- No active code migration in progress.
- Next likely enhancement if requested: fill `expired` in relogin-generated CPA JSON by parsing token expiry.

## Blockers

- Local dev environment may not have `pytest` installed even though `pyproject.toml` declares it under optional dev dependencies.
- OAuth callback token exchange can still hit network/proxy timeouts in unstable environments.

## Next Step

- For new work, start from `agent.md`, then read only the one or two docs relevant to the task.

## Files To Read First

- `agent.md`
- `MAINTENANCE.md`
- `ARCHITECTURE.md`
- `src/core/upload/csv_cpa.py`
- `src/core/register.py`
- `src/web/routes/accounts.py`

## Notes

- Keep this file short. Replace stale bullets instead of appending a running diary.
