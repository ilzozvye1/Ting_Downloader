"""
core - 核心基础设施模块

提供网络、数据模型、下载引擎、工具函数等公共组件,
被所有 Source 插件和 GUI/CLI 共享。
"""

from .models import Chapter, BookInfo
from .network import (
    set_proxy, get_proxy, detect_system_proxy,
    build_session, ClashRotator,
)
from .download import DownloadEngine, DownloadCallbacks, is_valid_audio_url
from .utils import sanitize_filename

__all__ = [
    "Chapter", "BookInfo",
    "set_proxy", "get_proxy", "detect_system_proxy",
    "build_session", "ClashRotator",
    "DownloadEngine", "DownloadCallbacks",
    "sanitize_filename",
]
