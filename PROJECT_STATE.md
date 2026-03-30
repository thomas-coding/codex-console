# Project State

## Goal

- Keep this fork usable against current OpenAI registration/login changes while preserving server-first verification and CPA-ready account export.

## Current Status

- Branch: `develop`
- Remote tracking branch: `origin/develop`
- Upstream sync point on local `main`: `8712237`
- Upstream note:
  `upstream_dou/main` has moved to `af66d98`, but that newer upstream state has not been merged locally yet.
- Working tree status:
  active code/doc updates exist around registration/browser-first flow, Outlook batch reservation, and DB pool/logging throughput; local reference files `http_client.py` and root `register.py` are intentionally untracked and should not be committed by accident.
- Server runtime last verified on `2026-03-30`:
  host `43.153.119.162`, app path `/opt/codex-console/app`, container `app_webui_1`; runtime deployment is done by file copy + `docker cp` + container restart, not by rebuilding images.

## Done Recently

- Hardened `src/core/register.py` for the newer OpenAI registration/login flow:
  cache `create_account` continue_url, handle `about-you` / `add-phone` gates, prefer OTP callback/session reuse, and only fall back to legacy relogin when needed.
- Added Browser First registration settings and Web UI controls:
  `registration.browser_first_enabled`, `registration.browser_headless`, `registration.browser_persistent_profile_dir`.
- Updated `get_settings()` so DB-backed settings reload cleanly once database initialization becomes ready; this avoids stale defaults during startup and tests.
- Fixed generic Outlook batch mailbox races:
  batch startup now prebinds unique `email_service_id`s, and worker threads honor task-bound Outlook mailboxes instead of rescanning the full mailbox pool.
- Live-validated server runtime:
  `about-you` / `add-phone` can still appear, but successful runs now reached full `access_token`, `refresh_token`, `session_token`, `account_id`, and `workspace_id`.
- Stabilized large-batch server runtime:
  SQLite connection pool defaults are now expanded (`pool_size=64`, `max_overflow=128`, `pool_timeout=120`) and DB log capture is reduced to `warning/error` to avoid saturating the pool during heavy registration batches.

## In Progress

- Operational risk still exists around proxy/TLS instability during callback/session capture; that is now a bigger bottleneck than mailbox selection or DB pool size.
- If requested later, bulk handling for `401` / expired accounts still needs a dedicated validation or refresh workflow.

## Blockers

- OpenAI auth pages and proxy behavior continue to change without notice.
- Local reference files `http_client.py` and root `register.py` are analysis helpers only; do not deploy or commit them unless explicitly requested.

## Next Step

- For operational issues, inspect the server container and server DB first.
- For code issues, start from Outlook batch reservation + Browser First registration flow, then inspect DB pool/logging only if batch throughput regresses again.

## Files To Read First

- `agent.md`
- `PROJECT_STATE.md`
- `ARCHITECTURE.md`
- `MAINTENANCE.md`
- `src/core/register.py`
- `src/web/routes/registration.py`
- `src/database/session.py`
- `webui.py`
- `src/config/settings.py`
- `src/web/routes/settings.py`
- `tests/test_registration_engine.py`
- `tests/test_registration_routes.py`
- `tests/test_settings_reload.py`

## Notes

- Keep this file compact and replace stale bullets instead of appending history.
- Server-first runtime verification remains the default operating rule for this repository.
