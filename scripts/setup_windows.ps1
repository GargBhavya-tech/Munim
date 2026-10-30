# Windows equivalent of `make setup` (+ optional `make setup-ml`).
# Usage (from the repo root):  powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 [-Ml] [-SkipTests]
param(
    [switch]$Ml,         # also install requirements-ml.txt (torch CPU etc.; needed by tests/test_nli_gate.py and tests/test_retrieval.py)
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
# Source files contain Hindi/Kannada text and several modules call read_text() without an encoding;
# without UTF-8 mode Windows decodes them as cp1252 and tests fail.
$env:PYTHONUTF8 = "1"
Set-Location (Split-Path $PSScriptRoot -Parent)

# Python 3.11+ via the py launcher; 3.12 preferred (every pinned wheel exists for it).
$pyver = $null
foreach ($v in @("3.12", "3.11", "3.13")) {
    & py "-$v" -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) { $pyver = $v; break }
}
if (-not $pyver) { throw "Python 3.11+ not found (install it and the py launcher)" }

if (-not (Test-Path .venv)) { & py "-$pyver" -m venv .venv }
$py = ".venv\Scripts\python.exe"
& $py -m pip install -q --upgrade pip
& $py -m pip install -q -r requirements.txt
if ($Ml) { & $py -m pip install -q -r requirements-ml.txt }

# Local .env with empty keys (never committed; .gitignore covers it).
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

# Pre-commit secret check (needs Git for Windows, which ships bash).
& bash scripts/install_hooks.sh

Push-Location dashboard
npm ci
npm run build
Pop-Location

if (-not $SkipTests) {
    if ($Ml) { & $py -m pytest }
    else {
        $env:MUNIM_SKIP_MODEL_TESTS = "1"
        & $py -m pytest --ignore=tests/test_nli_gate.py --ignore=tests/test_retrieval.py
    }
}
