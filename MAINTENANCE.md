# Maintenance

## Source Of Truth

- Upstream remote: `upstream_dou` -> `https://github.com/dou-jiang/codex-console.git`
- Fork remote: `origin` -> `https://github.com/thomas-coding/codex-console.git`

## Branch Policy

- `main`
  - Reserved for syncing upstream.
  - Keep it aligned with `upstream_dou/main`.
  - Do not mix fork-only feature commits into `main`.
- `develop`
  - Integration branch for this fork's own changes.
  - Custom features, fixes, docs, and maintenance commits go here.

## Standard Workflows

### 1. Sync upstream into `main`

```bash
git switch main
git fetch upstream_dou
git merge --ff-only upstream_dou/main
git push origin main
```

If `--ff-only` fails, stop and inspect why `main` diverged before proceeding.

### 2. Bring upstream changes from `main` into `develop`

```bash
git switch develop
git merge main
git push origin develop
```

Run targeted verification before pushing if the merge touched active subsystems.

### 3. Make fork-specific changes

```bash
git switch develop
# edit / test
git add <files>
git commit -m "<message>"
git push origin develop
```

### 4. Deploy current `develop` runtime to the Ubuntu server

- Active deployment shape:
  - OS: Ubuntu
  - App root: `/opt/codex-console/app`
  - Web UI: `http://<server-ip>:1455`
  - noVNC: `http://<server-ip>:6080`
- Do not store server passwords or live secrets in git.
- Typical deploy flow:

```bash
git switch develop
git push origin develop
# then on server or deploy shell:
cd /opt/codex-console/app
git fetch origin
git checkout develop
git pull --ff-only origin develop
docker compose up -d --build
```

- If old `docker-compose` metadata causes recreate failures mentioning `ContainerConfig`, remove the stale web container/network for this stack and recreate non-interactively instead of trying to patch the old container in place.

## Commit Hygiene

- Do not commit:
  - `csv/`
  - `csvoutput/`
  - `data/`
  - `logs/`
- Those paths may contain live tokens, mailbox passwords, or exported account bundles.
- Avoid destructive git commands in a dirty workspace unless explicitly approved.

## Current Custom Areas On `develop`

- CSV complete export with Outlook recovery fields.
- CSV-to-CPA export and relogin recovery flow.
- Registration-page CSV-to-CPA monitored background export with per-record proxy rotation.
- Outlook recovery metadata duplicated into account records.
- Independent `registered_emails` history table plus startup backfill, so batch Outlook registration skips mailboxes that were previously registered even if the related account row was later deleted.
- Registration-time IPRoyal sticky-session rotation for one-task-one-IP behavior.
- Registration-time real public-IP logging and same-IP retry against the previous registration task.
- Opt-in browser-profile switch for registration only, affecting registration HTTP headers and Sentinel payload while preserving original behavior when disabled.
- Dedicated token-exchange retry setting in registration UI/settings.
- Registration partial-success persistence after `create_account` succeeds, so later CSV/CPA recovery can finish token acquisition if OAuth tail steps fail transiently.
- Account overview cards no longer exclude free/non-paid accounts at query time.
- Agent/architecture/state/maintenance knowledge base.

## Proxy Notes

- The DB proxy record for IPRoyal should be treated as a template, typically including `_country-...`, `_session-...`, and `_lifetime-...` in credentials.
- Runtime session rewriting is centralized in `src/core/proxy_runtime.py`.
- Registration rewrites `_session-...` per task at runtime, then queries `api64.ipify.org` with fallback to `api.ipify.org` through that proxy to log the actual exit IP and retry if it matches the previous registration task's IP.
- CSV-to-CPA monitored recovery rewrites `_session-...` per processed CSV record at runtime, but does not currently perform real-IP verification.
- None of these runtime rewrites persist the rewritten session back into the proxy table.

## Operational Checks

- Registration logs:
  - `logs/app.log`
  - look for `当前真实出口 IP`, `启用浏览器画像`, `OAuth 授权成功`, `注册任务完成`
- Remote settings sanity:

```bash
curl -s http://127.0.0.1:1455/api/settings/registration
```

- Minimum local sanity before push:

```bash
python -m compileall src tests
python -m pytest tests/test_registration_engine.py
```

## Useful Checks

```bash
git status --short
git branch -vv
git remote -v
python -m compileall src tests
```

## When To Read More

- If the task is operational or branch-related, read this file first.
- If the task is about runtime behavior, then jump to `ARCHITECTURE.md` and the relevant module files.
