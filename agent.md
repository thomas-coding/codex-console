# Agent Entry

Read this file first. Do not scan the whole repository by default.

## What This Repo Is

`codex-console` is a Web UI and automation console for OpenAI account registration, login, token refresh, account export, CSV-to-CPA recovery, and related account-management workflows. This fork tracks upstream fixes while carrying fork-specific registration, proxy, Outlook, CSV, and recovery features.

## Operating Context

- Local repository:
  - source tree for development, review, patching, and build output.
- Remote server:
  - the operational source of truth for runtime verification, logs, database state, and real user testing.
  - default runtime target: Ubuntu host `43.153.119.162`, container `app_webui_1`, app root `/app`, DB `/app/data/database.db`, logs `/app/logs`, Web UI port `1455`.
- Default rule:
  - if the user asks to check logs, task progress, running accounts, mailbox state, or clear runtime data, assume they mean the server deployment unless they explicitly say local.

## Knowledge Layers

1. [PROJECT_STATE.md](/D:/workspace/code/codex-console/PROJECT_STATE.md)
   current branch, active focus, recent custom work, known pending items, and what changed most recently.
2. [ARCHITECTURE.md](/D:/workspace/code/codex-console/ARCHITECTURE.md)
   stable module map, runtime flow, data model, deployment shape, and important design decisions.
3. [MAINTENANCE.md](/D:/workspace/code/codex-console/MAINTENANCE.md)
   server-first operations, branch policy, upstream sync, deployment, resets, and common checks.
4. [README.md](/D:/workspace/code/codex-console/README.md)
   user-facing setup or packaging details only when needed.

## Reading Order By Task

- New session handoff:
  - read `PROJECT_STATE.md`, then `MAINTENANCE.md` if the task is operational, otherwise `ARCHITECTURE.md`.
- Registration, login, token refresh, Outlook, CSV, CPA:
  - read `PROJECT_STATE.md` then `ARCHITECTURE.md`.
- Proxy/IP rotation, rate limiting, browser profile, batch concurrency:
  - read `ARCHITECTURE.md`, then `src/core/proxy_runtime.py`, `src/core/browser_profile.py`, `src/web/routes/registration.py`, `src/core/register.py`.
- CSV export, CSV-to-CPA, account recovery:
  - read `ARCHITECTURE.md`, then `src/web/routes/accounts.py`, `src/core/upload/csv_cpa.py`, `src/database/crud.py`, `src/core/account_archive.py`.
- Deployment, Docker, logs, DB cleanup, server verification:
  - read `MAINTENANCE.md` first.
- Upstream sync, branching, push strategy:
  - read `MAINTENANCE.md`.
- Deep code changes:
  - use the docs above to narrow the target files, then open only the directly relevant modules.

## First Checks For The Next AI

1. Read `PROJECT_STATE.md`.
2. Run `git status --short`.
3. If the request is operational, go to `MAINTENANCE.md` and use the server runtime, not the local DB.
4. If the request is code behavior, go to `ARCHITECTURE.md` and then open only the mapped modules.

## Guardrails

- `main` is reserved for syncing upstream.
- `develop` is the integration branch for this fork's own changes.
- Never commit local account exports or runtime data. `csv/`, `csvoutput/`, `data/`, `logs/`, `temp/`, and ad hoc report files are local-only.
- Treat real tokens, passwords, and mailbox credentials as sensitive.
- Do not assume local DB state reflects production use; verify on the server container first.
