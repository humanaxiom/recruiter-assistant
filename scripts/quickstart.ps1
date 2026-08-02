#Requires -Version 5.1
<#
.SYNOPSIS
    One-command quickstart: boot the whole recruiter-assistant stack in Docker.

.DESCRIPTION
    Automates the README "Quick start":
      1. Verifies Docker is running.
      2. Ensures a valid .env exists (generates the REQUIRED PII_KEY /
         SKILL_HASH_SALT secrets if they are missing or blank — 32 random
         bytes, base64, via the .NET CSPRNG, so no openssl is needed).
      3. (Best-effort) checks that Ollama is reachable on host metal with the
         two required models pulled — inference is HOST-ONLY, so parsing and
         ranking will stall without it, but the stack itself still boots.
      4. `docker compose up -d` for postgres · neo4j · redis · api · worker ·
         frontend. Postgres tables + Neo4j vector indexes are created on API
         startup (no separate migration step).
      5. Waits for the data tier to report healthy and the API /health to go
         green, then prints the URLs.

    Offline-only by design: LLM_BASE_URL points at local Ollama; no candidate
    data ever leaves the machine.

.PARAMETER Build
    Force a rebuild of the api/worker/frontend images (`--build`). Use after
    changing the Dockerfile or requirements.

.PARAMETER Cas
    Also apply compose.cas.yml (SFU CAS login override), if present.

.PARAMETER LiveEval
    Also apply compose.live-eval.yml (points inference at the Tailscale peer
    instead of localhost), if present.

.PARAMETER Down
    Stop and remove the stack (`docker compose down`) instead of starting it.

.PARAMETER Reset
    With -Down, also delete the pg/neo4j volumes (`down -v`) — DESTROYS all
    local data. Ignored without -Down.

.PARAMETER Logs
    After starting, follow the combined container logs (Ctrl-C to detach; the
    stack keeps running).

.PARAMETER TimeoutSeconds
    How long to wait for the stack to become healthy. Default 180.

.EXAMPLE
    ./scripts/quickstart.ps1
    Boot everything with the images already built.

.EXAMPLE
    ./scripts/quickstart.ps1 -Build -Logs
    Rebuild the app images, boot, then tail logs.

.EXAMPLE
    ./scripts/quickstart.ps1 -Down -Reset
    Tear the stack down and wipe the Postgres/Neo4j volumes.
#>
[CmdletBinding()]
param(
    [switch] $Build,
    [switch] $Cas,
    [switch] $LiveEval,
    [switch] $Down,
    [switch] $Reset,
    [switch] $Logs,
    [int]    $TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ── helpers ──────────────────────────────────────────────────────────────────
function Write-Step($msg) { Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  ✔ $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

function New-Base64Secret {
    # 32 cryptographically-random bytes, base64 — same shape as `openssl rand -base64 32`.
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes)
}

# Repo root = parent of this script's directory (scripts/quickstart.ps1).
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# ── docker-compose file set ──────────────────────────────────────────────────
$ComposeArgs = @('-f', 'docker-compose.yml')
if ($Cas) {
    if (Test-Path 'compose.cas.yml') { $ComposeArgs += @('-f', 'compose.cas.yml') }
    else { Write-Warn2 'compose.cas.yml not found — ignoring -Cas.' }
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
# Compose v2 ("docker compose") is expected; fail clearly if only v1 exists.
docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Docker Compose v2 ("docker compose") is required.'
    exit 1
}
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

# ── 1. .env with required secrets ────────────────────────────────────────────
Write-Step 'Checking .env'
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Ok 'Created .env from .env.example.'
}

# Ensure PII_KEY and SKILL_HASH_SALT are present AND non-blank (the compose
# file hard-fails with `${PII_KEY:?...}` if either is empty).
$envLines = Get-Content '.env'
$changed = $false
foreach ($key in @('PII_KEY', 'SKILL_HASH_SALT')) {
    $line = $envLines | Where-Object { $_ -match "^\s*$key\s*=" } | Select-Object -First 1
    $value = $null
    if ($line) { $value = ($line -replace "^\s*$key\s*=", '').Trim() }
    if ([string]::IsNullOrWhiteSpace($value)) {
        $secret = New-Base64Secret
        if ($line) {
            $envLines = $envLines | ForEach-Object {
                if ($_ -match "^\s*$key\s*=") { "$key=$secret" } else { $_ }
            }
        } else {
            $envLines += "$key=$secret"
        }
        $changed = $true
        Write-Ok "Generated a random $key (32 bytes, base64)."
    }
}
if ($changed) {
    Set-Content -Path '.env' -Value $envLines -Encoding ASCII
    Write-Warn2 '.env now holds secrets — it is gitignored; never commit it. Losing PII_KEY makes encrypted columns unrecoverable.'
} else {
    Write-Ok 'PII_KEY and SKILL_HASH_SALT already set.'
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
    } else {
        Write-Ok 'Ollama reachable with both required models.'
    }
} catch {
    Write-Warn2 'Ollama not reachable on localhost:11434 — the stack will boot, but parsing/ranking need it.'
    Write-Warn2 'Start it:  ollama serve   then   ollama pull gpt-oss:20b nomic-embed-text'
}

# ── 3. Bring up the stack ────────────────────────────────────────────────────
Write-Step 'Starting containers (postgres · neo4j · redis · api · worker · frontend)'
$upArgs = @('up', '-d')
if ($Build) { $upArgs += '--build' }
docker compose @ComposeArgs @upArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error 'docker compose up failed — see the output above.'
    exit 1
}

# ── 4. Wait for health ───────────────────────────────────────────────────────
Write-Step "Waiting for the stack to become healthy (up to ${TimeoutSeconds}s)"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$apiOk = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-RestMethod -Uri 'http://localhost:8000/health' -TimeoutSec 3
        $status = if ($resp.PSObject.Properties.Name -contains 'status') { $resp.status } else { "$resp" }
        if ($status -eq 'ok') { $apiOk = $true; break }
    } catch {
        # API not up yet (it waits for pg/neo4j/redis to be healthy first).
    }
    Start-Sleep -Seconds 3
}

Write-Host ''
docker compose @ComposeArgs ps

if ($apiOk) {
    Write-Host ''
    Write-Ok 'Stack is up.'
    Write-Host ''
    Write-Host '  Frontend (recruiter UI) : http://localhost:5000' -ForegroundColor White
    Write-Host '  API                     : http://localhost:8000   (/health, /docs)' -ForegroundColor White
    Write-Host '  Neo4j browser           : http://localhost:7474   (neo4j / recruiterpass)' -ForegroundColor White
    Write-Host ''
    Write-Host '  Logs : docker compose logs -f            Stop : ./scripts/quickstart.ps1 -Down' -ForegroundColor DarkGray
} else {
    Write-Warn2 "API /health did not go green within ${TimeoutSeconds}s. Inspect with:"
    Write-Host  '     docker compose logs api worker' -ForegroundColor DarkGray
    exit 1
}

# ── 5. Optional log follow ───────────────────────────────────────────────────
if ($Logs) {
    Write-Step 'Following logs (Ctrl-C detaches; the stack keeps running)'
    docker compose @ComposeArgs logs -f
}
