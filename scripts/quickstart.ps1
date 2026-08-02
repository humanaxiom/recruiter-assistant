#Requires -Version 5.1
<#
.SYNOPSIS
    One-command quickstart: boot the whole recruiter-assistant stack in Docker,
    on UNIQUE host ports so it never collides with other apps on this machine.

.DESCRIPTION
    Automates the README "Quick start":
      1. Verifies Docker is running.
      2. Ensures a valid .env exists:
         - generates the REQUIRED PII_KEY / SKILL_HASH_SALT secrets if missing
           or blank (32 random bytes, base64, via the .NET CSPRNG — no openssl);
         - writes the project's UNIQUE host-port block (29xxx) if absent, so the
           stack never fights another app for 5432/6379/7474/7687/8000/5000.
           (docker-compose.yml reads these as ${X_PORT:-<stock>}; only the HOST
           side changes — in-network service DSNs are unchanged.)
      3. (Best-effort) checks Ollama is reachable on host metal with the two
         required models — inference is HOST-ONLY, so parsing/ranking stall
         without it, but the stack still boots.
      4. Preflights every required host port and fails with a clear
         "port N is held by <container>" message (not a raw Docker bind error)
         if a FOREIGN process already owns one.
      5. `docker compose up -d` for postgres · neo4j · redis · api · worker ·
         frontend. Schema + Neo4j vector indexes are created on API startup.
      6. Waits for the data tier + API /health to go green, then prints the URLs
         on their resolved ports.

    Offline-only by design: LLM_BASE_URL points at local Ollama; no candidate
    data ever leaves the machine.

.PARAMETER Build      Force a rebuild of the api/worker/frontend images (--build).
.PARAMETER NoCas      Boot WITHOUT CAS (dev-anonymous admin, no login screen).
                      CAS (SFU login + RBAC + user management) is ON by default.
.PARAMETER LiveEval   Also apply compose.live-eval.yml (Tailscale peer), if present.
.PARAMETER Down       Stop and remove the stack instead of starting it.
.PARAMETER Reset      With -Down, also delete the pg/neo4j volumes (down -v). DESTROYS data.
.PARAMETER Logs       After starting, follow the combined container logs.
.PARAMETER TimeoutSeconds  How long to wait for health. Default 180.

.EXAMPLE
    ./scripts/quickstart.ps1
.EXAMPLE
    ./scripts/quickstart.ps1 -Build -Logs
.EXAMPLE
    ./scripts/quickstart.ps1 -Down -Reset
#>
[CmdletBinding()]
param(
    [switch] $Build,
    [switch] $NoCas,
    [switch] $LiveEval,
    [switch] $Down,
    [switch] $Reset,
    [switch] $Logs,
    [int]    $TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ── helpers ──────────────────────────────────────────────────────────────────
function Write-Step($msg)  { Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  ✔ $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

function New-Base64Secret {
    # 32 cryptographically-random bytes, base64 — same shape as `openssl rand -base64 32`.
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes)
}

function Get-EnvValue($lines, $key, $default) {
    $line = $lines | Where-Object { $_ -match "^\s*$key\s*=" } | Select-Object -First 1
    if ($line) {
        $v = ($line -replace "^\s*$key\s*=", '').Trim()
        if (-not [string]::IsNullOrWhiteSpace($v)) { return $v }
    }
    return $default
}

# UNIQUE host-port scheme for this project (see .env.example / the unique-host-ports
# standing order). Only the HOST side; in-network ports stay stock.
$PortVars = [ordered]@{
    API_PORT        = 29800
    FRONTEND_PORT   = 29500
    POSTGRES_PORT   = 29432
    REDIS_PORT      = 29379
    NEO4J_HTTP_PORT = 29474
    NEO4J_BOLT_PORT = 29687
}

# Repo root = parent of this script's directory (scripts/quickstart.ps1).
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# ── docker-compose file set ──────────────────────────────────────────────────
# CAS (SFU login + RBAC + user management) is ON BY DEFAULT — the app is meant
# to run authenticated. Pass -NoCas to boot the dev-anonymous-admin passthrough.
$ComposeArgs = @('-f', 'docker-compose.yml')
$CasOn = $false
if (-not $NoCas) {
    if (Test-Path 'compose.cas.yml') { $ComposeArgs += @('-f', 'compose.cas.yml'); $CasOn = $true }
    else { Write-Warn2 'compose.cas.yml not found — booting WITHOUT CAS (dev-anonymous admin).' }
} else {
    Write-Warn2 'CAS disabled via -NoCas — dev-anonymous admin, no login screen.'
}
if ($LiveEval) {
    if (Test-Path 'compose.live-eval.yml') { $ComposeArgs += @('-f', 'compose.live-eval.yml') }
    else { Write-Warn2 'compose.live-eval.yml not found — ignoring -LiveEval.' }
}

# ── 0. Docker up? ────────────────────────────────────────────────────────────
Write-Step 'Checking Docker'
try {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw 'docker info failed' }
} catch {
    Write-Error 'Docker does not appear to be running. Start Docker Desktop and retry.'
    exit 1
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Write-Error 'Docker Compose v2 ("docker compose") is required.'; exit 1 }
Write-Ok 'Docker is running.'

