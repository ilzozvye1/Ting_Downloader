# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for ting13_downloader
将 Playwright + Chromium 浏览器完整打包为单个 exe

构建命令:
    pyinstaller ting13_downloader.spec

或使用 build.bat 一键构建
"""
import os
import sys
import glob

# ── 路径配置 ──────────────────────────────────────────────────

# Python site-packages 中的 playwright 包
PLAYWRIGHT_PKG = os.path.join(
    sys.prefix, "Lib", "site-packages", "playwright"
)

# Playwright 浏览器存放目录
BROWSERS_DIR = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "ms-playwright"
)

# 查找 chromium 目录（支持不同版本号）
chromium_dirs = glob.glob(os.path.join(BROWSERS_DIR, "chromium-*"))
if not chromium_dirs:
    raise FileNotFoundError(
        f"未找到 Chromium 浏览器，请先运行: playwright install chromium\n"
        f"搜索路径: {BROWSERS_DIR}"
    )
CHROMIUM_DIR = sorted(chromium_dirs)[-1]  # 取最新版本
CHROMIUM_NAME = os.path.basename(CHROMIUM_DIR)  # e.g. "chromium-1208"

print(f"[build] Playwright package: {PLAYWRIGHT_PKG}")
print(f"[build] Chromium browser:   {CHROMIUM_DIR}")
print(f"[build] Chromium version:   {CHROMIUM_NAME}")

# ── 数据文件 ──────────────────────────────────────────────────

added_datas = [
    # 1) Playwright driver（node.exe + JS 运行时）
    (os.path.join(PLAYWRIGHT_PKG, "driver"), os.path.join("playwright", "driver")),

    # 2) Chromium 浏览器二进制文件（完整目录）
    (CHROMIUM_DIR, os.path.join("ms-playwright", CHROMIUM_NAME)),
]

# ── 隐式导入 ──────────────────────────────────────────────────

hidden_imports = [
    "playwright",
    "playwright.sync_api",
    "playwright._impl",
    "playwright._impl._driver",
    "lxml",
    "lxml.html",
    "lxml.etree",
    "lxml._elementpath",
    "cssselect",
    "requests",
    "urllib3",
    "charset_normalizer",
    "certifi",
    "idna",
]

# ── Analysis ──────────────────────────────────────────────────

a = Analysis(
    ["ting13_downloader.py"],
    pathex=[],
    binaries=[],
    datas=added_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不需要的大模块以减小体积
        "tkinter",
        "matplotlib",
        "pandas",
        "scipy",
        "torch",
        "tensorflow",
    ],
    noarchive=False,
    optimize=0,
)

# ── PYZ ───────────────────────────────────────────────────────

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ── EXE ───────────────────────────────────────────────────────

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ting13_downloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,      # 不压缩 Chromium 二进制文件
    console=True,    # 控制台程序
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,       # 可替换为 .ico 图标路径
)

# ── COLLECT ───────────────────────────────────────────────────

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ting13_downloader",
)
