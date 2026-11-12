# Copies the plugins in this repository into the Hermes home folder.
# Existing files with the same names are replaced; nothing else is touched.
$ErrorActionPreference = 'Stop'

$hermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
if (-not (Test-Path $hermesHome)) {
    throw "Hermes home not found at $hermesHome. Set HERMES_HOME to your Hermes folder and run again."
}

foreach ($root in 'plugins', 'desktop-plugins') {
    $source = Join-Path $PSScriptRoot $root
    $target = Join-Path $hermesHome $root
    New-Item -ItemType Directory -Force $target | Out-Null
    Copy-Item -Path (Join-Path $source '*') -Destination $target -Recurse -Force
    Write-Host "Copied $root -> $target"
}

Write-Host ''
Write-Host 'Next:'
Write-Host '  hermes plugins enable provider-keys'
Write-Host '  hermes plugins enable session-context'
Write-Host '  hermes plugins enable nanogpt-accounts'
Write-Host '  hermes plugins enable opencode-accounts'
Write-Host '  hermes gateway restart'
Write-Host 'Then switch on the Desktop parts under Capabilities > Plugins in Hermes Desktop.'
