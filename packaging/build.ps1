<#
.SYNOPSIS
  ToolDock を Windows 向けに onedir ビルドする。-Zip で配布用の zip も作る。

.DESCRIPTION
  dist/ToolDock/ に ToolDock.exe (窓なし) と _internal/ を作る。
  設定 tooldock.toml は exe の隣に置く。すでにあれば上書きしない (人が直したものを消さない)。
  -Zip: dist/ToolDock-<版>-win-x64.zip を作る。設定は見本 (tooldock.example.toml) を tooldock.toml として入れる。
  手元の dist/ToolDock/tooldock.toml (自分のツールの場所が書いてある) は zip に入れない。
#>
[CmdletBinding()]
param([switch]$Zip)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 動いている ToolDock (dist のもの) が _internal を掴んでいるとビルドが失敗する
$running = Get-Process ToolDock -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith((Join-Path $root 'dist'), [StringComparison]::OrdinalIgnoreCase) }
if ($running) {
    Write-Warning "dist の ToolDock が動いています。閉じてからビルドしてください"
    exit 1
}

uv run --no-project --with pyinstaller pyinstaller packaging/tooldock.spec --noconfirm --clean --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$out = Join-Path $root 'dist/ToolDock'
$toml = Join-Path $out 'tooldock.toml'
if (-not (Test-Path $toml)) {
    Copy-Item (Join-Path $root 'tooldock.example.toml') $toml
    Write-Host "設定の見本を置きました: $toml"
}
Write-Host "できました: $(Join-Path $out 'ToolDock.exe')"

if ($Zip) {
    $version = (Select-String -Path (Join-Path $root 'tooldock.py') -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
    $name = "ToolDock-$version-win-x64"
    $stage = Join-Path $root "build/$name"
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    Copy-Item $out $stage -Recurse
    Copy-Item (Join-Path $root 'tooldock.example.toml') (Join-Path $stage 'tooldock.toml') -Force  # 手元の設定を入れない
    foreach ($f in 'README.md', 'LICENSE', 'CHANGELOG.md') { Copy-Item (Join-Path $root $f) $stage }
    $zipPath = Join-Path $root "dist/$name.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipPath
    Write-Host "zip: $zipPath"
}
