# Maintenance

## Source Of Truth

- Upstream remote: `upstream_dou` -> `https://github.com/dou-jiang/codex-console.git`
- Fork remote: `origin` -> `https://github.com/thomas-coding/codex-console.git`
- Operational runtime:
  - default target is the Ubuntu server deployment, not the local repository runtime.
  - host: `43.153.119.162`
  - container: `app_webui_1`
  - app root in container: `/app`
  - runtime DB: `/app/data/database.db`
  - runtime logs: `/app/logs`
  - Web UI port: `1455`

## Default Operating Rule

- If the user asks to:
  - check logs
  - inspect active registration tasks
  - verify exported/runtime data
  - clear accounts and mailboxes
  - check whether the current deployment is running correctly
- then assume they mean the server runtime unless they explicitly say local.
- Local repo/runtime is for development, patching, compile checks, and preparing deployments.

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

## Fast Operational Checks

### 1. Confirm container status

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep app_webui_1
```

### 2. Tail runtime logs

```bash
docker exec app_webui_1 sh -lc 'tail -n 200 /app/logs/app.log'
```

### 3. Check runtime table counts

```bash
docker exec app_webui_1 python3 - <<'PY'
import sqlite3, json
conn = sqlite3.connect('/app/data/database.db')
cur = conn.cursor()
tables = ['accounts', 'email_services', 'registered_emails', 'registration_tasks', 'bind_card_tasks', 'cpa_services']
print(json.dumps({t: cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}, ensure_ascii=False))
PY
```

## Commit Hygiene

- Do not commit:
  - `csv/`
  - `csvoutput/`
  - `data/`
  - `logs/`
  - `temp/`
  - one-off recovery reports such as `single_account_cpa_recovery_report.json`
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
- Independent account archive snapshots written on account create/update/delete so recovery material survives later account/email cleanup.
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

## Server Data Reset

- When the user asks to "clear accounts and mailboxes" for a fresh registration run on the Ubuntu server, use this exact sequence.
- Scope to clear:
  - `bind_card_tasks`
  - `registration_tasks`
  - `registered_emails`
  - `accounts`
  - `email_services`
- Preserve:
  - `cpa_services`
  - `data/account_archive/`
  - settings, proxies, and other service configuration unless the user explicitly asks to clear them too.

### Safe Procedure

1. Verify there are no active registration tasks:

```bash
cd /opt/codex-console/app
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('data/database.db')
cur = conn.cursor()
rows = cur.execute(
    "SELECT id, task_uuid, status, started_at "
    "FROM registration_tasks "
    "WHERE status NOT IN ('completed','failed','cancelled') "
    "ORDER BY id DESC"
).fetchall()
print(rows)
PY
```

2. Back up the database before deleting anything:

```bash
cd /opt/codex-console/app/data
cp database.db database_before_reset_$(date +%Y%m%d_%H%M%S).db
```

3. Clear the runtime data and reset SQLite autoincrement for those tables only:

```bash
cd /opt/codex-console/app
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('data/database.db')
cur = conn.cursor()
cur.execute('PRAGMA foreign_keys = OFF')
cur.execute('BEGIN')
for table in ['bind_card_tasks', 'registration_tasks', 'registered_emails', 'accounts', 'email_services']:
    cur.execute(f'DELETE FROM {table}')
for table in ['bind_card_tasks', 'registration_tasks', 'registered_emails', 'accounts', 'email_services']:
    try:
        cur.execute('DELETE FROM sqlite_sequence WHERE name=?', (table,))
    except Exception:
        pass
conn.commit()
PY
```

4. Verify counts after reset:

```bash
cd /opt/codex-console/app
python3 - <<'PY'
import sqlite3, json
conn = sqlite3.connect('data/database.db')
cur = conn.cursor()
tables = ['accounts', 'email_services', 'registered_emails', 'registration_tasks', 'bind_card_tasks', 'cpa_services']
print(json.dumps({t: cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}, ensure_ascii=False))
PY
```

### Expected Result

- `accounts = 0`
- `email_services = 0`
- `registered_emails = 0`
- `registration_tasks = 0`
- `bind_card_tasks = 0`
- `cpa_services` should stay unchanged

### Notes

- Do not run this while a batch is still active.
- This is the standard reset flow used on 2026-03-28 before rerunning large Outlook batch-registration tests.
- This reset clears UI/runtime tables only; it does not touch the independent account archive unless explicitly requested.

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
