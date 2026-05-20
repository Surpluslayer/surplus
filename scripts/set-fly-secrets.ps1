# Apply DEPLOY_PREP.md §1: push secrets from .env to Fly (app: surplus-prod).
#
# Usage:
#   1. flyctl auth login
#   2. flyctl launch --no-deploy --name surplus-prod --region sjc --copy-config --no-postgres --no-redis
#   3. Add DATABASE_URL (Neon) + other keys to repo-root .env
#   4. .\scripts\set-fly-secrets.ps1
#      Or: .\scripts\set-fly-secrets.ps1 -DatabaseUrl "postgresql://...?sslmode=require"
#
# Requires: flyctl on PATH (winget install Fly-io.flyctl)

param(
    [string]$App = "surplus-prod",
    [string]$DatabaseUrl = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$envFile = Join-Path $repoRoot ".env"

if (-not (Get-Command flyctl -ErrorAction SilentlyContinue)) {
    throw "flyctl not found. Run: winget install Fly-io.flyctl, then open a new terminal."
}

function Read-DotEnv([string]$path) {
    $map = @{}
    if (-not (Test-Path $path)) { return $map }
    Get-Content $path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $i = $line.IndexOf("=")
        if ($i -lt 1) { return }
        $k = $line.Substring(0, $i).Trim()
        $v = $line.Substring($i + 1).Trim().Trim('"').Trim("'")
        if ($k) { $map[$k] = $v }
    }
    return $map
}

$fromFile = Read-DotEnv $envFile

# Production overrides (DEPLOY_PREP §1)
$secrets = @{
    PROVIDER                   = "unipile"
    SURPLUS_BASE_URL           = "https://www.surpluslayer.com"
    UNIPILE_DRY_RUN            = "false"
    UNIPILE_REQUIRE_SIGNATURE  = "true"
}

$keysFromEnv = @(
    "DATABASE_URL",
    "UNIPILE_DSN",
    "UNIPILE_API_KEY",
    "UNIPILE_ACCOUNT_ID",
    "UNIPILE_WEBHOOK_SECRET",
    "ANTHROPIC_API_KEY",
    "EXA_API_KEY",
    "DEMO_ACCESS_TOKEN",
    "ADMIN_TOKEN",
    "GITHUB_TOKEN"
)

foreach ($k in $keysFromEnv) {
    if ($fromFile.ContainsKey($k) -and $fromFile[$k]) {
        $secrets[$k] = $fromFile[$k]
    }
}

if ($DatabaseUrl) {
    $secrets["DATABASE_URL"] = $DatabaseUrl
}

$required = @(
    "DATABASE_URL",
    "UNIPILE_DSN",
    "UNIPILE_API_KEY",
    "UNIPILE_ACCOUNT_ID",
    "UNIPILE_WEBHOOK_SECRET",
    "ANTHROPIC_API_KEY",
    "EXA_API_KEY",
    "DEMO_ACCESS_TOKEN",
    "ADMIN_TOKEN"
)

$missing = @($required | Where-Object { -not $secrets[$_] })
if ($missing.Count -gt 0) {
    Write-Host "Missing required values (set in .env or pass -DatabaseUrl for DATABASE_URL):" -ForegroundColor Yellow
    $missing | ForEach-Object { Write-Host "  - $_" }
    Write-Host ""
    Write-Host "Neon URL must end with ?sslmode=require" -ForegroundColor Cyan
    exit 1
}

# Optional: omit empty GITHUB_TOKEN
if (-not $secrets["GITHUB_TOKEN"]) {
    $secrets.Remove("GITHUB_TOKEN")
}

$args = @("secrets", "set", "-a", $App)
foreach ($kv in $secrets.GetEnumerator() | Sort-Object Name) {
    $escaped = $kv.Value -replace '"', '""'
    $args += "$($kv.Key)=$escaped"
}

Write-Host "Setting $($secrets.Count) secrets on app '$App'..." -ForegroundColor Green
& flyctl @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Verify:" -ForegroundColor Green
& flyctl secrets list -a $App
