"""
yuetingba.cn 有声小说源

特点:
- SPA + iframe 架构, 音频URL通过JS动态解密获取
- 反调试: disable-devtool 脚本 (需拦截阻止)
- 章节列表: HTML分页 (/book/detail/{book_id}/{offset}), 每页200集
- 音频: MP3 直链 (CDN, 带 token+expire)
- 爬取方式: Playwright 拦截网络请求获取音频URL
- 浏览器生命周期: 每5章自动重启, 规避SPA内部限流
"""

import re
import time
import threading
from typing import Dict, List, Optional
from urllib.parse import urljoin

from .base import Source
from ting13.core.models import BookInfo, Chapter

_playwright_available = True
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    _playwright_available = False

BASE = "http://www.yuetingba.cn"
CHAPTERS_PER_PAGE = 200
BROWSER_LIFETIME = 5
DETAIL_WAIT = 5000
CHAPTER_WAIT = 5000


def _extract_chapter_num(title: str) -> int:
    m = re.match(r'^(\d+)', title)
    return int(m.group(1)) if m else 0


class YuetingbaSource(Source):
    """yuetingba.cn 有声小说"""

    match = [r"yuetingba\.cn"]
    names = ["yuetingba.cn"]
    base_url = BASE

    def __init__(self):
        super().__init__()
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._headless = True
        self._lock = threading.Lock()
        self._book_id: str = ""
        self._chapters_since_restart: int = 0
        self._current_offset: int = 0

    # ── URL 识别 ──

    def detect_url_type(self, url: str) -> str:
        if "/book/ting/" in url:
            return "play"
        elif "/book/detail/" in url:
            return "book"
        return "unknown"

    # ── 页面解析 ──

    def parse_book(self, url: str) -> BookInfo:
        import requests
        import urllib3
        urllib3.disable_warnings()

        m = re.search(r'/book/detail/([a-f0-9-]+)', url)
        if not m:
            raise ValueError(f"无法从URL提取书籍ID: {url}")
        self._book_id = m.group(1)

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        session.verify = False

        first_url = f"{BASE}/book/detail/{self._book_id}/0"
        resp = session.get(first_url, timeout=30)
        resp.encoding = "utf-8"
        html = resp.text

        if len(html) < 500:
            raise ConnectionError(f"获取书籍页面失败, 响应长度: {len(html)}")

        title = self._extract_title(html)
        author = self._extract_author(html)
        announcer = self._extract_announcer(html)
        cover_url = self._extract_cover(html)

        page_offsets = [0]
        for om in re.findall(rf'/book/detail/{re.escape(self._book_id)}/(\d+)', html):
            o = int(om)
            if o not in page_offsets:
                page_offsets.append(o)
        page_offsets.sort()

        total_chapters_text = ""
        tc_m = re.search(r'集\s*数：</span>[^<]*<span[^>]*>(\d+)</span>', html)
        if tc_m:
            total_chapters_text = tc_m.group(1)

        chapters = self._parse_chapters_from_html(html)
        self._log_func(f"  [*] 第1页: {len(chapters)} 章")

        for offset in page_offsets[1:]:
            page_url = f"{BASE}/book/detail/{self._book_id}/{offset}"
            try:
                time.sleep(0.3)
                r = session.get(page_url, timeout=30)
                r.encoding = "utf-8"
                page_chapters = self._parse_chapters_from_html(r.text)
                chapters.extend(page_chapters)
                self._log_func(f"  [*] 第{offset // CHAPTERS_PER_PAGE + 1}页: {len(page_chapters)} 章 (offset={offset})")
            except Exception as e:
                self._log_func(f"  [!] 分页 {offset} 解析失败: {e}")

        # 按标题数字排序
        chapters.sort(key=lambda c: _extract_chapter_num(c.title))
        for i, ch in enumerate(chapters):
            ch.index = i + 1

        expected = int(total_chapters_text) if total_chapters_text else len(chapters)
        if expected != len(chapters):
            self._log_func(f"  [!] 章节数不匹配: 页面显示{expected}, 实际解析{len(chapters)}")

        self._log_func(f"  书名: {title}, 作者: {author}, 演播: {announcer}, 共{len(chapters)}集")

        return BookInfo(
            title=title, author=author, announcer=announcer,
            cover_url=cover_url, chapters=chapters,
            source_name=self.names[0],
            extra={"book_id": self._book_id},
        )

    def _extract_title(self, html: str) -> str:
        m = re.search(r'<h1[^>]*class="book-detail-title"[^>]*>([^<]+)</h1>', html)
        return m.group(1).strip() if m else ""

    def _extract_author(self, html: str) -> str:
        m = re.search(r'作\s*者：</span>.*?<a[^>]*>([^<]+)</a>', html)
        if not m:
            m = re.search(r'作\s*者：</span>(.*?)</div>', html, re.DOTALL)
        return re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ""

    def _extract_announcer(self, html: str) -> str:
        m = re.search(r'演\s*播：</span>.*?<a[^>]*>([^<]+)</a>', html)
        if not m:
            m = re.search(r'演\s*播：</span>(.*?)</div>', html, re.DOTALL)
        return re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ""

    def _extract_cover(self, html: str) -> str:
        m = re.search(r'<img\s+src=["\']([^"\']+_thumb\.webp[^"\']*)["\']', html)
        return m.group(1) if m else ""

    def _parse_chapters_from_html(self, html: str) -> List[Chapter]:
        chapters = []
        matches = re.findall(
            r'onclick="testFn\(\'([a-f0-9-]+)\'\)"[^>]*>\s*([^<]+)\s*</a>',
            html
        )
        for cid, title in matches:
            title = title.strip()
            if title:
                chapters.append(Chapter(
                    index=0,
                    title=title,
                    play_url=f"{BASE}/book/ting/{cid}",
                ))

        if not chapters:
            comment_pattern = re.findall(
                r'<!--<a[^>]*href="(/book/ting/[a-f0-9-]+)"[^>]*>([^<]+)</a>-->',
                html
            )
            for href, title in comment_pattern:
                chapters.append(Chapter(
                    index=0,
                    title=title.strip(),
                    play_url=urljoin(BASE, href),
                ))
        return chapters

    # ── 音频 URL 提取 ──

    def prefetch_audio_url(self, chapter: Chapter) -> Optional[str]:
        return None

    def get_audio_url(self, chapter: Chapter) -> Optional[str]:
        if not self._page:
            return None

        with self._lock:
            return self._get_audio_url_impl(chapter)

    def _get_audio_url_impl(self, chapter: Chapter) -> Optional[str]:
        chapter_id = self._extract_chapter_id(chapter.play_url)
        if not chapter_id:
            return None

        # 检查是否需要重启浏览器
        if self._chapters_since_restart >= BROWSER_LIFETIME:
            self._log_func("  [*] 章节数达到限制, 重启浏览器...")
            self._restart_browser()
            self._chapters_since_restart = 0

        # 确保在正确的分页
        need_offset = (chapter.index - 1) // CHAPTERS_PER_PAGE * CHAPTERS_PER_PAGE
        if need_offset != self._current_offset:
            offset_url = f"{BASE}/book/detail/{self._book_id}/{need_offset}"
            self._page.goto(offset_url, wait_until="domcontentloaded", timeout=20000)
            self._page.wait_for_timeout(DETAIL_WAIT)
            self._current_offset = need_offset

        captured_urls: List[str] = []

        def on_request(request):
            url = request.url
            if '.mp3' in url.lower() and url.startswith('http') and 'myfiles' in url:
                captured_urls.append(url)

        self._page.on("request", on_request)
        try:
            selector = f'a[onclick*="testFn(\'{chapter_id}\')"]'
            el = self._page.query_selector(selector)
            if el:
                el.scroll_into_view_if_needed()
                self._page.wait_for_timeout(200)
                el.click()
                self._page.wait_for_timeout(CHAPTER_WAIT)
            else:
                self._page.evaluate(f"""
                    (function() {{
                        const iframe = document.getElementById('iframe_tingPlay');
                        if (iframe && iframe.contentWindow && typeof iframe.contentWindow.testFun === 'function') {{
                            try {{ iframe.contentWindow.testFun('{chapter_id}'); }} catch(e) {{}}
                        }}
                    }})()
                """)
                self._page.wait_for_timeout(CHAPTER_WAIT + 1000)

            if captured_urls:
                self._chapters_since_restart += 1
                return captured_urls[0]
            return None
        except Exception as e:
            self._log_func(f"  [!] 获取音频URL失败: {e}")
            return None
        finally:
            self._page.remove_listener("request", on_request)

    def _extract_chapter_id(self, play_url: str) -> Optional[str]:
        m = re.search(r'/book/ting/([a-f0-9-]+)', play_url)
        return m.group(1) if m else None

    # ── 浏览器管理 ──

    def _ensure_browser_started(self, callbacks=None):
        """启动浏览器（如果尚未启动）"""
        if self._page is not None:
            return
        cb = callbacks or (lambda msg: print(msg))
        cb("[*] 启动浏览器...")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        )
        self._context = self._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            ignore_https_errors=True,
        )
        self._page = self._context.new_page()
        self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {} };
            const oq = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : oq(p);
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
        """)
        self._page.route("**/*", self._route_handler)

    def _route_handler(self, route):
        url = route.request.url.lower()
        if 'disable-devtool' in url:
            route.abort()
            return
        if 'google' in url and ('ads' in url or 'fundingchoices' in url or 'googlesyndication' in url):
            route.abort()
            return
        route.continue_()

    def _load_detail_page(self, offset=0):
        """加载书籍详情页"""
        detail_url = f"{BASE}/book/detail/{self._book_id}/{offset}"
        self._log_func(f"  [*] 加载详情页: offset={offset}")
        self._page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_timeout(DETAIL_WAIT)
        self._current_offset = offset

    def _restart_browser(self):
        """重启浏览器"""
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        self._chapters_since_restart = 0
        self._current_offset = -1
        self._ensure_browser_started()
        self._load_detail_page(0)

    # ── 生命周期 ──

    def before_download(self, chapters, callbacks):
        super().before_download(chapters, callbacks)

        if not _playwright_available:
            callbacks.on_log("[FAIL] Playwright 未安装, 无法下载 yuetingba.cn")
            return

        if not self._book_id and hasattr(self, '_book_info') and self._book_info:
            self._book_id = self._book_info.extra.get("book_id", "")

        self._ensure_browser_started(callbacks.on_log)
        self._load_detail_page(0)
        callbacks.on_log(f"  [*] 页面标题: {self._page.title()}")
        self._chapters_since_restart = 0

    def after_download(self):
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        super().after_download()