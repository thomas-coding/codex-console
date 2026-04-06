# Project State

- Date: 2026-04-03
- Active repo: `D:\workspace\code\codex-console-main`
- Branch: `main`
- Working tree: dirty; do not revert unrelated local edits.
- Production host: `43.153.119.162`
- Production app path: `/opt/codex-console/app`
- Production container: `app_webui_1`

## Current Runtime Facts

- The live server is built from `codex-console-main`, not `D:\workspace\code\codex-console`.
- `/opt/codex-console/app/src/config/settings.py` on the server must be treated carefully; a prior overwrite from the wrong repo caused startup failure and was restored from backup.
- `/opt/codex-console/app/data/database.db` was repaired on 2026-04-03 after `app_logs` growth and corruption symptoms; current DB size is back to normal.

## Recent Progress

- Verified email-service deletion failures were caused by `registration_tasks.email_service_id` foreign keys pointing at `email_services.id`.
- Added route-level deletion guard in `src/web/routes/email.py`:
  - block delete with `409` if the service is still referenced by active registration tasks
  - null historical task references before deleting the service
  - apply the same logic to single delete and Outlook batch delete
- Added targeted tests in `tests/test_email_service_delete_routes.py`.
- Deployed a minimal hotfix version of `src/web/routes/email.py` to the server and rebuilt `app_webui_1`.

## Verification

- Local targeted tests passed:
  - `tests/test_email_service_delete_routes.py`
  - `tests/test_email_service_luckmail_routes.py`
- On the running container, direct route invocation confirmed:
  - deleting a service referenced only by completed tasks succeeds and clears `email_service_id`
  - deleting a service referenced by a pending task returns `409`

## Next Cautions

- Deploy from `codex-console-main` only.
- For server hotfixes, prefer minimal file-level changes and back up the remote file first.
- If container recreation hits legacy compose issues, remove `app_webui_1` first and then run `docker-compose up -d --build`.
