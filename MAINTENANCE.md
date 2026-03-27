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
- Outlook recovery metadata duplicated into account records.
- Agent/architecture/state/maintenance knowledge base.

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
