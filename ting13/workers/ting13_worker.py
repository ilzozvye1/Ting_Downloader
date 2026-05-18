"""
子进程工作函数模块 — 解析 / 下载
独立模块以解决 PyInstaller frozen 环境下 multiprocessing spawn 找不到 __main__ 属性的问题

使用统一的 Source + DownloadEngine 架构，不再依赖 legacy 模块。
"""

import multiprocessing
import os
import sys
import traceback
from typing import Optional, Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from ting13.core.utils import fix_windows_encoding, setup_playwright_env
fix_windows_encoding()
setup_playwright_env()

from ting13.core.models import BookInfo, Chapter
from ting13.core.network import set_proxy, ClashRotator
from ting13.core.download import DownloadEngine, DownloadCallbacks
from ting13.sources import find_source


class _MsgSender:
    def __init__(self, q: multiprocessing.Queue):
        self._q = q
    def log(self, msg: str):
        self._q.put(("log", msg))
    def status(self, text: str):
        self._q.put(("status", text))
    def info(self, text: str):
        self._q.put(("info", text))
    def progress(self, value: float, label: str = ""):
        self._q.put(("progress", value, label))
    def buttons(self, working: bool):
        self._q.put(("buttons", working))
    def rename(self, name: str):
        self._q.put(("rename_tab", name))
    def result(self, key: str, value: Any):
        self._q.put(("result", key, value))
    def request_rotate(self, reason: str = ""):
        self._q.put(("rotate_request", reason))


class _PrintToQueue:
    def __init__(self, q: multiprocessing.Queue):
        self._q = q
        self.encoding = "utf-8"
    def write(self, text: str):
        if text and text.strip():
            self._q.put(("log", text.rstrip("\n")))
    def flush(self):
        pass


def _rebuild_book(book_data: dict, source_name: str) -> BookInfo:
    chapters = [
        Chapter(index=ch["index"], title=ch["title"], play_url=ch["play_url"])
        for ch in book_data.get("chapters", [])
    ]
    return BookInfo(
        title=book_data.get("title", "unknown"),
        author=book_data.get("author", ""),
        chapters=chapters,
        source_name=source_name,
        extra=book_data.get("extra", {}),
    )


def worker_parse(msg_q: multiprocessing.Queue,
                 stop_evt: multiprocessing.Event,
                 url: str, site: str, proxy: str):
    sys.stdout = _PrintToQueue(msg_q)
    sys.stderr = _PrintToQueue(msg_q)
    s = _MsgSender(msg_q)
    set_proxy(proxy if proxy else None)

    try:
        source = find_source(url)
        if not source:
            s.info(f"无法识别的 URL: {url}")
            s.status("解析失败")
            s.log("[FAIL] 无法识别的 URL")
            return

        # 设置停止事件到source（支持中断长时间操作）
        if hasattr(source, 'set_stop_event'):
            source.set_stop_event(stop_evt)

        # 优先使用增量式解析（如果支持）
        if hasattr(source, 'parse_book_incremental'):
            all_chapters = []
            total_estimated = 0
            first_batch_sent = False

            for batch_chapters, meta in source.parse_book_incremental(url):
                phase = meta.get("phase", "unknown")

                if phase in ("initial", "increment"):
                    # 追加新章节到总列表
                    new_count = len(batch_chapters)
                    all_chapters.extend([
                        {"index": ch.index, "title": ch.title,
                         "play_url": ch.play_url, "audio_url": ch.audio_url}
                        for ch in batch_chapters
                    ])
                    current_total = len(all_chapters)

                    if phase == "initial":
                        # 第一批：发送完整书籍信息（GUI可以立即显示并允许下载）
                        total_estimated = meta.get("total_estimated", current_total)
                        book_data = {
                            "title": meta.get("title", "未知书名"),
                            "author": meta.get("author", "未知作者"),
                            "chapters": all_chapters[:],
                            "extra": {},
                            "_streaming": True,  # 标记为流式模式（后续还有更多章节）
                            "_total_estimated": total_estimated,
                        }
                        s.info(f"书名: {meta.get('title')}  作者: {meta.get('author')}  已解析: {current_total} 章 (估算共{total_estimated}章)")
                        s.status(f"解析中 - 已获取 {current_total}/{total_estimated} 章")
                        s.log(f"[OK] 首批解析完成: {current_total} 章 (继续后台解析...)")
                        s.rename(meta.get("title", "")[:10])
                        s.result(f"{site}_book", book_data)
                        first_batch_sent = True
                    else:
                        # 后续批次：发送增量更新
                        page_num = meta.get("current_page", 0)
                        total_pages = meta.get("total_pages", 0)
                        s.status(f"解析中 - 已获取 {current_total}/{total_estimated} 章 (第{page_num}/{total_pages}页)")
                        s.log(f"[>] 增量更新: +{new_count} 章 (总计 {current_total})")

                        # 发送增量数据给GUI
                        increment_data = {
                            "chapters": [
                                {"index": ch.index, "title": ch.title,
                                 "play_url": ch.play_url, "audio_url": ch.audio_url}
                                for ch in batch_chapters
                            ],
                            "total_parsed": current_total,
                            "current_page": page_num,
                            "total_pages": total_pages,
                        }
                        s.result(f"{site}_increment", increment_data)

                elif phase == "done":
                    # 解析完成：发送最终确认
                    final_total = len(all_chapters)
                    s.info(f"书名: {meta.get('title', '')}  作者: {meta.get('author', '')}  章节: {final_total}")
                    s.status(f"解析完成 - 共 {final_total} 章")
                    s.progress(1, "完成")
                    s.log(f"[OK] 解析完成: {meta.get('title', '')} ({final_total} 章)")

                    # 发送完成信号（包含最终完整列表）
                    final_book_data = {
                        "title": meta.get("title", ""),
                        "author": meta.get("author", ""),
                        "chapters": all_chapters,
                        "extra": {},
                        "_streaming": False,  # 标记流式结束
                        "_final_total": final_total,
                    }
                    s.result(f"{site}_complete", final_book_data)

            # 如果没有收到任何数据
            if not first_batch_sent and not all_chapters:
                s.info("未找到任何章节")
                s.status("解析失败")
                s.log("[FAIL] 未找到任何章节")

        else:
            # 回退到传统全量解析模式
            book = source.parse_book(url)
            total = len(book.chapters)

            s.info(f"书名: {book.title}  作者: {book.author}  章节: {total}")
            s.status(f"解析完成 - 共 {total} 章")
            s.progress(1, "完成")
            s.log(f"[OK] 解析完成: {book.title} ({total} 章)")
            s.rename(book.title[:10])

            book_data = {
                "title": book.title,
                "author": book.author,
                "chapters": [
                    {"index": ch.index, "title": ch.title,
                     "play_url": ch.play_url,
                     "audio_url": ch.audio_url}
                    for ch in book.chapters
                ],
                "extra": book.extra if hasattr(book, 'extra') else {},
            }
            s.result(f"{site}_book", book_data)

    except Exception as e:
        s.info(f"解析失败: {e}")
        s.status("解析失败")
        s.log(f"[FAIL] 解析失败: {e}")
        s.log(traceback.format_exc())
    finally:
        s.buttons(False)


