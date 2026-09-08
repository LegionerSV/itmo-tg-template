$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$bootstrap = Join-Path $PSScriptRoot 'bootstrap.py'
$localPython = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $localPython) {
    & $localPython $bootstrap @args
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 $bootstrap @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $bootstrap @args
} else {
    # ASCII escape sequences work in Windows PowerShell 5.1 with UTF-8 without BOM.
    $message = [System.Text.RegularExpressions.Regex]::Unescape('\u041d\u0443\u0436\u0435\u043d Python 3.12. \u0421\u043c. README.md.')
    Write-Host $message
    exit 1
}
exit $LASTEXITCODE
