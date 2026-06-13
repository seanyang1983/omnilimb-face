<#
.SYNOPSIS
    Build and publish omnilimb-face to PyPI using the project-scoped token.

.DESCRIPTION
    This script:
      1. Locates the .env file (repo root, parent dir, or $env:OMNILIMB_ENV_FILE).
      2. Reads the PyPI token (PYPI_API_TOKEN_FACE by default).
      3. Cleans old build artifacts.
      4. Builds the sdist + wheel with Python 3.11.
      5. Runs `twine check` on the artifacts.
      6. Uploads to PyPI with twine (token used transiently; never printed).

    No secrets are stored in this file. The token is read at runtime from .env
    and masked in any output.

.PARAMETER TokenKey
    The .env key holding the PyPI token. Defaults to PYPI_API_TOKEN_FACE
    (project-scoped to "omnilimb-face"). Use PYPI_API_TOKEN for the "omnilimb"
    project if you ever repurpose this script.

.PARAMETER SkipBuild
    Skip the build step and upload whatever is already in dist/.

.PARAMETER TestPyPI
    Upload to TestPyPI (https://test.pypi.org/legacy/) instead of PyPI.

.EXAMPLE
    py -3.11 -m pip install --upgrade build twine   # one-time
    .\release.ps1                                   # build + upload to PyPI

.EXAMPLE
    .\release.ps1 -SkipBuild                        # upload existing dist/*
#>
[CmdletBinding()]
param(
    [string]$TokenKey = 'PYPI_API_TOKEN_FACE',
    [switch]$SkipBuild,
    [switch]$TestPyPI
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- 1. Locate .env -----------------------------------------------------------
$envCandidates = @()
if ($env:OMNILIMB_ENV_FILE) { $envCandidates += $env:OMNILIMB_ENV_FILE }
$envCandidates += (Join-Path $RepoRoot '.env')
$envCandidates += (Join-Path (Split-Path $RepoRoot -Parent) '.env')

$envFile = $envCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $envFile) {
    throw "Could not find a .env file. Looked in: $($envCandidates -join ', '). " +
          "Set `$env:OMNILIMB_ENV_FILE to its full path."
}
Write-Step "Using .env: $envFile"

# --- 2. Read the token --------------------------------------------------------
$token = $null
foreach ($line in Get-Content -LiteralPath $envFile) {
    $trimmed = $line.Trim()
    if ($trimmed -match "^\s*$TokenKey\s*=\s*(.+)$") {
        $token = $Matches[1].Trim().Trim('"').Trim("'")
        break
    }
}
if (-not $token) { throw "Key '$TokenKey' not found (or empty) in $envFile." }
if ($token -notmatch '^pypi-') { throw "Value of '$TokenKey' does not look like a PyPI token (missing 'pypi-' prefix)." }
Write-Step "Loaded token from key '$TokenKey' (value hidden)."

# --- 3. Clean old artifacts ---------------------------------------------------
if (-not $SkipBuild) {
    Write-Step "Cleaning dist/ ..."
    if (Test-Path (Join-Path $RepoRoot 'dist')) {
        Remove-Item -Recurse -Force (Join-Path $RepoRoot 'dist')
    }
}

# --- 4. Build -----------------------------------------------------------------
if (-not $SkipBuild) {
    Write-Step "Building sdist + wheel (py -3.11 -m build) ..."
    & py -3.11 -m build
    if ($LASTEXITCODE -ne 0) { throw "Build failed (exit $LASTEXITCODE)." }
}

$artifacts = Get-ChildItem -Path (Join-Path $RepoRoot 'dist') -File -ErrorAction SilentlyContinue
if (-not $artifacts) { throw "No artifacts found in dist/. Run without -SkipBuild first." }
Write-Step ("Artifacts: " + (($artifacts | Select-Object -ExpandProperty Name) -join ', '))

# --- 5. twine check -----------------------------------------------------------
Write-Step "Validating artifacts (twine check) ..."
& py -3.11 -m twine check dist/*
if ($LASTEXITCODE -ne 0) { throw "twine check failed (exit $LASTEXITCODE)." }

# --- 6. Upload ----------------------------------------------------------------
$repoArgs = @()
if ($TestPyPI) {
    $repoArgs = @('--repository-url', 'https://test.pypi.org/legacy/')
    Write-Step "Uploading to TestPyPI ..."
} else {
    Write-Step "Uploading to PyPI ..."
}

$env:TWINE_USERNAME = '__token__'
$env:TWINE_PASSWORD = $token
$env:PYTHONIOENCODING = 'utf-8'
try {
    $raw = & py -3.11 -m twine upload @repoArgs dist/* --non-interactive --disable-progress-bar 2>&1
    $code = $LASTEXITCODE
} finally {
    # Always scrub the token from the environment.
    $env:TWINE_PASSWORD = ''
}

# Mask any accidental token echo before printing.
($raw | Out-String) -replace 'pypi-[A-Za-z0-9_\-]+', 'pypi-***MASKED***' | Write-Host
if ($code -ne 0) { throw "twine upload failed (exit $code)." }

Write-Step "Done. View at: https://pypi.org/project/omnilimb-face/"
