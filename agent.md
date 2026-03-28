# Agent Entry

Read this file first. Do not scan the whole repository by default.

## What This Repo Is

`codex-console` is a Web UI and automation console for OpenAI account registration, login, token refresh, account export, and related account-management workflows. This fork tracks upstream fixes while carrying custom account-management and CSV/CPA recovery features.

## Reading Order

1. Read [PROJECT_STATE.md](/D:/workspace/code/codex-console/PROJECT_STATE.md) for current branch, recent work, and what changed lately.
2. Read [ARCHITECTURE.md](/D:/workspace/code/codex-console/ARCHITECTURE.md) for stable structure, entry points, and module map.
3. Read [MAINTENANCE.md](/D:/workspace/code/codex-console/MAINTENANCE.md) when the task involves branches, syncing upstream, releases, or repository hygiene.
4. Read [README.md](/D:/workspace/code/codex-console/README.md) only when you need user-facing setup or packaging details.

## Task Routing

- Startup, local run, environment questions:
  Read `PROJECT_STATE.md` then `README.md`.
- Deployment, Docker, server sync, or remote troubleshooting:
  Read `MAINTENANCE.md` first.
- Registration, login, token, Outlook, CSV, CPA tasks:
  Read `PROJECT_STATE.md` then `ARCHITECTURE.md`.
- Registration hardening, proxy/session/IP rotation, browser-profile switch:
  Read `ARCHITECTURE.md`, then `src/web/routes/registration.py`, `src/core/proxy_runtime.py`, `src/core/browser_profile.py`, and `src/core/register.py`.
- Proxy/IP rotation or rate-limit investigations:
  Read `ARCHITECTURE.md`, then `src/core/proxy_runtime.py`, then `src/web/routes/registration.py`.
- Branch sync, upstream merge, release, commit hygiene:
  Read `MAINTENANCE.md`.
- Deep code changes:
  Use the docs above to narrow the file set, then open only the directly relevant modules.

## Guardrails

- `main` is reserved for syncing upstream.
- `develop` is the integration branch for this fork's own changes.
- Never commit local account exports or runtime data. `csv/`, `csvoutput/`, `data/`, and `logs/` are local-only.
- Treat real tokens, passwords, and mailbox credentials as sensitive.
