param(
    [string]$EnvPrefix = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($EnvPrefix)) {
    $EnvPrefix = Join-Path $ProjectRoot ".conda"
}

$CondaExe = (Get-Command conda -ErrorAction Stop).Source
$CondaPkgsDir = Join-Path $ProjectRoot ".conda-pkgs"
$PnpmStore = Join-Path $ProjectRoot ".pnpm-store"
$env:CONDA_PKGS_DIRS = $CondaPkgsDir
$env:NPM_CONFIG_USERCONFIG = Join-Path $ProjectRoot ".npmrc"
New-Item -ItemType Directory -Force -Path $CondaPkgsDir | Out-Null

Push-Location $ProjectRoot
try {
    Write-Host "Creating/updating ClarifyDeck conda env at $EnvPrefix"
    Write-Host "Using project-local conda package cache at $CondaPkgsDir"
    & $CondaExe env update --prefix $EnvPrefix --file (Join-Path $ProjectRoot "environment.yml") --prune
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $PnpmCmd = Join-Path $EnvPrefix "Scripts\pnpm.cmd"
    if (-not (Test-Path $PnpmCmd)) {
        $PnpmCmd = Join-Path $EnvPrefix "bin/pnpm"
    }
    if (-not (Test-Path $PnpmCmd)) {
        throw "pnpm was not found inside $EnvPrefix"
    }

    Write-Host "Installing frontend dependencies into project-local pnpm store $PnpmStore"
    & $PnpmCmd install --frozen-lockfile --store-dir $PnpmStore
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "ClarifyDeck environment is ready."
    Write-Host "Use: conda activate $EnvPrefix"
}
finally {
    Pop-Location
}
