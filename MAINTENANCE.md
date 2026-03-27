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
- Agent/architecture/state/maintenance knowledge base.

## Proxy Notes

- The DB proxy record for IPRoyal should be treated as a template, typically including `_country-...`, `_session-...`, and `_lifetime-...` in credentials.
- Runtime session rewriting is centralized in `src/core/proxy_runtime.py`.
- Registration rewrites `_session-...` per task at runtime, then queries `api64.ipify.org` with fallback to `api.ipify.org` through that proxy to log the actual exit IP and retry if it matches the previous registration task's IP.
- CSV-to-CPA monitored recovery rewrites `_session-...` per processed CSV record at runtime, but does not currently perform real-IP verification.
- None of these runtime rewrites persist the rewritten session back into the proxy table.

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