# ── Tear-down path ───────────────────────────────────────────────────────────
if ($Down) {
    Write-Step 'Stopping the stack'
    if ($Reset) {
        Write-Warn2 'Reset requested — this DELETES the Postgres/Neo4j volumes.'
        docker compose @ComposeArgs down -v
    } else {
        docker compose @ComposeArgs down
    }
    Write-Ok 'Stack stopped.'
    return
}

# ── 1. .env — required secrets + unique host ports ───────────────────────────
Write-Step 'Checking .env (secrets + unique host ports)'
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Ok 'Created .env from .env.example.'
}

$envLines = @(Get-Content '.env')
$changed = $false

# Secrets: generate if missing OR blank (compose hard-fails on `${PII_KEY:?...}`).
foreach ($key in @('PII_KEY', 'SKILL_HASH_SALT')) {
    $line = $envLines | Where-Object { $_ -match "^\s*$key\s*=" } | Select-Object -First 1
    $value = if ($line) { ($line -replace "^\s*$key\s*=", '').Trim() } else { $null }
    if ([string]::IsNullOrWhiteSpace($value)) {
        $secret = New-Base64Secret
        if ($line) {
            $envLines = $envLines | ForEach-Object { if ($_ -match "^\s*$key\s*=") { "$key=$secret" } else { $_ } }
        } else { $envLines += "$key=$secret" }
        $changed = $true
        Write-Ok "Generated a random $key (32 bytes, base64)."
    }
}

# Host ports: write the unique default only if the key is ENTIRELY ABSENT
# (respect any value the user has already chosen).
foreach ($key in $PortVars.Keys) {
    $has = $envLines | Where-Object { $_ -match "^\s*$key\s*=" }
    if (-not $has) {
        $envLines += "$key=$($PortVars[$key])"
        $changed = $true
        Write-Ok "Set $key=$($PortVars[$key]) (unique host port)."
    }
}

if ($changed) {
    Set-Content -Path '.env' -Value $envLines -Encoding ASCII
    Write-Warn2 '.env holds secrets — it is gitignored; never commit it. Losing PII_KEY makes encrypted columns unrecoverable.'
} else {
    Write-Ok 'Secrets and host ports already set.'
}

# Resolve the ports actually in effect (user override in .env wins over the default).
$envLines     = @(Get-Content '.env')
$apiPort      = [int](Get-EnvValue $envLines 'API_PORT'        $PortVars['API_PORT'])
$frontendPort = [int](Get-EnvValue $envLines 'FRONTEND_PORT'   $PortVars['FRONTEND_PORT'])
$neo4jHttp    = [int](Get-EnvValue $envLines 'NEO4J_HTTP_PORT' $PortVars['NEO4J_HTTP_PORT'])
$resolved = [ordered]@{
    'api'          = $apiPort
    'frontend'     = $frontendPort
    'postgres'     = [int](Get-EnvValue $envLines 'POSTGRES_PORT'   $PortVars['POSTGRES_PORT'])
    'redis'        = [int](Get-EnvValue $envLines 'REDIS_PORT'      $PortVars['REDIS_PORT'])
    'neo4j-http'   = $neo4jHttp
    'neo4j-bolt'   = [int](Get-EnvValue $envLines 'NEO4J_BOLT_PORT' $PortVars['NEO4J_BOLT_PORT'])
}

# The filesystem BlobStore bind-mounts ./data.
if (-not (Test-Path 'data')) { New-Item -ItemType Directory -Path 'data' | Out-Null }

