"""
通用工具函数
"""

import io
import os
import re
import sys
from typing import Optional


# ══════════════════════════════════════════════════════════════
# 文件名处理
# ══════════════════════════════════════════════════════════════

def sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符 (Windows 兼容)"""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    return name or "untitled"


# ══════════════════════════════════════════════════════════════
# PyInstaller 打包支持
# ══════════════════════════════════════════════════════════════

def is_frozen() -> bool:
    """检测是否在 PyInstaller 打包的 exe 中运行"""
    return getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


def get_bundled_base() -> str:
    """获取 PyInstaller 解压的临时目录"""
    return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))


def setup_playwright_env():
    """
    在打包模式下设置 Playwright 所需的环境变量,
    让它能找到内嵌的 Chromium 浏览器和 Node 驱动。
    """
    if not is_frozen():
        return

    base = get_bundled_base()

    browsers_path = os.path.join(base, "ms-playwright")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

    driver_path = os.path.join(base, "playwright", "driver")
    if os.path.isdir(driver_path):
        os.environ["PLAYWRIGHT_DRIVER_PATH"] = driver_path


def get_chrome_exe_path() -> Optional[str]:
    """获取打包内嵌的 Chromium 可执行文件路径"""
    if not is_frozen():
        return None
    base = get_bundled_base()
    chrome_exe = os.path.join(
        base, "ms-playwright", "chromium-1208", "chrome-win64", "chrome.exe"
    )
    return chrome_exe if os.path.isfile(chrome_exe) else None


# ══════════════════════════════════════════════════════════════
# Windows 控制台编码修复
# ══════════════════════════════════════════════════════════════

def fix_windows_encoding():
    """修复 Windows 控制台的 UTF-8 编码问题"""
    if sys.platform == "win32":
        if hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "buffer") and getattr(sys.stderr, "encoding", "").lower() != "utf-8":
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
