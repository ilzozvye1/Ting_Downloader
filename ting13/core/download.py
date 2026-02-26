"""
通用下载引擎 — 适用于所有 Source 插件

职责:
- 目录创建、封面下载
- 扫描已下载文件、识别缺失集数(gap)
- 调用 Source.get_audio_url() 获取音频 URL (带无限重试)
- 下载音频文件 (带重试、进度回调)
- 自适应反限流延迟、URL 预取流水线、Clash 自动换 IP
- 支持 stop 中断

GUI 和 CLI 都使用这个引擎, 只需传入不同的回调函数即可。
"""

import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from .models import BookInfo, Chapter
from .network import build_session, get_proxy, ClashRotator, random_ua
from .utils import sanitize_filename

if TYPE_CHECKING:
    from ting13.sources.base import Source


# ══════════════════════════════════════════════════════════════
# 回调接口
# ══════════════════════════════════════════════════════════════

@dataclass
class DownloadCallbacks:
    """
    下载过程中的回调函数集合

    GUI 模式: 将这些函数绑定到 UI 更新方法
    CLI 模式: 将这些函数绑定到 print
    """
    on_log: Callable[[str], None] = lambda msg: print(msg)
    on_status: Callable[[str], None] = lambda text: None
    on_info: Callable[[str], None] = lambda text: None
    on_progress: Callable[[float, str], None] = lambda val, label: None
    is_stopped: Callable[[], bool] = lambda: False


# ══════════════════════════════════════════════════════════════
# 文件下载
# ══════════════════════════════════════════════════════════════

# 不是音频的 URL (iframe / PHP 页面等)
_INVALID_AUDIO_URLS = [
    "MTaudio.php", "PTaudio2.php", "PTaudio.php",
    "MTaudio2024.js", "PTingJplayer.js", "MTingJplayer.js",
]

# 音频文件的最小有效大小 (50 KB)
MIN_AUDIO_SIZE = 50 * 1024


def is_valid_audio_url(url: str) -> bool:
    """
    判断 URL 是否是有效的音频下载地址

    过滤掉 iframe HTML 页面、JS 文件等非音频 URL。
    这些 URL 如果被误当作音频下载, 只会得到 4KB 的 HTML。
    """
    if not url:
        return False
    url_lower = url.lower()
    for bad in _INVALID_AUDIO_URLS:
        if bad.lower() in url_lower:
            return False
    # 纯 PHP/HTML 路径, 没有音频扩展名 → 大概率不是音频
    if url_lower.endswith((".php", ".html", ".htm", ".js")):
        return False
    return True


# CDN 域名白名单 — 这些域名不走代理, 直连更快
_CDN_DOMAINS = ["xmcdn.com", "cos.tx.", "cdn.", "clouddn.com"]


def _is_cdn_url(url: str) -> bool:
    """判断 URL 是否指向 CDN (可跳过代理)"""
    url_lower = url.lower()
    return any(d in url_lower for d in _CDN_DOMAINS)


def download_file(
    url: str,
    filepath: str,
    *,
    referer: str = "",
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cdn_direct: bool = True,
) -> bool:
    """
    下载文件到指定路径 (带临时文件保护 + 大小校验)

    Args:
        url: 下载 URL
        filepath: 保存路径
        referer: Referer 头
        progress_callback: 进度回调 (downloaded_bytes, total_bytes)
        cdn_direct: CDN 地址是否跳过代理直连 (默认 True)

    Returns:
        是否成功
    """
    if url.startswith("//"):
        url = "https:" + url

    # CDN 直连: 跳过代理, 减少一跳延迟
    use_proxy = not (cdn_direct and _is_cdn_url(url))
    if use_proxy:
        session = build_session(referer=referer)
    else:
        session = build_session(referer=referer, proxy="__none__")

    tmp_path = filepath + ".tmp"

    try:
        resp = session.get(url, stream=True, timeout=60, verify=False)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))

        downloaded = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total:
                        progress_callback(downloaded, total)

        # 验证文件完整性
        if total and downloaded < total * 0.95:
            os.remove(tmp_path)
            return False

        # 验证文件大小 (< 50KB 视为无效, 可能是 HTML/错误页面)
        if downloaded < MIN_AUDIO_SIZE:
            os.remove(tmp_path)
            return False

        os.replace(tmp_path, filepath)
        return True

    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def download_cover(cover_url: str, save_dir: str, referer: str = "") -> Optional[str]:
    """下载封面图片"""
    if not cover_url:
        return None
    from urllib.parse import urlparse
    ext = os.path.splitext(urlparse(cover_url).path)[1] or ".jpg"
    filepath = os.path.join(save_dir, f"cover{ext}")
    if os.path.exists(filepath):
        return filepath
    if download_file(cover_url, filepath, referer=referer):
        return filepath
    return None


