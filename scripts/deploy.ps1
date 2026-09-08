$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
& (Join-Path $PSScriptRoot 'python.ps1') cloud @args
exit $LASTEXITCODE
