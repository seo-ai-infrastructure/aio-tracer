$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv')) { python -m venv .venv; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.venv\Scripts\python.exe' -m playwright install chromium
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.venv\Scripts\python.exe' -m uvicorn server:app --host 127.0.0.1 --port 8001