def worker_download(msg_q: multiprocessing.Queue,
                    stop_evt: multiprocessing.Event,
                    url: str, site: str, url_type: str,
                    output_dir: str, start: int, end: Optional[int],
                    headless: bool, proxy: str,
                    rotate_enabled: bool, rotate_interval: int,
                    book_data: Optional[dict],
                    concurrency: int = 1):
    sys.stdout = _PrintToQueue(msg_q)
    sys.stderr = _PrintToQueue(msg_q)
    s = _MsgSender(msg_q)
    set_proxy(proxy if proxy else None)

    try:
        source = find_source(url)
        if not source:
            s.log(f"[FAIL] 无法识别的 URL: {url}")
            s.status("下载失败")
            return

        # 设置停止事件到source（支持中断长时间操作）
        if hasattr(source, 'set_stop_event'):
            source.set_stop_event(stop_evt)

        source.set_headless(headless)

        if url_type == "play":
            book = BookInfo(
                title="single_audio",
                chapters=[Chapter(index=1, title="单集", play_url=url)],
                source_name=source.name,
            )
        elif book_data:
            book = _rebuild_book(book_data, source.name)
            s.log(f"[*] 使用已解析数据: {book.title} ({len(book.chapters)} 章)")
        else:
            s.log("[*] 尚未解析，先自动解析书籍信息...")
            s.status("自动解析中...")
            book = source.parse_book(url)

        clash_rotator = None
        if rotate_enabled and rotate_interval > 0:
            clash_rotator = ClashRotator()
            if clash_rotator.auto_detect():
                nodes = clash_rotator.load_nodes()
                if nodes:
                    s.log(f"[*] Clash API 就绪: {clash_rotator.group_name} ({len(nodes)} 节点)")
                else:
                    clash_rotator = None
            else:
                clash_rotator = None

        callbacks = DownloadCallbacks(
            on_log=s.log,
            on_status=s.status,
            on_info=s.info,
            on_progress=s.progress,
            is_stopped=lambda: stop_evt.is_set(),
        )

        engine = DownloadEngine(
            source=source,
            callbacks=callbacks,
            clash_rotator=clash_rotator,
            rotate_interval=rotate_interval if rotate_enabled else 0,
            download_workers=max(1, concurrency),
        )
        engine.run(book, output_dir, start, end)

    except Exception as e:
        s.log(f"[FAIL] 下载出错: {e}")
        s.log(traceback.format_exc())
        s.status(f"出错: {e}")
    finally:
        s.buttons(False)
