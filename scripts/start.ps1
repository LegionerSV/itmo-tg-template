$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
& (Join-Path $PSScriptRoot 'python.ps1') local @args
exit $LASTEXITCODE
