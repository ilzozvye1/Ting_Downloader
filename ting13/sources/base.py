"""
Source 基类 — 所有有声书站点插件的抽象接口

参考 audiobook-dl 的设计, 每个 Source 插件实现:
  - URL 匹配 (match)
  - 页面解析 (parse_book)
  - 音频 URL 提取 (get_audio_url)
  - 生命周期钩子 (before_download / after_download)
"""

from abc import ABC, abstractmethod
from typing import List, Optional, TYPE_CHECKING

from core.models import BookInfo, Chapter

if TYPE_CHECKING:
    from core.download import DownloadCallbacks


class Source(ABC):
    """
    有声书源站抽象基类

    子类必须实现:
      - match: URL 正则匹配列表
      - names: 站点名称列表
      - base_url: 站点基础 URL
      - parse_book(url): 解析书籍页面
      - get_audio_url(chapter): 获取音频下载 URL
    """

    # ── 子类必须覆盖 ──

    match: List[str] = []       # URL 匹配正则列表
    names: List[str] = []       # 站点名称列表
    base_url: str = ""          # 站点基础 URL (用于 Referer)

    @property
    def name(self) -> str:
        """主名称"""
        return self.names[0] if self.names else "unknown"

    # ── 核心方法 (子类必须实现) ──

    @abstractmethod
    def detect_url_type(self, url: str) -> str:
        """
        识别 URL 类型

        Returns:
            'book' (书籍页面), 'play' (播放页面), 或 'unknown'
        """
        ...

    @abstractmethod
    def parse_book(self, url: str) -> BookInfo:
        """
        解析书籍页面, 获取书籍信息和完整章节列表

        Args:
            url: 书籍页面 URL

        Returns:
            BookInfo 对象, 包含 title, chapters 等
        """
        ...

    @abstractmethod
    def get_audio_url(self, chapter: Chapter) -> Optional[str]:
        """
        获取单个章节的音频下载 URL

        Args:
            chapter: 章节对象

        Returns:
            音频 URL, 失败返回 None
        """
        ...

    # ── 预取 (可选覆盖, 用于流水线下载) ──

    def prefetch_audio_url(self, chapter: Chapter) -> Optional[str]:
        """
        快速获取音频 URL (仅做轻量 API 调用, 不触发验证码解算)

        用于下载流水线: 在后台线程中预取下一章 URL。
        默认实现直接调用 get_audio_url, 子类可覆盖为轻量版本。
        """
        return self.get_audio_url(chapter)

    # ── 生命周期钩子 (可选覆盖) ──

    def before_download(self, chapters: List[Chapter], callbacks: "DownloadCallbacks"):
        """
        下载开始前的准备工作

        可用于: 启动 Playwright 浏览器, 初始化 session, 解算验证码等。
        默认什么都不做。
        """
        pass

    def after_download(self):
        """
        下载完成后的清理工作

        可用于: 关闭浏览器, 清理临时文件等。
        默认什么都不做。
        """
        pass

    # ── 认证 (可选) ──

    def supports_login(self) -> bool:
        """是否支持登录"""
        return False

    def is_authenticated(self) -> bool:
        """是否已登录"""
        return False
