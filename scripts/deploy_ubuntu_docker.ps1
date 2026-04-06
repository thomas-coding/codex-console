param(
    [string]$Host = "43.153.119.162",
    [string]$User = "root",
    [string]$Password,
    [string]$RemoteBaseDir = "/opt/codex-console",
    [string]$AppDirName = "app",
    [int]$HealthPort = 1455,
    [string]$SourceDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Get-PlaintextPassword {
    param([string]$ExistingPassword)

    if ($ExistingPassword) {
        return $ExistingPassword
    }

    $secure = Read-Host "SSH password for $User@$Host" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [string]$WorkingDirectory
    )

    if ($WorkingDirectory) {
        Push-Location $WorkingDirectory
    }

    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed ($LASTEXITCODE): $FilePath $($ArgumentList -join ' ')"
        }
    }
    finally {
        if ($WorkingDirectory) {
            Pop-Location
        }
    }
}

Require-Command "tar"
Require-Command "plink"
Require-Command "pscp"

if (-not (Test-Path $SourceDir)) {
    throw "Source directory not found: $SourceDir"
}

$plainPassword = Get-PlaintextPassword -ExistingPassword $Password
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$archivePath = Join-Path $env:TEMP "codex-console-deploy-$timestamp.tar.gz"
$remoteDeployDir = "$RemoteBaseDir/deploy_stage"
$remoteArchivePath = "$remoteDeployDir/codex-console-deploy-$timestamp.tar.gz"
$remoteAppDir = "$RemoteBaseDir/$AppDirName"
$plinkPath = (Get-Command "plink").Source
$pscpPath = (Get-Command "pscp").Source
$remoteScriptPath = Join-Path $env:TEMP "codex-console-remote-deploy-$timestamp.sh"

Write-Host "Creating deploy archive from $SourceDir"
if (Test-Path $archivePath) {
    Remove-Item $archivePath -Force
}

$tarArgs = @(
    "-czf", $archivePath,
    "--exclude=.git",
    "--exclude=.github",
    "--exclude=.pytest_cache",
    "--exclude=__pycache__",
    "--exclude=*.pyc",
    "--exclude=.venv",
    "--exclude=data",
    "--exclude=logs",
    "--exclude=csv",
    "--exclude=csvoutput",
    "--exclude=temp",
    "--exclude=tests",
    "--exclude=tests_runtime",
    "--exclude=docs",
    "--exclude=release",
    "--exclude=tags",
    "."
)
Invoke-Checked -FilePath "tar" -ArgumentList $tarArgs -WorkingDirectory $SourceDir

if (-not (Test-Path $archivePath)) {
    throw "Archive was not created: $archivePath"
}

$remoteScript = @"
set -euo pipefail

BASE='$RemoteBaseDir'
APP='$remoteAppDir'
ARCHIVE='$remoteArchivePath'
PORT='$HealthPort'
TS=\$(date +%Y%m%d_%H%M%S)
STAGE="\$BASE/deploy_stage/app_stage_\$TS"
RESTORE_STAGE="\$BASE/deploy_stage/app_restore_\$TS"
BACKUP="\$BASE/backups/app_code_backup_\$TS.tar.gz"

if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE='docker-compose'
else
  COMPOSE='docker compose'
fi

rollback() {
  status=\$?
  echo "DEPLOY_FAILED=1"
  echo "DEPLOY_FAILED_STATUS=\$status"
  if [ -f "\$BACKUP" ]; then
    echo "ROLLBACK_FROM=\$BACKUP"
    rm -rf "\$RESTORE_STAGE"
    mkdir -p "\$RESTORE_STAGE"
    tar -xzf "\$BACKUP" -C "\$RESTORE_STAGE"
    rsync -a --delete \
      --exclude 'data/' \
      --exclude 'logs/' \
      --exclude 'csv/' \
      --exclude 'csvoutput/' \
      --exclude 'temp/' \
      --exclude '.env' \
      "\$RESTORE_STAGE"/ "\$APP"/ || true
    rm -rf "\$RESTORE_STAGE"
    cd "\$APP"
    \$COMPOSE up -d --build || true
  fi
  exit \$status
}

trap rollback ERR

test -d "\$APP"
test -f "\$ARCHIVE"
mkdir -p "\$BASE/backups" "\$BASE/deploy_stage" "\$STAGE"

tar -xzf "\$ARCHIVE" -C "\$STAGE"
find "\$STAGE" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +

tar -czf "\$BACKUP" \
  --exclude='./data' \
  --exclude='./logs' \
  --exclude='./csv' \
  --exclude='./csvoutput' \
  --exclude='./temp' \
  --exclude='./.env' \
  -C "\$APP" .

rsync -a --delete \
  --exclude 'data/' \
  --exclude 'logs/' \
  --exclude 'csv/' \
  --exclude 'csvoutput/' \
  --exclude 'temp/' \
  --exclude '.env' \
  "\$STAGE"/ "\$APP"/

rm -rf "\$STAGE"

cat >"\$APP/.dockerignore" <<'EOF'
.git
.github/
.pytest_cache/
__pycache__/
*.pyc
.venv/
data/
logs/
csv/
csvoutput/
temp/
tests/
tests_runtime/
docs/
release/
tags/
EOF

cd "\$APP"
\$COMPOSE up -d --build

python3 - <<'PY'
import sys
import time
import urllib.error
import urllib.request

port = int("$HealthPort")
url = f"http://127.0.0.1:{port}/api/settings/registration"
last_error = None

for _ in range(20):
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            print(f"HEALTH_STATUS={resp.status}")
            sys.exit(0)
    except urllib.error.HTTPError as exc:
        if exc.code in (200, 401):
            print(f"HEALTH_STATUS={exc.code}")
            sys.exit(0)
        last_error = f"HTTPError({exc.code})"
    except Exception as exc:  # noqa: BLE001
        last_error = repr(exc)
    time.sleep(3)

print(f"HEALTH_STATUS=failed:{last_error}")
sys.exit(1)
PY

echo "DEPLOY_BACKUP=\$BACKUP"
rm -f "\$ARCHIVE"
"@

[System.IO.File]::WriteAllText($remoteScriptPath, $remoteScript.Replace("`r`n", "`n"))

try {
    Write-Host "Ensuring remote deploy directory exists on $Host"
    Invoke-Checked -FilePath $plinkPath -ArgumentList @(
        "-ssh", "-batch", "-pw", $plainPassword,
        "$User@$Host",
        "mkdir -p '$remoteDeployDir'"
    )

    Write-Host "Uploading deploy archive to $Host"
    Invoke-Checked -FilePath $pscpPath -ArgumentList @(
        "-batch", "-pw", $plainPassword,
        $archivePath,
        "$User@${Host}:$remoteArchivePath"
    )

    Write-Host "Running remote deploy script on $Host"
    Invoke-Checked -FilePath $plinkPath -ArgumentList @(
        "-ssh", "-batch", "-pw", $plainPassword,
        "$User@$Host",
        "-m", $remoteScriptPath
    )
}
finally {
    if (Test-Path $archivePath) {
        Remove-Item $archivePath -Force
    }
    if (Test-Path $remoteScriptPath) {
        Remove-Item $remoteScriptPath -Force
    }
}
