<#
.SYNOPSIS
  Build WinTabDock for Windows (PyInstaller onedir). -Zip also makes the release zip.

.DESCRIPTION
  Creates dist/WinTabDock/ with WinTabDock.exe (no console) and _internal/.
  The settings file wintabdock.toml lives next to the exe. PyInstaller deletes
  dist/WinTabDock/ before it builds, so an existing wintabdock.toml is copied
  aside first and put back afterwards (keep what the user wrote). If there was
  none, the sample (wintabdock.example.toml) is placed there.
  -Zip: creates dist/WinTabDock-<version>-win-x64.zip with the sample settings.
  The local dist/WinTabDock/wintabdock.toml (your own tool paths) is not zipped.
#>
[CmdletBinding()]
param([switch]$Zip)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# A running WinTabDock from dist holds _internal and makes the build fail
$running = Get-Process WinTabDock -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith((Join-Path $root 'dist'), [StringComparison]::OrdinalIgnoreCase) }
if ($running) {
    Write-Warning "WinTabDock from dist is running. Close it and build again."
    exit 1
}

$out = Join-Path $root 'dist/WinTabDock'
$toml = Join-Path $out 'wintabdock.toml'
$saved = Join-Path $root 'build/wintabdock.toml.keep'
New-Item -ItemType Directory -Force (Join-Path $root 'build') | Out-Null
if (Test-Path $toml) {
    Copy-Item $toml $saved -Force
}

uv run --no-project --with pyinstaller pyinstaller packaging/wintabdock.spec --noconfirm --clean --distpath dist --workpath build
$code = $LASTEXITCODE

if (Test-Path $saved) {
    New-Item -ItemType Directory -Force $out | Out-Null
    Copy-Item $saved $toml -Force
    Remove-Item $saved
    Write-Host "Kept your settings: $toml"
} elseif ($code -eq 0) {
    Copy-Item (Join-Path $root 'wintabdock.example.toml') $toml
    Write-Host "Placed the sample settings: $toml"
}
if ($code -ne 0) { exit $code }
Write-Host "Built: $(Join-Path $out 'WinTabDock.exe')"

if ($Zip) {
    $version = (Select-String -Path (Join-Path $root 'wintabdock.py') -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
    $name = "WinTabDock-$version-win-x64"
    $stage = Join-Path $root "build/$name"
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    Copy-Item $out $stage -Recurse
    Copy-Item (Join-Path $root 'wintabdock.example.toml') (Join-Path $stage 'wintabdock.toml') -Force  # never ship local settings
    foreach ($f in 'README.md', 'LICENSE', 'CHANGELOG.md') { Copy-Item (Join-Path $root $f) $stage }
    $zipPath = Join-Path $root "dist/$name.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipPath
    Write-Host "zip: $zipPath"
}