# ── 2. Ollama on host (best-effort) ──────────────────────────────────────────
Write-Step 'Checking Ollama on host metal (localhost:11434)'
$needModels = @('gpt-oss:20b', 'nomic-embed-text')
try {
    $tags = Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 4
    $have = @($tags.models | ForEach-Object { $_.name })
    $missing = $needModels | Where-Object { $m = $_; -not ($have | Where-Object { $_ -like "$m*" }) }
    if ($missing) {
        Write-Warn2 "Ollama is up but missing model(s): $($missing -join ', ')."
        Write-Warn2 "Pull them:  ollama pull $($missing -join ' ')"
    } else { Write-Ok 'Ollama reachable with both required models.' }
} catch {
    Write-Warn2 'Ollama not reachable on localhost:11434 — the stack will boot, but parsing/ranking need it.'
    Write-Warn2 'Start it:  ollama serve   then   ollama pull gpt-oss:20b nomic-embed-text'
}

# ── 3. Port preflight — clear message instead of a raw Docker bind error ──────
Write-Step 'Preflighting host ports'
$conflict = $false
foreach ($svc in $resolved.Keys) {
    $port = $resolved[$svc]
    $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        $holder = (docker ps --filter "publish=$port" --format '{{.Names}}' 2>$null | Select-Object -First 1)
        if ($holder -and $holder -like 'recruiter-assistant-*') {
            # Our own already-running container — up -d will reconcile it, not a conflict.
            continue
        }
        $by = if ($holder) { " by container '$holder'" } else { ' (non-Docker process)' }
        $var = ($PortVars.Keys | Where-Object { $resolved[$svc] -eq $PortVars[$_] } | Select-Object -First 1)
        if (-not $var) { $var = '<the matching *_PORT>' }
        Write-Warn2 ("Host port {0} ({1}) is already in use{2}. Change {3} in .env to a free port." -f $port, $svc, $by, $var)
        $conflict = $true
    }
}
if ($conflict) { Write-Error 'Resolve the port conflict(s) above (edit .env), then re-run.'; exit 1 }
Write-Ok 'All host ports free.'

# ── 4. Bring up the stack ────────────────────────────────────────────────────
Write-Step 'Starting containers (postgres · neo4j · redis · api · worker · frontend)'
$upArgs = @('up', '-d')
if ($Build) { $upArgs += '--build' }
docker compose @ComposeArgs @upArgs
if ($LASTEXITCODE -ne 0) { Write-Error 'docker compose up failed — see the output above.'; exit 1 }

# ── 5. Wait for health ───────────────────────────────────────────────────────
Write-Step "Waiting for the stack to become healthy (up to ${TimeoutSeconds}s)"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$apiOk = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:$apiPort/health" -TimeoutSec 3
        $status = if ($resp.PSObject.Properties.Name -contains 'status') { $resp.status } else { "$resp" }
        if ($status -eq 'ok') { $apiOk = $true; break }
    } catch { }
    Start-Sleep -Seconds 3
}

Write-Host ''
docker compose @ComposeArgs ps

if ($apiOk) {
    Write-Host ''
    Write-Ok 'Stack is up.'
    Write-Host ''
    Write-Host ("  Frontend (recruiter UI) : http://localhost:{0}" -f $frontendPort) -ForegroundColor White
    Write-Host ("  API                     : http://localhost:{0}   (/health, /docs)" -f $apiPort) -ForegroundColor White
    Write-Host ("  Neo4j browser           : http://localhost:{0}   (neo4j / recruiterpass)" -f $neo4jHttp) -ForegroundColor White
    Write-Host ''
    if ($CasOn) {
        Write-Host ("  CAS login is ON — the browser will redirect to SFU CAS; first login as the" ) -ForegroundColor White
        Write-Host ("  default admin lands you as admin (RBAC + /admin/users). Boot with -NoCas to skip.") -ForegroundColor White
        Write-Host ''
    } else {
        Write-Warn2 'CAS is OFF (dev-anonymous admin) — no login, no user management UI. Drop -NoCas to enable.'
        Write-Host ''
    }
    Write-Host '  Logs : docker compose logs -f            Stop : ./scripts/quickstart.ps1 -Down' -ForegroundColor DarkGray
} else {
    Write-Warn2 "API /health did not go green within ${TimeoutSeconds}s. Inspect with:"
    Write-Host  '     docker compose logs api worker' -ForegroundColor DarkGray
    exit 1
}

# ── 6. Optional log follow ───────────────────────────────────────────────────
if ($Logs) {
    Write-Step 'Following logs (Ctrl-C detaches; the stack keeps running)'
    docker compose @ComposeArgs logs -f
}
