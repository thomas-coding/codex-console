# Architecture

## Project Map

- Entry points:
  - `webui.py`: CLI entry and uvicorn bootstrap.
  - `src/web/app.py`: FastAPI app assembly.
  - `templates/` and `static/`: Web UI templates and frontend scripts.
- Main modules:
  - `src/core/register.py`: registration/login engine, OTP flow, OAuth callback handling.
  - `src/core/openai/`: OAuth helpers, token refresh, overview fetching.
  - `src/core/upload/`: export/upload adapters, including CPA and CSV-to-CPA logic.
  - `src/services/`: mailbox integrations, especially Outlook providers and OTP polling.
  - `src/web/routes/`: account, registration, email, payment, settings, upload APIs.
  - `src/database/`: SQLAlchemy models, CRUD, DB init.
- Data/storage:
  - Runtime DB defaults to `data/database.db`.
  - Runtime logs default to `logs/app.log`.
  - Local export/import working dirs: `csv/` and `csvoutput/`.

## Key Flows

- Registration/login:
  - Web route starts task.
  - Registration route resolves the proxy once per task and rewrites IPRoyal sticky-session credentials at runtime so each task gets a fresh sticky IP while the task itself stays on one IP.
  - `RegistrationEngine` drives email creation, OTP, login fallback, OAuth callback, token/session capture.
  - Result persists to `accounts`.
- Account export:
  - `src/web/routes/accounts.py` builds JSON/CSV export formats from DB records.
- CSV to CPA:
  - `src/core/upload/csv_cpa.py` parses CSV rows, tries token refresh, validates access token, and falls back to Outlook relogin when CSV contains full recovery material.

## Data Model Notes

- `accounts` is the main account table.
- Sensitive recovery details for Outlook are now duplicated into `accounts.extra_data.outlook_recovery`.
- `email_services` stores configured mailbox providers, but CSV export should not depend on those records remaining present for already-registered Outlook accounts.

## Conventions

- Build/test commands:
  - Install: `uv sync` or `pip install -r requirements.txt`
  - Run UI: `python webui.py`
  - Optional tests: `python -m pytest`
  - Compile sanity check: `python -m compileall src tests`
- Editing rules:
  - Prefer small targeted changes.
  - Use existing route/module boundaries instead of adding parallel subsystems.
- Naming:
  - Route handlers live under `src/web/routes/`.
  - Upload/export helpers live under `src/core/upload/`.
  - New durable account metadata goes in `Account.extra_data` unless a dedicated indexed column is clearly needed.

## Important Decisions

- `main` mirrors upstream state; custom work lands on `develop`.
- Outlook recovery data is duplicated into account records to survive later mailbox deletion.
- CSV-to-CPA is not a raw format conversion; it is a token-recovery flow that may refresh or relogin before emitting CPA JSON.
- IPRoyal proxy records in DB are treated as templates for registration; task startup may rewrite `_session-...` on the fly, but the stored proxy record is not mutated.
- Outlook batch registration should use `concurrency=1` when the goal is "one mailbox, one fresh IP, then next mailbox".

## Known Risks

- OpenAI auth pages and token exchange behavior change frequently.
- Proxy/network instability can still break OAuth callback exchange.
- Local runtime folders contain sensitive data and should remain untracked.