# ══════════════════════════════════════════════════════════════
# 缺失集数检测
# ══════════════════════════════════════════════════════════════

def scan_downloaded(book_dir: str) -> set:
    """扫描目录中已下载的章节编号 (根据文件名前缀 0001_)"""
    downloaded = set()
    try:
        for f in os.listdir(book_dir):
            parts = f.split("_", 1)
            if parts[0].isdigit():
                downloaded.add(int(parts[0]))
    except OSError:
        pass
    return downloaded


def reorder_with_gaps_first(
    chapters: List[Chapter],
    downloaded_indices: set,
) -> List[Chapter]:
    """
    重排章节列表: 先补齐缺失的 (gap), 再下载后续的

    Args:
        chapters: 待下载的章节列表 (已过滤掉已下载的)
        downloaded_indices: 已下载的章节编号集合

    Returns:
        重排后的章节列表
    """
    if not downloaded_indices or not chapters:
        return chapters

    min_dl = min(downloaded_indices)
    max_dl = max(downloaded_indices)

    gaps = [ch for ch in chapters if min_dl <= ch.index <= max_dl]
    continuations = [ch for ch in chapters if ch.index > max_dl or ch.index < min_dl]

    if gaps:
        return gaps + continuations
    return chapters


# ══════════════════════════════════════════════════════════════
# 通用下载引擎
# ══════════════════════════════════════════════════════════════

