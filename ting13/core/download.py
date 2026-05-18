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
import re
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from .models import BookInfo, Chapter
from .network import build_session, get_proxy, ClashRotator, random_ua, ProxyPool, get_proxy_pool
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


def _build_chapter_filename(index: int, title: str, ext: str) -> str:
    """统一命名: 序号+章节内容"""
    return f"{index}_{sanitize_filename(title)}{ext}"


def _is_chapter_file(filename: str, index: int) -> bool:
    """兼容旧命名(0001_)和新命名(1_或1集_)，用于去重判断。"""
    old_prefix = f"{index:04d}_"
    new_prefix1 = f"{index}集_"
    new_prefix2 = f"{index}_"
    return filename.startswith(old_prefix) or filename.startswith(new_prefix1) or filename.startswith(new_prefix2)


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
    """扫描目录中已下载的章节编号 (根据文件名前缀 0001_、1_ 或 1集_)"""
    downloaded = set()
    try:
        for f in os.listdir(book_dir):
            # 匹配 0001_、1_ 或 1集_ 格式
            m = re.match(r"^(\d+)(集_)?", f)
            if m:
                downloaded.add(int(m.group(1)))
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
    # 单集 URL 获取最大重试次数
    MAX_URL_RETRIES = 3
    # 单集文件下载最大重试次数
    MAX_DOWNLOAD_RETRIES = 10
    # 并发 URL 获取线程数（仅代理池模式有效）
    URL_FETCH_WORKERS = 3

    def __init__(
        self,
        source: "Source",
        callbacks: DownloadCallbacks,
        clash_rotator: Optional[ClashRotator] = None,
        proxy_pool: Optional[ProxyPool] = None,
        rotate_interval: int = 0,
        download_workers: int = 0,
        url_fetch_workers: int = 0,
        fast_mode: bool = False,
        batch_fetch: bool = False,
        extra_delay: float = 0.0,
    ):
        self.source = source
        self.cb = callbacks
        self.clash_rotator = clash_rotator
        self.proxy_pool = proxy_pool
        self.fast_mode = fast_mode
        self.batch_fetch = batch_fetch
        self.extra_delay = extra_delay
        self._proxy_pool_failed_count = 0
        self._proxy_pool_disabled = False
        self._proxy_pool_retry_interval = 20  # 每20章重试一次代理池
        self._chapters_since_proxy_pool_disabled = 0  # 代理池禁用后已下载的章节数
        self._total_switch_failures = 0  # 总的切换失败次数
        self._max_switch_failures = 4  # 最大切换失败次数

        # 如果用户没有设置, 且有 Clash 或代理池, 则使用默认间隔
        if rotate_interval > 0:
            self.rotate_interval = rotate_interval
        elif clash_rotator or proxy_pool:
            self.rotate_interval = self.DEFAULT_ROTATE_INTERVAL
        else:
            self.rotate_interval = 0

        # 工作线程数配置
        self.download_workers = download_workers if download_workers > 0 else self.DOWNLOAD_WORKERS
        self.url_fetch_workers = url_fetch_workers if url_fetch_workers > 0 else self.URL_FETCH_WORKERS

        # 自适应延迟状态
        self._consecutive_ok = 0
        self._consecutive_fail = 0
        self._rate_limit_count = 0

        # URL 预取 (代理池模式下可用多线程)
        prefetch_workers = self.url_fetch_workers if proxy_pool else 1
        self._prefetch_pool = ThreadPoolExecutor(max_workers=prefetch_workers)
        self._prefetch_futures: Dict[int, Future] = {}  # 存储多个预取任务

        # 并行下载池 (CDN 不限流, 可多线程)
        self._dl_pool = ThreadPoolExecutor(max_workers=self.download_workers)
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

        start_idx = max(0, start - 1)
        end_idx = end if end else len(chapters)
        chapters = chapters[start_idx:end_idx]

        source_tag = f"[{self.source.name}]"
        self.cb.on_info(
            f"{source_tag} {book.title}   "
            f"第{start}~{start + len(chapters) - 1}集 (共{len(chapters)}集)"
        )

        if hasattr(self.source, 'check_login_required'):
            if self.source.check_login_required(chapters, self.cb):
                if hasattr(self.source, 'prompt_login'):
                    self.source.prompt_login(self.cb)
                self.cb.on_status("需要登录")
                return

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

        if self.rotate_interval > 0 and (self.proxy_pool or self.clash_rotator):
            proxy_type = "代理池" if self.proxy_pool else "Clash"
            self.cb.on_log(
                f"[*] 自动换IP已启用 ({proxy_type}): 每 {self.rotate_interval} 集切换节点"
            )

        mode_desc = []
        if self.batch_fetch:
            mode_desc.append("📦 批量预获取URL")
        if self.fast_mode:
            mode_desc.append("⚡ 极速模式")
        mode_desc.append(f"{self.download_workers} 线程下载")
        if self.proxy_pool:
            mode_desc.append(f"{self.url_fetch_workers} 线程获取URL")
        else:
            mode_desc.append("URL预取")
        mode_desc.append("CDN直连")
        if not self.fast_mode and not self.batch_fetch:
            mode_desc.append("自适应延迟")
        
        self.cb.on_log(
            f"[*] 并行模式: {' + '.join(mode_desc)}\n"
        )

        # ── 调用 Source 的准备钩子 ──
        self.source.before_download(chapters, self.cb)

        # ── 批量预获取所有 URL (可选) ──
        if self.batch_fetch and self.proxy_pool:
            chapters = self._batch_fetch_all_urls(chapters, book_dir)
            if not chapters:
                return

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
                while (len(self._pending_dl) >= self.download_workers
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
                        if _is_chapter_file(f, chapter.index)
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

                filename = _build_chapter_filename(chapter.index, chapter.title, ext)
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
                if not self.batch_fetch:
                    self._start_prefetch(chapters, i + 1, book_dir)

                # ── 反限流延迟 (只控制 API 调用节奏) ──
                if not self.batch_fetch:
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

    def run_streaming(
        self,
        chapter_queue,
        parse_done_event,
        book_info: dict,
        output_dir: str,
        start: int = 1,
        end: Optional[int] = None,
    ):
        """
        流式下载引擎：边解析边下载（生产者-消费者模式）+ 完整断点续传

        特性：
        - ✅ 自动检测并跳过已下载章节
        - ✅ 智能续传：从上次停止的地方继续
        - ✅ 缺失优先：先补齐缺失章节
        - ✅ 完整性保证：去重+连续性检查
        - ✅ 详细日志：显示跳过/新增/缺失统计

        Args:
            chapter_queue: queue.Queue，用于接收新章节（由外部解析线程持续填充）
            parse_done_event: threading.Event，标记解析是否完成
            book_info: 书籍元信息 {"title", "author", "cover_url"}
            output_dir: 输出目录
            start: 起始章节（1-based）
            end: 结束章节（None表示不限）
        """
        import queue as _queue

        title = book_info.get("title", "未知书名")
        author = book_info.get("author", "")
        cover_url = book_info.get("cover_url", "")

        self.cb.on_log(f"\n{'='*60}")
        self.cb.on_log(f"  🚀 流式下载模式 (增强版): 边解析边下载 + 断点续传")
        self.cb.on_log(f"  📖 {title} - {author}")
        self.cb.on_log(f"{'='*60}\n")

        # 创建输出目录
        book_dir = os.path.join(output_dir, sanitize_filename(title))
        os.makedirs(book_dir, exist_ok=True)
        self.cb.on_log(f"[*] 输出目录: {os.path.abspath(book_dir)}")

        # 下载封面
        if cover_url:
            from .utils import download_cover
            referer = self.source.base_url
            cover = download_cover(cover_url, book_dir, referer=referer)
            if cover:
                self.cb.on_log("[*] 封面已保存")

        # ════════════════════════════════════════════════════════════
        # ★ 核心1: 扫描已下载章节（断点续传基础）
        # ════════════════════════════════════════════════════════════
        downloaded_indices = scan_downloaded(book_dir)

        if downloaded_indices:
            min_dl = min(downloaded_indices)
            max_dl = max(downloaded_indices)
            total_dl = len(downloaded_indices)
            self.cb.on_log(
                f"\n[*] 🔍 检测到 {total_dl} 个已下载章节 "
                f"(范围: 第{min_dl}~{max_dl}集)"
            )
            self.cb.on_status(f"继续下载 (已有{total_dl}集)")
        else:
            self.cb.on_log("\n[*] 未检测到已下载章节，全新下载")
            self.cb.on_status("开始下载")

        # 初始化统计
        success = 0
        fail = 0
        total_downloaded = 0
        all_chapters_buffer = []
        received_total = 0  # 接收到的总章节数
        skipped_already = 0   # 跳过已下载的数量
        skipped_duplicate = 0 # 跳过重复的数量
        new_to_download = 0   # 需要新下载的数量
        seen_urls_global = set()  # 全局URL去重集合

        # ── 调用 Source 的准备钩子 ──
        dummy_chapters = []
        self.source.before_download(dummy_chapters, self.cb)

        try:
            i = 0  # 全局章节计数器（包含所有接收到的）

            while True:
                # 检查停止信号
                if self.cb.is_stopped():
                    self.cb.on_log("[!] 用户请求停止")
                    break

                # 从队列中获取新章节（带超时）
                try:
                    new_chapters = chapter_queue.get(timeout=2.0)
                except _queue.Empty:
                    # 队列为空，检查是否解析完成
                    if parse_done_event.is_set():
                        self.cb.on_log("\n[*] 解析已完成，等待剩余下载任务...")
                        break
                    else:
                        # 解析还在继续，打印等待信息
                        if i == 0 and total_downloaded == 0:
                            self.cb.on_status("等待首批章节...")
                        continue

                # 处理特殊信号（结束标志）
                if new_chapters is None:
                    break

                # 将新章节添加到缓冲区
                all_chapters_buffer.extend(new_chapters)
                received_total += len(new_chapters)
                current_buffer_size = len(all_chapters_buffer)

                self.cb.on_log(
                    f"\n[>] 收到 {len(new_chapters)} 个新章节 "
                    f"(累计接收: {received_total}, 缓冲区: {current_buffer_size})"
                )

                # ═══════════════════════════════════════════════════
                # ★ 核心2: 智能过滤 - 立即跳过已下载+重复章节
                # ═══════════════════════════════════════════════════
                chapters_to_process = []
                for ch in all_chapters_buffer:
                    # 全局URL去重（防止增量模式下重复发送同一章）
                    if ch.play_url in seen_urls_global:
                        skipped_duplicate += 1
                        continue

                    seen_urls_global.add(ch.play_url)

                    # 跳过已下载章节（断点续传核心！）
                    # 注意：这里使用动态分配的全局索引
                    global_index = len(seen_urls_global)  # 基于全局顺序分配索引
                    ch.index = global_index

                    if global_index in downloaded_indices:
                        skipped_already += 1
                        if skipped_already <= 3 or skipped_already % 50 == 0:
                            self.cb.on_log(
                                f"  [SKIP] 已存在: 第{global_index}集 {ch.title}"
                            )
                        continue

                    # 需要新下载
                    chapters_to_process.append(ch)
                    new_to_download += 1

                # 清空缓冲区（已处理完毕）
                all_chapters_buffer.clear()

                if not chapters_to_process:
                    if skipped_already > 0:
                        self.cb.on_log(
                            f"  [*] 本批全部跳过 (已下载:{skipped_already}, 重复:{skipped_duplicate})"
                        )
                    continue

                self.cb.on_log(
                    f"  [*] 待处理: {len(chapters_to_process)} 章 "
                    f"(历史跳过-已下载:{skipped_already}, 重复:{skipped_duplicate})"
                )

                # 处理需要下载的章节
                for chapter in chapters_to_process:
                    if self.cb.is_stopped():
                        break

                    i += 1

                    # 应用起止范围过滤
                    effective_idx = chapter.index  # 使用全局索引
                    if effective_idx < start:
                        continue
                    if end and effective_idx > end:
                        break

                    # 收割已完成的下载（非阻塞）
                    s, f = self._collect_completed()
                    success += s
                    fail += f
                    total_downloaded += s + f

                    # 控制并发：如果满了，等一个完成
                    while (len(self._pending_dl) >= self.download_workers
                           and not self.cb.is_stopped()):
                        time.sleep(0.2)
                        s, f = self._collect_completed()
                        success += s
                        fail += f
                        total_downloaded += s + f

                    # 更新进度
                    progress_val = min(total_downloaded / max(new_to_download, 1), 0.99)
                    self.cb.on_progress(progress_val, f"{total_downloaded}/{new_to_download}")
                    self.cb.on_status(
                        f"下载中 [已完成:{total_downloaded}/待下:{new_to_download}]  "
                        f"{chapter.title}"
                    )
                    self.cb.on_log(
                        f"[{total_downloaded+1}/{new_to_download}] "
                        f"第{effective_idx}集 {chapter.title}"
                    )

                    # ═══════════════════════════════════════════════════
                    # ★ 核心3: 二次检查是否已下载（防并发重复）
                    # ═══════════════════════════════════════════════════
                    try:
                        existing = [
                            f for f in os.listdir(book_dir)
                            if _is_chapter_file(f, chapter.index)
                        ]
                    except OSError:
                        existing = []

                    if existing:
                        self.cb.on_log(f"  [SKIP] 已存在: {existing[0]}")
                        downloaded_indices.add(chapter.index)
                        success += 1
                        total_downloaded += 1
                        continue

                    # 获取音频 URL
                    audio_url = self._consume_prefetch(chapter)
                    if audio_url:
                        self.cb.on_log("  [>] 获取URL... (预取)")
                    else:
                        self.cb.on_log("  [>] 获取URL...")
                        audio_url = self._fetch_url_with_retry(chapter)

                    if self.cb.is_stopped() or not audio_url:
                        if not self.cb.is_stopped():
                            fail += 1
                            total_downloaded += 1
                        continue

                    if not is_valid_audio_url(audio_url):
                        self.cb.on_log(f"  [!] 无效音频 URL: {audio_url}")
                        fail += 1
                        total_downloaded += 1
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

                    filename = _build_chapter_filename(chapter.index, chapter.title, ext)
                    filepath = os.path.join(book_dir, filename)

                    # 提交下载到线程池
                    cdn_tag = " (CDN直连)" if _is_cdn_url(audio_url) else ""
                    self.cb.on_log(f"  [>>] 提交下载: {filename}{cdn_tag}")
                    future = self._dl_pool.submit(
                        self._download_task, chapter, audio_url,
                        filepath, filename, self.source.base_url,
                    )
                    self._pending_dl[chapter.index] = (chapter, filename, future)

                    # 反限流延迟
                    self._anti_rate_limit_delay(i, max(i, 1))

            # 等待所有剩余下载完成
            if self._pending_dl and not self.cb.is_stopped():
                remaining = len(self._pending_dl)
                self.cb.on_log(f"\n[*] 等待 {remaining} 个下载完成...")

            while self._pending_dl and not self.cb.is_stopped():
                time.sleep(0.5)
                s, f = self._collect_completed()
                success += s
                fail += f
                total_downloaded += s + f

        finally:
            self._cancel_prefetch()
            self._prefetch_pool.shutdown(wait=False)
            self._dl_pool.shutdown(wait=True)
            self.source.after_download()

        # ════════════════════════════════════════════════════════════
        # ★ 核心4: 完整性校验 & 最终报告
        # ════════════════════════════════════════════════════════════
        final_total = total_downloaded + fail

        # 重新扫描最终状态
        final_downloaded = scan_downloaded(book_dir)
        final_count = len(final_downloaded)

        # 保存下载记录
        record_book = BookInfo(
            title=title,
            author=author,
            cover_url=cover_url,
            source_name=self.source.name,
        )
        self._save_record(record_book, [], book_dir)

        # 生成详细报告
        self.cb.on_progress(1, "完成")
        stopped = " (已停止)" if self.cb.is_stopped() else ""

        summary = (
            f"完成{stopped}\n"
            f"─────────────────────────────────────\n"
            f"📊 本次会话:\n"
            f"  • 新下载成功: {success} 章\n"
            f"  • 下载失败: {fail} 章\n"
            f"  • 跳过已下载: {skipped_already} 章\n"
            f"  • 跳过重复: {skipped_duplicate} 章\n"
            f"  • 接收总数: {received_total} 章\n"
            f"\n📁 文件系统:\n"
            f"  • 总共拥有: {final_count} 章\n"
            f"  • 输出目录: {os.path.abspath(book_dir)}"
        )
        self.cb.on_status(summary.split('\n')[0])
        self.cb.on_log(f"\n[DONE] {summary}")

        # 完整性警告
        if final_count < received_total and fail > 0:
            missing_count = received_total - final_count + fail
            self.cb.on_log(
                f"\n⚠️  完整性警告: 有 {missing_count} 章可能缺失或失败"
            )
            self.cb.on_log("  建议: 可重新运行以补全缺失章节")

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
        while not self.cb.is_stopped() and dl_attempt < self.MAX_DOWNLOAD_RETRIES:
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
        if not self.cb.is_stopped():
            self.cb.on_log(
                f"  [!] 下载重试超限({self.MAX_DOWNLOAD_RETRIES}): {filename}"
            )
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

    def _prefetch_url(self, chapter: Chapter, use_proxy_idx: int = 0) -> Optional[str]:
        """在后台线程中预取 URL (支持代理池模式下每个线程独立 IP)"""
        try:
            # 代理池模式下，每个预取线程尝试获取独立 IP
            if self.proxy_pool and use_proxy_idx > 0:
                # 简单策略：每 N 个预取任务轮换一次 IP
                if use_proxy_idx % max(1, self.rotate_interval // 2) == 0:
                    self.proxy_pool.rotate()
            return self.source.prefetch_audio_url(chapter)
        except Exception:
            return None

    def _start_prefetch(self, chapters: List[Chapter], next_i: int,
                        book_dir: str):
        """启动 URL 预取（代理池模式下预取多章）"""
        if next_i >= len(chapters):
            return
        
        # 代理池模式：预取接下来的几章
        prefetch_count = self.url_fetch_workers if self.proxy_pool else 1
        
        for offset in range(prefetch_count):
            idx = next_i + offset
            if idx >= len(chapters):
                break
            next_ch = chapters[idx]
            
            # 检查是否已下载
            try:
                existing = [
                    f for f in os.listdir(book_dir)
                    if _is_chapter_file(f, next_ch.index)
                ]
                if existing:
                    continue
            except OSError:
                pass
            
            # 避免重复预取
            if next_ch.index in self._prefetch_futures:
                continue
            
            # 启动预取任务
            future = self._prefetch_pool.submit(
                self._prefetch_url, next_ch, offset
            )
            self._prefetch_futures[next_ch.index] = future

    def _consume_prefetch(self, chapter: Chapter) -> Optional[str]:
        """消费预取结果"""
        if chapter.index not in self._prefetch_futures:
            return None
        
        future = self._prefetch_futures.pop(chapter.index)
        
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
        """取消所有进行中的预取"""
        for future in self._prefetch_futures.values():
            if not future.done():
                future.cancel()
        self._prefetch_futures.clear()

    def _batch_fetch_all_urls(self, chapters: List[Chapter], book_dir: str) -> List[Chapter]:
        """
        批量预获取所有章节的 URL（终极加速方案）
        
        策略：
        - 多线程并发获取（依赖代理池）
        - 每 N 个任务轮换一次 IP
        - 获取完成后直接全速下载（无延迟）
        """
        self.cb.on_log("[*] 📦 开始批量预获取所有音频 URL...")
        self.cb.on_log(f"  并发数: {self.url_fetch_workers}, 总章节: {len(chapters)}")
        
        # 检查哪些需要获取
        to_fetch = []
        for ch in chapters:
            try:
                existing = [
                    f for f in os.listdir(book_dir)
                    if _is_chapter_file(f, ch.index)
                ]
                if not existing:
                    to_fetch.append(ch)
            except OSError:
                to_fetch.append(ch)
        
        if not to_fetch:
            self.cb.on_log("[*] 所有章节均已下载，无需获取 URL")
            return chapters
        
        self.cb.on_log(f"  需获取: {len(to_fetch)} 个 URL\n")
        
        # 批量获取
        from concurrent.futures import as_completed
        
        futures = {}
        completed = 0
        failed = 0
        
        for i, chapter in enumerate(to_fetch):
            if self.cb.is_stopped():
                break
            
            # 轮换 IP
            if self.proxy_pool and (i + 1) % self.rotate_interval == 0:
                new_proxy = self.proxy_pool.rotate()
                if new_proxy:
                    self.cb.on_log(f"  [*] 切换 IP: {new_proxy['ip']}:{new_proxy['port']}")
            
            # 提交任务
            future = self._prefetch_pool.submit(
                self._fetch_url_for_batch, chapter
            )
            futures[future] = chapter
        
        # 收集结果
        self.cb.on_log("\n[*] 等待所有 URL 获取完成...")
        
        for future in as_completed(futures):
            chapter = futures[future]
            try:
                audio_url = future.result(timeout=30)
                if audio_url and is_valid_audio_url(audio_url):
                    chapter.audio_url = audio_url
                    completed += 1
                    self.cb.on_log(f"  [OK] [{chapter.index}] {chapter.title}")
                else:
                    failed += 1
                    self.cb.on_log(f"  [FAIL] [{chapter.index}] {chapter.title}")
            except Exception as e:
                failed += 1
                self.cb.on_log(f"  [FAIL] [{chapter.index}] {chapter.title}: {e}")
        
        self.cb.on_log(f"\n[*] 批量获取完成: 成功 {completed}, 失败 {failed}")
        
        # 过滤掉没有 URL 的章节
        success_chapters = [ch for ch in chapters if ch.audio_url]
        return success_chapters

    def _fetch_url_for_batch(self, chapter: Chapter) -> Optional[str]:
        """批量获取模式下的单个 URL 获取（带重试）"""
        attempt = 0
        while not self.cb.is_stopped() and attempt < self.MAX_URL_RETRIES:
            attempt += 1
            try:
                audio_url = self.source.get_audio_url(chapter)
                if audio_url and audio_url != "RATE_LIMITED":
                    return audio_url
                if audio_url == "RATE_LIMITED":
                    self._record_rate_limit()
            except Exception:
                pass
            
            # 重试
            wait = min(5 * attempt, 30)
            time.sleep(wait)
            
            # 失败时换 IP
            if attempt % 2 == 0 and self.proxy_pool:
                self.proxy_pool.rotate()
        
        return None

    # ══════════════════════════════════════════════════════════════
    # 获取 URL (带无限重试)
    # ══════════════════════════════════════════════════════════════

    def _fetch_url_with_retry(self, chapter: Chapter) -> Optional[str]:
        """获取音频 URL, 失败重试后跳过当前章节（支持智能代理轮换）"""
        attempt = 0
        
        # 检查是否需要重试代理池
        if self._proxy_pool_disabled and self.proxy_pool:
            self._chapters_since_proxy_pool_disabled += 1
            if self._chapters_since_proxy_pool_disabled >= self._proxy_pool_retry_interval:
                self.cb.on_log(f"  [*] 尝试重新启用代理池（已禁用 {self._chapters_since_proxy_pool_disabled} 章）")
                self._proxy_pool_disabled = False
                self._proxy_pool_failed_count = 0
                self._chapters_since_proxy_pool_disabled = 0
                # 测试代理池是否可用
                test_proxy = self.proxy_pool.get_proxy()
                if test_proxy:
                    self.cb.on_log(f"  [OK] 代理池已重新启用，当前IP: {test_proxy['ip']}:{test_proxy['port']}")
                else:
                    self.cb.on_log(f"  [!] 代理池测试失败，继续使用Clash")
                    self._proxy_pool_disabled = True

        while not self.cb.is_stopped() and attempt < self.MAX_URL_RETRIES:
            attempt += 1
            try:
                audio_url = self.source.get_audio_url(chapter)
            except Exception as e:
                self.cb.on_log(f"  [!] 获取出错: {e}")
                audio_url = None

            if audio_url and audio_url != "RATE_LIMITED":
                self._consecutive_ok += 1
                self._consecutive_fail = 0
                self._total_switch_failures = 0  # 重置失败计数
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

            if attempt % 2 == 0:
                switch_success = False
                
                if self.proxy_pool and not self._proxy_pool_disabled:
                    # 优先尝试代理池
                    new_proxy = self.proxy_pool.rotate()
                    if new_proxy:
                        self.cb.on_log(f"  [*] 已切换代理池 IP: {new_proxy['ip']}:{new_proxy['port']}")
                        self._interruptible_sleep(3)
                        self._proxy_pool_failed_count += 1
                        switch_success = True
                        
                        if self._proxy_pool_failed_count >= 3 and self.clash_rotator:
                            self.cb.on_log(f"  [!] 代理池连续{self._proxy_pool_failed_count}次失败，自动切换到Clash备用")
                            self._proxy_pool_disabled = True
                            self._chapters_since_proxy_pool_disabled = 0
                            new_node = self.clash_rotator.rotate()
                            if new_node:
                                self.cb.on_log(f"  [*] 已切换到 Clash 节点: {new_node}")
                                self._interruptible_sleep(3)
                                switch_success = True
                elif self.clash_rotator:
                    # 使用Clash
                    new_node = self.clash_rotator.rotate()
                    if new_node:
                        self.cb.on_log(f"  [*] 已切换 Clash 节点: {new_node}")
                        self._interruptible_sleep(3)
                        switch_success = True
                
                # 记录切换失败
                if not switch_success:
                    self._total_switch_failures += 1
                    self.cb.on_log(f"  [!] 代理切换失败（{self._total_switch_failures}/{self._max_switch_failures}）")
                    
                    # 检查是否达到最大失败次数
                    if self._total_switch_failures >= self._max_switch_failures:
                        self.cb.on_log("\n" + "=" * 60)
                        self.cb.on_log("  ⚠️  警告：连续4次代理切换失败！")
                        self.cb.on_log("  建议检查：")
                        self.cb.on_log("    1. 代理池服务是否正常运行")
                        self.cb.on_log("    2. Clash服务是否正常运行")
                        self.cb.on_log("    3. 网络连接是否正常")
                        self.cb.on_log("  下载将继续，但可能较慢...")
                        self.cb.on_log("=" * 60 + "\n")
        
        if not self.cb.is_stopped():
            self.cb.on_log(
                f"  [!] URL获取重试超限({self.MAX_URL_RETRIES})，跳过本集"
            )
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
        自适应反限流延迟 + 主动换 IP

        双模式:
        - 极速模式 (代理池): 最小延迟，依赖多 IP 分散压力
        - 快速 (从未限流): 每章 2~4s, 每 15 章休 15~25s
        - 保守 (曾被限流): 每章 4~8s, 每 10 章休 30~60s
        """
        chapter_num = i + 1

        # 应用额外延迟（用于多进程并发防限流）
        if self.extra_delay > 0:
            self._interruptible_sleep(self.extra_delay)

        if self.fast_mode and self.proxy_pool:
            # 极速模式：最小延迟，依赖多 IP 分散压力
            if chapter_num % self.rotate_interval == 0:
                pause = random.uniform(1, 3)
                self.cb.on_log(
                    f"  [*] 已完成 {chapter_num} 集, "
                    f"短暂休息 {pause:.0f}s..."
                )
                self._interruptible_sleep(pause)
            else:
                delay = random.uniform(0.3, 0.8)
                self._interruptible_sleep(delay)
        elif self._rate_limit_count == 0:
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

        if (self.rotate_interval > 0
                and chapter_num % self.rotate_interval == 0):
            if self.proxy_pool:
                new_proxy = self.proxy_pool.rotate()
                if new_proxy:
                    self.cb.on_log(f"  [*] 已切换代理池 IP: {new_proxy['ip']}:{new_proxy['port']}")
                    self._interruptible_sleep(2)
            elif self.clash_rotator:
                new_node = self.clash_rotator.rotate()
                if new_node:
                    self.cb.on_log(f"  [*] 已切换 Clash 节点: {new_node}")
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
