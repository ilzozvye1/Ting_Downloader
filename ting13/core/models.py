"""
统一数据模型 — 所有 Source 插件共用
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Chapter:
    """一个章节"""
    index: int          # 序号 (1-based)
    title: str          # 标题
    play_url: str       # 播放页 URL
    audio_url: str = "" # 音频下载 URL (由 Source 填充)
    downloaded: bool = False

    def __repr__(self):
        audio = "Yes" if self.audio_url else "No"
        return f"Chapter({self.index}, '{self.title}', audio={audio})"


@dataclass
class BookInfo:
    """一本有声书"""
    title: str
    chapters: List[Chapter] = field(default_factory=list)
    author: str = ""
    announcer: str = ""        # 播音员
    cover_url: str = ""
    source_name: str = ""      # 来源站点名称
    extra: dict = field(default_factory=dict)  # 站点特有数据 (如 book_id)

    def __repr__(self):
        return f"BookInfo('{self.title}', chapters={len(self.chapters)}, source='{self.source_name}')"
