@echo off
setlocal
set "REACT_WIKI_LAUNCHER=%~f0"
powershell.exe -NoLogo -NoProfile -Command "$source = [System.IO.File]::ReadAllText($env:REACT_WIKI_LAUNCHER); $marker = '# REACT-WIKI-' + 'POWERSHELL'; $offset = $source.IndexOf($marker); if ($offset -lt 0) { exit 1 }; & ([scriptblock]::Create($source.Substring($offset + $marker.Length)))"
set "REACT_WIKI_EXIT=%ERRORLEVEL%"
if not "%REACT_WIKI_EXIT%"=="0" pause
exit /b %REACT_WIKI_EXIT%

# REACT-WIKI-POWERSHELL
$ErrorActionPreference = 'Stop'
trap {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}

$projectDir = Split-Path -Parent $env:REACT_WIKI_LAUNCHER
Set-Location -LiteralPath $projectDir
$proxyFile = Join-Path $projectDir 'react_proxy.py'
$promptFile = Join-Path $projectDir 'tool_prompts.temp.json'
foreach ($file in @($proxyFile, $promptFile)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Required file missing: $file"
    }
}

$python = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $bootstrap = $pyLauncher.Source
        $bootstrapArgs = @('-3')
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw 'Install Python 3.11 or later, then reopen this launcher.' }
        $bootstrap = $pythonCommand.Source
        $bootstrapArgs = @()
    }
    & $bootstrap @bootstrapArgs -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or later is required.' }
    & $bootstrap @bootstrapArgs -m venv (Join-Path $projectDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}
& $python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
if ($LASTEXITCODE -ne 0) { throw 'The existing virtual environment requires Python 3.11 or later.' }
$dependencyProbe = @'
import importlib.util, sys
raise SystemExit(not all(importlib.util.find_spec(name) for name in ('fastapi', 'uvicorn', 'httpx')))
'@
& $python -c $dependencyProbe
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing the proxy dependencies into its own virtual environment...'
    & $python -m pip install 'fastapi>=0.115,<1' 'uvicorn>=0.30,<1' 'httpx>=0.27,<1'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the message above.' }
}

if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    $secureKey = Read-Host 'DeepSeek API key (hidden; used for this launch only)' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try { $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer); $secureKey.Dispose() }
}
if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) { throw 'The DeepSeek API key is empty.' }

$privateDir = Join-Path $projectDir '.local'
New-Item -ItemType Directory -Path $privateDir -Force | Out-Null
$tokenFile = Join-Path $privateDir 'proxy-token.txt'
if ([string]::IsNullOrWhiteSpace($env:WIKI_PROXY_TOKEN)) {
    if (Test-Path -LiteralPath $tokenFile) {
        $env:WIKI_PROXY_TOKEN = [IO.File]::ReadAllText($tokenFile).Trim()
    } else {
        $env:WIKI_PROXY_TOKEN = 'wiki-local-' + [Guid]::NewGuid().ToString('N')
        [IO.File]::WriteAllText($tokenFile, $env:WIKI_PROXY_TOKEN)
    }
}
if ($env:WIKI_PROXY_TOKEN.Length -lt 24 -or $env:WIKI_PROXY_TOKEN -eq $env:DEEPSEEK_API_KEY) {
    throw 'The local proxy token must contain at least 24 characters and differ from the DeepSeek key.'
}

# Defaults for the integrated V3.0 preset. Existing environment settings take precedence.
if (-not $env:DEEPSEEK_MODEL) { $env:DEEPSEEK_MODEL = 'deepseek-v4-pro' }
if (-not $env:WIKI_BACKEND) { $env:WIKI_BACKEND = 'mediawiki' }
if (-not $env:WIKI_PORT) { $env:WIKI_PORT = '8001' }
# Local byte-based admission guard; separate from SillyTavern/model token limits.
if (-not $env:WIKI_CONTEXT_BUDGET) { $env:WIKI_CONTEXT_BUDGET = '262144' }
if (-not $env:WIKI_DATA_DIR) { $env:WIKI_DATA_DIR = Join-Path $projectDir 'runtime-data' }
# Optional: uncomment for one persistent story. Change the name for a different story.
# $env:WIKI_SINGLE_SESSION = 'my-personal-story'

# This integrated preset does not use the old three-block replacement adapter.
Remove-Item Env:WIKI_LEGACY_PRESET_FILE -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$logDir = Join-Path $projectDir 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$consoleLog = Join-Path $logDir 'proxy-console.log'
$eventsLog = Join-Path $logDir 'proxy-events.jsonl'
$header = @(
    ('Started: ' + (Get-Date).ToString('s')),
    'Protocol: server JSON mode (2.0.3)',
    ('Model: ' + $env:DEEPSEEK_MODEL),
    ('Retrieval: ' + $env:WIKI_BACKEND),
    ('Local context budget: ' + $env:WIKI_CONTEXT_BUDGET),
    ('Port: ' + $env:WIKI_PORT)
)
$header | Out-File -LiteralPath $consoleLog -Encoding utf8
if (Test-Path -LiteralPath $eventsLog) { Remove-Item -LiteralPath $eventsLog }

Write-Host ''
Write-Host ('Endpoint: http://127.0.0.1:' + $env:WIKI_PORT + '/v1')
Write-Host 'SillyTavern model: react-engine'
Write-Host ('SillyTavern API key (LOCAL proxy token): ' + $env:WIKI_PROXY_TOKEN)
Write-Host 'Start SillyTavern separately using C:\.SillyTavern\Start.bat.'
Write-Host 'Leave this window open. Press Ctrl+C here to stop the proxy.'
Write-Host ('Console feedback file: ' + $consoleLog)
Write-Host ''

$proxyExit = 0
try {
    # Windows PowerShell 5.1 otherwise treats normal native stderr logs as terminating errors.
    $ErrorActionPreference = 'Continue'
    & $python -u $proxyFile 2>&1 | ForEach-Object {
        $line = $_.ToString()
        Write-Host $line
        $line | Out-File -LiteralPath $consoleLog -Encoding utf8 -Append
    }
    $proxyExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = 'Continue'
    # Export metadata from the latest recorded proxy run; no story or reasoning table is exported.
    $export = @'
import json, sqlite3, sys
from pathlib import Path
database = Path(sys.argv[1]).expanduser().resolve() / 'state.sqlite3'
if database.is_file():
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        rows = connection.execute('SELECT time,event_json FROM events ORDER BY id DESC LIMIT 80').fetchall()
    finally:
        connection.close()
    records = [{'time': stamp, **json.loads(event)} for stamp, event in reversed(rows)]
    latest_run = records[-1].get('run_id') if records else None
    if latest_run:
        records = [record for record in records if record.get('run_id') == latest_run]
    text = ''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records)
    Path(sys.argv[2]).write_text(text, encoding='utf-8')
'@
    & $python -c $export $env:WIKI_DATA_DIR $eventsLog
    if (Test-Path -LiteralPath $eventsLog) { Write-Host ('Event feedback file: ' + $eventsLog) }
}
exit $proxyExit
