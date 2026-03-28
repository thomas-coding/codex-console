# Project State

## Goal

- Keep this fork usable against current OpenAI registration/login flow while preserving a clean upstream-sync path and local account-recovery tooling.

## Current Status

- Branch: `develop`
- Upstream sync point on `main`: `8712237`
- Working tree expectation: code/docs clean; local `csv/`, `csvoutput/`, `data/`, `logs/` are ignored and may exist.
- Focus area: registration/login resilience, CSV account recovery, repository maintenance docs.
- Runtime status last verified on 2026-03-28:
  local `develop` and server runtime were aligned for active runtime files; `browser_profile_enabled=true`; `registration.token_exchange_max_retries=5` on the server.

## Done Recently

- Added CSV-to-CPA export flow with per-account refresh and Outlook relogin fallback.
- Added complete CSV export fields for Outlook recovery material.
- Stored Outlook recovery credentials into `accounts.extra_data.outlook_recovery` so CSV export still works after mailbox-service deletion.
- Added Web UI support for CSV-to-CPA export from file list or upload.
- Added registration-time IPRoyal sticky-session rewriting so each registration task gets a fresh proxy session/IP while keeping a stable IP inside the task.
- Added registration-page CSV-to-CPA background task mode with live monitor, completion download, and per-record proxy session rotation.
- Added registration-time public-IP probing through the active proxy and same-IP retry logic so consecutive registration tasks try to avoid reusing the previous task's real exit IP.
- Added independent `registered_emails` history so Outlook mailboxes that have ever registered or been confirmed as pre-existing can be skipped even if the related `accounts` row is later deleted.
- Confirmed Outlook batch registration can run sequentially with `skip_registered=true`, consuming one Outlook mailbox per task without reusing already-registered mailboxes.
- Added opt-in `registration_browser_profile_enabled` switch for registration only; when enabled it injects a lightweight browser profile into registration HTTP headers and Sentinel payload, and when disabled it stays on the original code path.
- Added dedicated `registration.token_exchange_max_retries` so OAuth callback token exchange can retry independently from the main registration retry count.
- Relaxed IP geolocation failure handling: geo lookup failures now continue as "unknown region" instead of aborting registration early.
- Added registration partial-success persistence:
  once `create_account` has succeeded, later OAuth/token tail failures can still save the account/password/outlook recovery material and `registered_emails` history for later CSV/CPA recovery.
- Adjusted verification-code retry logic:
  if OTP submission fails due to network timeout/error, retry the same newest code before assuming it is stale.
- Updated account-overview cards to include all accounts instead of filtering to paid plans only.
- Separated repository workflow:
  `main` for upstream sync, `develop` for fork-specific work.

## In Progress

- No active code migration in progress.
- Pending: evaluate a batch-registration "exclusive IP leasing" scheduler so `pipeline + concurrency=N` can behave like `N` parallel lanes, each registration task acquiring a distinct real exit IP before starting while keeping `concurrency=1` behavior compatible.
- Next likely enhancement if requested: fill `expired` in relogin-generated CPA JSON by parsing token expiry.

## Blockers

- Local dev environment may not have `pytest` installed even though `pyproject.toml` declares it under optional dev dependencies.
- OAuth callback token exchange can still hit network/proxy timeouts in unstable environments.
- Even with sticky-session rotation, different sessions can still occasionally map to the same real exit IP at the proxy provider.

## Next Step

- For new work, start from `agent.md`, then read only the one or two docs relevant to the task.

## Files To Read First

- `agent.md`
- `MAINTENANCE.md`
- `ARCHITECTURE.md`
- `src/core/proxy_runtime.py`
- `src/core/browser_profile.py`
- `src/web/routes/registration.py`
- `src/core/upload/csv_cpa.py`
- `src/core/register.py`
- `src/database/crud.py`
- `src/database/session.py`
- `src/web/routes/accounts.py`
- `src/web/routes/settings.py`
- `src/core/http_client.py`
- `static/js/app.js`
- `static/js/settings.js`
- `templates/index.html`
- `templates/settings.html`

## Notes

- Keep this file short. Replace stale bullets instead of appending a running diary.
