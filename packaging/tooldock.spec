# -*- mode: python ; coding: utf-8 -*-
"""onedir ビルド定義。packaging/build.ps1 から使う。

出力は dist/ToolDock/ (ToolDock.exe + _internal/)。設定 (tooldock.toml) は exe の隣に置く。
窓なし (console=False) の 1 本だけ。試験 (selftest.py、Pillow) は入れない。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH は PyInstaller が注入する

a = Analysis(  # noqa: F821
    [str(ROOT / "tooldock.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "assets" / "icon.ico"), "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["selftest", "PIL", "pytest"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ToolDock",
    icon=str(ROOT / "assets" / "icon.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ToolDock",
)
