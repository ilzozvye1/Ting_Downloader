"""
sources — 有声书站点插件注册表

每个 Source 通过 URL 正则匹配来识别兼容的链接。
添加新站点只需:
  1. 在此目录下创建新的 .py 文件
  2. 继承 Source 基类并实现必要方法
  3. 在下方 get_source_classes() 中注册
"""

import re
from typing import List, Optional, Type

from .base import Source
from .ting13 import Ting13Source
from .huanting import HuantingSource


def get_source_classes() -> List[Type[Source]]:
    """返回所有已注册的 Source 类"""
    return [
        Ting13Source,
        HuantingSource,
    ]


def find_source(url: str) -> Optional[Source]:
    """
    根据 URL 匹配找到兼容的 Source 实例

    Returns:
        匹配的 Source 实例, 无匹配则返回 None
    """
    for source_cls in get_source_classes():
        for pattern in source_cls.match:
            if re.search(pattern, url, re.IGNORECASE):
                return source_cls()
    return None


def get_source_names() -> List[str]:
    """返回所有支持的站点名称"""
    names = []
    for cls in get_source_classes():
        names.extend(cls.names)
    return sorted(names)
