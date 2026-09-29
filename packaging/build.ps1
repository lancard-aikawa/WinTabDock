<#
.SYNOPSIS
  ToolDock を Windows 向けに onedir ビルドする。

.DESCRIPTION
  dist/ToolDock/ に ToolDock.exe (窓なし) と _internal/ を作る。
  設定 tooldock.toml は exe の隣に置く。すでにあれば上書きしない (人が直したものを消さない)。
#>
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
    Copy-Item (Join-Path $root 'tooldock.toml') $toml
    Write-Host "設定の見本を置きました: $toml"
}
Write-Host "できました: $(Join-Path $out 'ToolDock.exe')"
