# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for ting13_gui (图形界面版)
将 Playwright + Chromium + CustomTkinter 完整打包

构建命令:
    pyinstaller ting13_gui.spec
"""
import os
import sys
import glob

# ── 路径配置 ──────────────────────────────────────────────────

SITE_PACKAGES = os.path.join(sys.prefix, "Lib", "site-packages")

# Playwright
PLAYWRIGHT_PKG = os.path.join(SITE_PACKAGES, "playwright")

# CustomTkinter（需要完整打包其资源文件：主题 JSON、图片等）
CUSTOMTKINTER_PKG = os.path.join(SITE_PACKAGES, "customtkinter")

# Playwright 浏览器目录
BROWSERS_DIR = os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright")
chromium_dirs = glob.glob(os.path.join(BROWSERS_DIR, "chromium-*"))
if not chromium_dirs:
    raise FileNotFoundError(
        f"未找到 Chromium 浏览器，请先运行: playwright install chromium\n"
        f"搜索路径: {BROWSERS_DIR}"
    )
CHROMIUM_DIR = sorted(chromium_dirs)[-1]
CHROMIUM_NAME = os.path.basename(CHROMIUM_DIR)

print(f"[build] Playwright:      {PLAYWRIGHT_PKG}")
print(f"[build] CustomTkinter:   {CUSTOMTKINTER_PKG}")
print(f"[build] Chromium:        {CHROMIUM_DIR} ({CHROMIUM_NAME})")

# ── 数据文件 ──────────────────────────────────────────────────

added_datas = [
    # Playwright driver
    (os.path.join(PLAYWRIGHT_PKG, "driver"), os.path.join("playwright", "driver")),

    # Chromium 浏览器
    (CHROMIUM_DIR, os.path.join("ms-playwright", CHROMIUM_NAME)),

    # CustomTkinter 完整资源（主题、字体、图片）
    (CUSTOMTKINTER_PKG, "customtkinter"),
]

# ── 隐式导入 ──────────────────────────────────────────────────

hidden_imports = [
    # Playwright
    "playwright",
    "playwright.sync_api",
    "playwright._impl",
    "playwright._impl._driver",

    # HTML 解析
    "lxml",
    "lxml.html",
    "lxml.etree",
    "lxml._elementpath",
    "cssselect",

    # 网络
    "requests",
    "urllib3",
    "charset_normalizer",
    "certifi",
    "idna",

    # GUI
    "customtkinter",
    "darkdetect",

    # 图像处理 (验证码解算)
    "numpy",
    "cv2",

    # 核心下载模块
    "ting13_downloader",
    "ting13_worker",
    "site_huanting",

    # 新插件架构模块
    "core",
    "core.models",
    "core.network",
    "core.download",
    "core.utils",
    "sources",
    "sources.base",
    "sources.ting13",
    "sources.huanting",
]

# ── Analysis ──────────────────────────────────────────────────

a = Analysis(
    ["ting13_gui.py"],
    pathex=["."],
    binaries=[],
    datas=added_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
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
    name="ting13_downloader_gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,    # GUI 程序，不显示控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# ── COLLECT ───────────────────────────────────────────────────

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ting13_downloader_gui",
)