class DownloadEngine:
    """
    通用下载引擎 (并行 CDN 下载 + URL 预取 + 自适应延迟)

    架构:  生产者 (主线程) → 消费者 (下载线程池)

    - 主线程: 按间隔顺序获取 URL (尊重 API 限流)
    - 下载线程池 (3 workers): 并行从 CDN 下载文件 (CDN 无限流)
    - 文件下载隐藏在 API 延迟之后, 不额外占用时间
    """

    # 默认换 IP 间隔 (章数)
    DEFAULT_ROTATE_INTERVAL = 15
    # 并行下载线程数
    DOWNLOAD_WORKERS = 3

    def __init__(
        self,
        source: "Source",
        callbacks: DownloadCallbacks,
        clash_rotator: Optional[ClashRotator] = None,
        rotate_interval: int = 0,
    ):
        self.source = source
        self.cb = callbacks
        self.clash_rotator = clash_rotator
        # 如果用户没有设置, 且有 Clash, 则使用默认间隔
        if rotate_interval > 0:
            self.rotate_interval = rotate_interval
        elif clash_rotator:
            self.rotate_interval = self.DEFAULT_ROTATE_INTERVAL
        else:
            self.rotate_interval = 0

        # 自适应延迟状态
        self._consecutive_ok = 0
        self._consecutive_fail = 0
        self._rate_limit_count = 0

        # URL 预取 (单线程, 仅轻量 API 调用)
        self._prefetch_pool = ThreadPoolExecutor(max_workers=1)
        self._prefetch_future: Optional[Future] = None
        self._prefetch_chapter_idx: int = -1

        # 并行下载池 (CDN 不限流, 可多线程)
        self._dl_pool = ThreadPoolExecutor(max_workers=self.DOWNLOAD_WORKERS)
        # 待完成的下载任务: chapter_index → (Chapter, filename, Future)
        self._pending_dl: Dict[int, Tuple[Chapter, str, Future]] = {}

        # 将 Clash 轮换器传递给 Source (用于验证码等场景)
        if clash_rotator and hasattr(source, 'set_clash_rotator'):
            source.set_clash_rotator(clash_rotator)

    def run(
        self,
        book: BookInfo,
        output_dir: str,
        start: int = 1,
        end: Optional[int] = None,
    ):
        chapters = book.chapters
        if not chapters:
            self.cb.on_log("[FAIL] 未找到任何章节")
            self.cb.on_status("未找到章节")
            return

        # 范围裁剪
        start_idx = max(0, start - 1)
        end_idx = end if end else len(chapters)
        chapters = chapters[start_idx:end_idx]

        source_tag = f"[{self.source.name}]"
        self.cb.on_info(
            f"{source_tag} {book.title}   "
            f"第{start}~{start + len(chapters) - 1}集 (共{len(chapters)}集)"
        )

        # 创建目录
        book_dir = os.path.join(output_dir, sanitize_filename(book.title))
        os.makedirs(book_dir, exist_ok=True)
        self.cb.on_log(f"[*] 输出目录: {os.path.abspath(book_dir)}")

        # 下载封面
        if book.cover_url:
            referer = self.source.base_url
            cover = download_cover(book.cover_url, book_dir, referer=referer)
            if cover:
                self.cb.on_log("[*] 封面已保存")

        # 扫描已下载
        downloaded_indices = scan_downloaded(book_dir)

        # 过滤 + 重排
        missing = [ch for ch in chapters if ch.index not in downloaded_indices]
        if not missing:
            self.cb.on_log("[*] 所有章节均已下载, 无需重复下载")
            self.cb.on_status("全部已下载")
            return

        skipped = len(chapters) - len(missing)
        if skipped:
            self.cb.on_log(f"[*] 跳过已下载: {skipped} 集")

        chapters = reorder_with_gaps_first(missing, downloaded_indices)

        # 显示缺失信息
        if downloaded_indices:
            min_dl = min(downloaded_indices)
            max_dl = max(downloaded_indices)
            gaps = [ch for ch in chapters if min_dl <= ch.index <= max_dl]
            if gaps:
                gap_nums = [str(ch.index) for ch in gaps]
                display = ', '.join(gap_nums[:20]) + ("... 等" if len(gaps) > 20 else "")
                self.cb.on_log(f"[*] 检测到 {len(gaps)} 个缺失集: {display}")

        total = len(chapters)
        self.cb.on_log(f"[*] 待下载: {total} 集\n")

        if self.rotate_interval > 0 and self.clash_rotator:
            self.cb.on_log(
                f"[*] 自动换IP已启用: 每 {self.rotate_interval} 集切换节点"
            )

        self.cb.on_log(
            f"[*] 并行模式: {self.DOWNLOAD_WORKERS} 线程下载 + "
            f"URL预取 + CDN直连 + 自适应延迟\n"
        )

        # ── 调用 Source 的准备钩子 ──
        self.source.before_download(chapters, self.cb)

        # ── 主循环: 获取 URL 并提交下载 ──
        success = 0
        fail = 0

        try:
            for i, chapter in enumerate(chapters):
                if self.cb.is_stopped():
                    self.cb.on_log("[!] 用户请求停止")
                    break

                # ── 收割已完成的下载 (非阻塞) ──
                s, f = self._collect_completed()
                success += s
                fail += f

                # 控制并发: 如果满了, 等一个完成
                while (len(self._pending_dl) >= self.DOWNLOAD_WORKERS
                       and not self.cb.is_stopped()):
                    time.sleep(0.2)
                    s, f = self._collect_completed()
                    success += s
                    fail += f

                progress = (success + fail) / total
                self.cb.on_progress(progress, f"{success + fail}/{total}")
                self.cb.on_status(f"下载中 [{i + 1}/{total}]  {chapter.title}")
                self.cb.on_log(f"[{i + 1}/{total}] {chapter.title}")

                # 二次检查是否已下载 (防并发)
                try:
                    existing = [
                        f for f in os.listdir(book_dir)
                        if f.startswith(f"{chapter.index:04d}_")
                    ]
                except OSError:
                    existing = []

                if existing:
                    self.cb.on_log(f"  [SKIP] 已存在: {existing[0]}")
                    success += 1
                    continue

                # ── 获取音频 URL (优先使用预取) ──
                audio_url = self._consume_prefetch(chapter)
                if audio_url:
                    self.cb.on_log("  [>] 获取URL... (预取)")
                else:
                    self.cb.on_log("  [>] 获取URL...")
                    audio_url = self._fetch_url_with_retry(chapter)

                if self.cb.is_stopped() or not audio_url:
                    if not self.cb.is_stopped():
                        fail += 1
                    continue

                if not is_valid_audio_url(audio_url):
                    self.cb.on_log(f"  [!] 无效音频 URL: {audio_url}")
                    fail += 1
                    continue

                chapter.audio_url = audio_url
                short_url = audio_url[:70] + "..." if len(audio_url) > 70 else audio_url
                self.cb.on_log(f"  [OK] {short_url}")

                # 确定扩展名
                ext = ".mp3"
                for fmt in [(".m4a", ".m4a"), (".aac", ".aac"), (".mp3", ".mp3")]:
                    if fmt[0] in audio_url:
                        ext = fmt[1]
                        break

                filename = f"{chapter.index:04d}_{sanitize_filename(chapter.title)}{ext}"
                filepath = os.path.join(book_dir, filename)

                # ── 提交下载到线程池 (非阻塞) ──
                cdn_tag = " (CDN直连)" if _is_cdn_url(audio_url) else ""
                self.cb.on_log(f"  [>>] 提交下载: {filename}{cdn_tag}")
                future = self._dl_pool.submit(
                    self._download_task, chapter, audio_url,
                    filepath, filename, self.source.base_url,
                )
                self._pending_dl[chapter.index] = (chapter, filename, future)

                # ── 启动预取下一章 URL ──
                self._start_prefetch(chapters, i + 1, book_dir)

                # ── 反限流延迟 (只控制 API 调用节奏) ──
                self._anti_rate_limit_delay(i, total)

            # ── 等待所有剩余下载完成 ──
            if self._pending_dl and not self.cb.is_stopped():
                self.cb.on_log(
                    f"\n[*] 等待 {len(self._pending_dl)} 个下载完成..."
                )
            while self._pending_dl and not self.cb.is_stopped():
                time.sleep(0.5)
                s, f = self._collect_completed()
                success += s
                fail += f

        finally:
            self._cancel_prefetch()
            self._prefetch_pool.shutdown(wait=False)
            self._dl_pool.shutdown(wait=True)
            self.source.after_download()

        self._save_record(book, chapters, book_dir)

        self.cb.on_progress(1, "完成")
        stopped = " (已停止)" if self.cb.is_stopped() else ""
        summary = f"完成{stopped} - 成功: {success}, 失败: {fail}"
        self.cb.on_status(summary)
        self.cb.on_log(f"\n[DONE] {summary}")
        self.cb.on_log(f"  输出目录: {os.path.abspath(book_dir)}")

    # ══════════════════════════════════════════════════════════════
    # 并行下载 — 文件下载任务
    # ══════════════════════════════════════════════════════════════

    def _download_task(
        self,
        chapter: Chapter,
        audio_url: str,
        filepath: str,
        filename: str,
        referer: str,
    ) -> Tuple[bool, int]:
        """
        在线程池中执行的下载任务 (带无限重试)

        Returns:
            (成功, 文件大小 KB)
        """
        dl_attempt = 0
        while not self.cb.is_stopped():
            dl_attempt += 1
            ok = download_file(
                audio_url, filepath,
                referer=referer,
                cdn_direct=True,
            )
            if ok:
                fsize = os.path.getsize(filepath) // 1024
                chapter.downloaded = True
                return (True, fsize)

            # 重试
            if os.path.exists(filepath + ".tmp"):
                try:
                    os.remove(filepath + ".tmp")
                except OSError:
                    pass

            wait = min(10 * dl_attempt, 60)
            time.sleep(wait)

        return (False, 0)

    def _collect_completed(self) -> Tuple[int, int]:
        """收割已完成的下载任务, 返回 (成功数, 失败数)"""
        done_keys = [
            idx for idx, (_, _, fut) in self._pending_dl.items()
            if fut.done()
        ]
        s, f = 0, 0
        for idx in done_keys:
            chapter, filename, future = self._pending_dl.pop(idx)
            try:
                ok, fsize = future.result(timeout=0)
                if ok:
                    self.cb.on_log(f"  [OK] {filename} ({fsize} KB)")
                    s += 1
                else:
                    self.cb.on_log(f"  [FAIL] {filename}")
                    f += 1
            except Exception as e:
                self.cb.on_log(f"  [FAIL] {filename}: {e}")
                f += 1
        return s, f

    # ══════════════════════════════════════════════════════════════
    # URL 预取流水线
    # ══════════════════════════════════════════════════════════════

    def _prefetch_url(self, chapter: Chapter) -> Optional[str]:
        """在后台线程中预取 URL (仅轻量 API, 不触发验证码)"""
        try:
            return self.source.prefetch_audio_url(chapter)
        except Exception:
            return None

    def _start_prefetch(self, chapters: List[Chapter], next_i: int,
                        book_dir: str):
        """启动对下一章的 URL 预取 (非阻塞)"""
        if next_i >= len(chapters):
            return
        next_ch = chapters[next_i]
        try:
            existing = [
                f for f in os.listdir(book_dir)
                if f.startswith(f"{next_ch.index:04d}_")
            ]
            if existing:
                return
        except OSError:
            pass

        self._prefetch_chapter_idx = next_ch.index
        self._prefetch_future = self._prefetch_pool.submit(
            self._prefetch_url, next_ch,
        )

    def _consume_prefetch(self, chapter: Chapter) -> Optional[str]:
        """消费预取结果"""
        if (self._prefetch_future is None
                or self._prefetch_chapter_idx != chapter.index):
            self._cancel_prefetch()
            return None

        future = self._prefetch_future
        self._prefetch_future = None
        self._prefetch_chapter_idx = -1

        try:
            url = future.result(timeout=10)
            if url and url != "RATE_LIMITED" and is_valid_audio_url(url):
                self._consecutive_ok += 1
                self._consecutive_fail = 0
                return url
        except Exception:
            pass
        return None

    def _cancel_prefetch(self):
        """取消进行中的预取"""
        if self._prefetch_future and not self._prefetch_future.done():
            self._prefetch_future.cancel()
        self._prefetch_future = None
        self._prefetch_chapter_idx = -1

    # ══════════════════════════════════════════════════════════════
    # 获取 URL (带无限重试)
    # ══════════════════════════════════════════════════════════════

    def _fetch_url_with_retry(self, chapter: Chapter) -> Optional[str]:
        """获取音频 URL, 无限重试直到成功或被停止"""
        attempt = 0
        while not self.cb.is_stopped():
            attempt += 1
            try:
                audio_url = self.source.get_audio_url(chapter)
            except Exception as e:
                self.cb.on_log(f"  [!] 获取出错: {e}")
                audio_url = None

            if audio_url and audio_url != "RATE_LIMITED":
                self._consecutive_ok += 1
                self._consecutive_fail = 0
                return audio_url

            if audio_url == "RATE_LIMITED":
                self._record_rate_limit()

            self._consecutive_fail += 1
            self._consecutive_ok = 0

            if attempt <= 2:
                wait = 15
            elif attempt <= 4:
                wait = 30
            else:
                wait = 60 + random.randint(0, 30)

            self.cb.on_log(f"  [!] 第{attempt}次失败, 等待 {wait}s...")
            self._interruptible_sleep(wait)

            if attempt % 2 == 0 and self.clash_rotator:
                new_node = self.clash_rotator.rotate()
                if new_node:
                    self.cb.on_log(f"  [*] 已切换节点: {new_node}")
                    self._interruptible_sleep(3)

        return None

    # ══════════════════════════════════════════════════════════════
    # 自适应延迟
    # ══════════════════════════════════════════════════════════════

    def _interruptible_sleep(self, seconds):
        """可被 stop 中断的 sleep"""
        end = time.time() + seconds
        while time.time() < end:
            if self.cb.is_stopped():
                return
            time.sleep(min(1.0, end - time.time()))

    def _anti_rate_limit_delay(self, i: int, total: int):
        """
        自适应反限流延迟 + 主动 Clash 换 IP

        双模式:
        - 快速 (从未限流): 每章 2~4s, 每 15 章休 15~25s
        - 保守 (曾被限流): 每章 4~8s, 每 10 章休 30~60s
        """
        chapter_num = i + 1

        if self._rate_limit_count == 0:
            batch_interval = 15
            if chapter_num % batch_interval == 0:
                pause = random.uniform(15, 25)
                self.cb.on_log(
                    f"  [*] 已完成 {chapter_num} 集, "
                    f"休息 {pause:.0f}s 防限流..."
                )
                self._interruptible_sleep(pause)
            else:
                delay = random.uniform(2.0, 4.0)
                self._interruptible_sleep(delay)
        else:
            multiplier = 1.0 + min(self._rate_limit_count, 3) * 0.5
            batch_interval = 10
            if chapter_num % batch_interval == 0:
                pause = random.uniform(30, 60) * multiplier
                self.cb.on_log(
                    f"  [*] 已完成 {chapter_num} 集, "
                    f"休息 {pause:.0f}s 防限流..."
                )
                self._interruptible_sleep(pause)
            else:
                delay = random.uniform(4.0, 8.0) * multiplier
                self._interruptible_sleep(delay)

        if (self.clash_rotator and self.rotate_interval > 0
                and chapter_num % self.rotate_interval == 0):
            new_node = self.clash_rotator.rotate()
            if new_node:
                self.cb.on_log(f"  [*] 已切换代理节点: {new_node}")
                self._interruptible_sleep(2)

    def _record_rate_limit(self):
        """记录一次限流事件 (供自适应延迟使用)"""
        self._rate_limit_count += 1
        self._consecutive_ok = 0

    def _save_record(self, book: BookInfo, chapters: List[Chapter], book_dir: str):
        """保存下载记录 JSON"""
        import json
        record = {
            "title": book.title,
            "source": self.source.name,
            "chapters": [
                {
                    "index": ch.index,
                    "title": ch.title,
                    "play_url": ch.play_url,
                    "audio_url": ch.audio_url,
                    "downloaded": ch.downloaded,
                }
                for ch in chapters
            ],
        }
        record_path = os.path.join(book_dir, "download_record.json")
        with open(record_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
