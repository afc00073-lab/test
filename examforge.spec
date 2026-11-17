# -*- mode: python ; coding: utf-8 -*-
"""Empaquetado de ExamForge como ejecutable único sin consola."""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

datas = [("examforge/ui", "examforge/ui")]
datas += collect_data_files("pymupdf", includes=["*.pyd", "*.dll", "*.ttf", "*.txt"])
binaries = collect_dynamic_libs("pymupdf")

hiddenimports = (
    collect_submodules("webview")
    + [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr_loader",
        "clr",
        "pythonnet",
        "pymupdf",
        "pymupdf.mupdf",
        "pydantic",
        "pydantic_core",
        "requests",
        "charset_normalizer",
        "idna",
    ]
)

a = Analysis(
    ["examforge/__main__.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "_pytest", "tkinter", "matplotlib", "IPython"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ExamForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="resources/icon.ico",
    version="resources/version_info.txt",
)
