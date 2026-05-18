"""
ting13.cc 有声小说源

特点:
- 使用 Playwright 浏览器自动化提取音频 URL
- 支持登录 (cookies) 和未登录两种模式
- 桌面版 API: /api/mapi/play (需登录)
- 移动版 API: /api/key/readplay (免费章节)
- 线程安全：使用锁保护 Playwright Page 操作
"""

import json
import os
import re
import time
import threading
from typing import Dict, List, Optional
from urllib.parse import urljoin

import lxml.html

from .base import Source
from ting13.core.models import BookInfo, Chapter
from ting13.core.network import build_session, fetch_page, fetch_json, get_proxy, MOBILE_UA, DEFAULT_UA, _TING13_WORKING_IP, _close_ting13_connection, _fetch_via_ting13_sni
from ting13.core.utils import is_frozen, get_bundled_base, get_chrome_exe_path

# Playwright 延迟导入 (仅在需要时)
_playwright_available = True
try:
    from playwright.sync_api import sync_playwright, Page
except ImportError:
    _playwright_available = False

# curl_cffi 用于绕过 Cloudflare TLS 指纹检测 (可选但强烈推荐)
_HAS_CFFI = False
_cffi_session = None
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    pass


# ══════════════════════════════════════════════════════════════
# Cookie 管理 (ting13.cc 登录)
# ══════════════════════════════════════════════════════════════

_COOKIE_FILE = os.path.join(os.path.expanduser("~"), ".ting13_cookies.json")
_cookies_store: List[dict] = []
_cookies_loaded: bool = False  # 标记是否已从文件加载过


def save_cookies(cookies: List[dict]):
    global _cookies_store
    _cookies_store = cookies
    try:
        with open(_COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_cookies() -> List[dict]:
    global _cookies_store, _cookies_loaded
    if _cookies_store:
        return _cookies_store
    if _cookies_loaded:
        return []
    try:
        if os.path.isfile(_COOKIE_FILE):
            with open(_COOKIE_FILE, "r", encoding="utf-8") as f:
                _cookies_store = json.load(f)
                _cookies_loaded = True
                return _cookies_store
    except Exception:
        pass
    _cookies_loaded = True
    return []


def clear_cookies():
    global _cookies_store, _cookies_loaded
    _cookies_store = []
    _cookies_loaded = False
    try:
        if os.path.isfile(_COOKIE_FILE):
            os.remove(_COOKIE_FILE)
    except Exception:
        pass


def has_cookies() -> bool:
    return bool(load_cookies())


def _cookies_for_requests() -> dict:
    cookies = load_cookies()
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}


# ══════════════════════════════════════════════════════════════
# 音频 URL 过滤
# ══════════════════════════════════════════════════════════════

_TRUSTED_DOMAINS = ["ysxs.top", "ting13.cc", "tingchina.com", "xmcdn.com", "cos.tx.xmcdn.com"]
_BLACKLISTED_DOMAINS = [
    "ximalaya.com", "qtfm.cn", "lrts.me",
    "kaolafm.net", "kugou.com", "kuwo.cn", "163.com",
    "qqmusic.qq.com", "douyin.com", "bytedance",
    "googlesyndication", "googleads",
]


def _is_trusted_audio_url(url: str) -> bool:
    url_lower = url.lower()
    return any(domain in url_lower for domain in _TRUSTED_DOMAINS)


def _is_blacklisted_audio_url(url: str) -> bool:
    url_lower = url.lower()
    return any(domain in url_lower for domain in _BLACKLISTED_DOMAINS)


def _pick_best_audio_url(audio_urls: List[str]) -> Optional[str]:
    """从候选 URL 列表中选出最佳音频 URL"""
    if not audio_urls:
        return None
    clean = [u for u in audio_urls if not _is_blacklisted_audio_url(u)]
    trusted = [u for u in clean if _is_trusted_audio_url(u)]

    for pool in [trusted, clean]:
        mp3 = [u for u in pool if ".mp3" in u]
        if mp3:
            return mp3[0]
        if pool:
            return pool[0]
    return None


# ══════════════════════════════════════════════════════════════
# Source 实现
# ══════════════════════════════════════════════════════════════

class Ting13Source(Source):
    """ting13.cc 有声小说"""

    match = [
        r"ting13\.cc",
        r"ting13\.com",
    ]
    names = ["ting13.cc"]
    base_url = "https://ting13.cc"

    def __init__(self):
        super().__init__()
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._headless = True
        self._lock = threading.Lock()  # 线程锁，保护 Playwright Page 操作
        self._http_session = None  # HTTP会话，用于API请求
        self._stored_cookies_cache: Optional[List[dict]] = None  # cookies 内存缓存
        self._stop_event = None  # 停止事件（用于支持中断操作）

    # ── 配置 ──

    def _setup_pw_ting13_route(self, page: "Page"):
        """
        为 Playwright Page 设置路由拦截 — 所有 ting13.cc 请求由 raw socket 直接获取内容,
        完全绕过 Playwright 的 DNS 解析和网络连接, 避免 DNS 污染和 SNI 限制。
        """
        import time as _t
        from urllib.parse import urlparse as _uparse
        _req_cache = {}

        def _handle_route(route):
            req_url = route.request.url
            try:
                hostname = _uparse(req_url).hostname or ""
            except Exception:
                hostname = ""

            if "ting13.cc" not in hostname:
                route.continue_()
                return

            # API/JSON/Font/Media 请求走浏览器原生网络（需要cookie/headers/tls协商）
            if any(skip in req_url for skip in [
                "/api/", "/ajax/", ".json", ".js", ".css",
                ".png", ".jpg", ".gif", ".svg", ".ico", ".woff",
                ".mp3", ".m4a", ".aac",
            ]):
                route.continue_()
                return

            cache_key = req_url
            cached = _req_cache.get(cache_key)
            if cached and _t.time() - cached["time"] < 60:
                route.fulfill(status=cached["status"], headers=cached["headers"], body=cached["body"])
                return

            for attempt in range(2):
                try:
                    body = _fetch_via_ting13_sni(req_url, timeout=15)
                except Exception:
                    body = None

                if body is not None:
                    text = body.decode("utf-8", errors="replace")
                    ct = "application/json; charset=utf-8" if text.strip().startswith("{") else "text/html; charset=utf-8"
                    pw_headers = {"Content-Type": ct, "Access-Control-Allow-Origin": "*"}

                    _req_cache[cache_key] = {"status": 200, "headers": pw_headers, "body": text, "time": _t.time()}
                    if len(_req_cache) > 200:
                        for k in list(_req_cache.keys())[:50]:
                            del _req_cache[k]

                    route.fulfill(status=200, headers=pw_headers, body=text)
                    return

                if attempt < 1:
                    _t.sleep(3)

            # 失败时不拦截，让浏览器原生处理
            route.continue_()

        page.route("**/*", _handle_route)

    # ── 配置 ──

    def set_headless(self, headless: bool):
        self._headless = headless

    def set_stop_event(self, stop_event):
        """设置停止事件，用于支持中断长时间操作"""
        self._stop_event = stop_event

    def _should_stop(self) -> bool:
        """检查是否应该停止"""
        if self._stop_event and hasattr(self._stop_event, 'is_set'):
            return self._stop_event.is_set()
        return False

    def _interruptible_sleep(self, seconds: float):
        """可被停止信号中断的睡眠"""
        import time as _time
        end = _time.time() + seconds
        while _time.time() < end:
            if self._should_stop():
                return
            _time.sleep(min(0.5, end - _time.time()))

    def _build_playwright_launch_kwargs(self) -> dict:
        """构建 Playwright launch_kwargs"""
        from ting13.core.network import get_host_resolver_rules
        kwargs = {
            "headless": self._headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        chrome_exe = get_chrome_exe_path()
        if chrome_exe:
            kwargs["executable_path"] = chrome_exe
        proxy = get_proxy()
        if proxy:
            kwargs["proxy"] = {"server": proxy}
        else:
            kwargs["args"].append("--no-proxy-server")

        host_rules = get_host_resolver_rules(["www.ting13.cc", "m.ting13.cc"])
        if host_rules:
            kwargs["args"].append(f"--host-resolver-rules={host_rules}")

        return kwargs

    # ── URL 识别 ──

    def detect_url_type(self, url: str) -> str:
        if "/play/" in url:
            return "play"
        elif "/youshengxiaoshuo/" in url or "/book/" in url:
            return "book"
        return "unknown"

    # ── 页面解析 ──

    def _fetch_page_via_playwright(self, url: str) -> Optional[str]:
        """使用 Playwright 获取页面 HTML (回退方式, 所有请求由 raw socket 拦截)"""
        if not _playwright_available:
            return None

        pw = None
        browser = None
        context = None
        page = None
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            launch_kwargs = self._build_playwright_launch_kwargs()
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=DEFAULT_UA,
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            self._setup_pw_ting13_route(page)

            page.goto(url, wait_until="commit", timeout=60000)
            page.wait_for_timeout(1000)

            def _safe_content():
                for retry in range(10):
                    try:
                        return page.content()
                    except Exception:
                        page.wait_for_timeout(1000)
                return ""

            content = ""
            for _ in range(15):
                content = _safe_content()
                if not content:
                    continue
                if "请求过于频繁" in content:
                    return None
                if "Loading..." not in content and len(content) > 1000:
                    break
                page.wait_for_timeout(1000)
            if not content or "Loading..." in content or "请求过于频繁" in content or len(content) < 500:
                return None
            return content
        except Exception:
            return None
        finally:
            try:
                if context:
                    context.close()
                if browser:
                    browser.close()
                if pw:
                    pw.stop()
            except Exception:
                pass

    def parse_book(self, url: str) -> BookInfo:
        desktop_url = url.replace("m.ting13.cc", "www.ting13.cc")

        content = None
        used_url = desktop_url

        # 策略1: curl_cffi (绕过Cloudflare TLS指纹检测)
        if _HAS_CFFI:
            try:
                s = cffi_requests.Session(impersonate="chrome120")
                resp = s.get(desktop_url, timeout=15, verify=False)
                s.close()
                if resp.status_code == 200 and len(resp.text) > 500:
                    content = resp.text.encode("utf-8")
                    used_url = desktop_url
                    self._log_func("  [*] curl_cffi 获取页面成功")
            except Exception:
                pass

        # 策略2: 标准 HTTP（通过 SNI raw socket 或 DoH）
        if content is None:
            mobile_url = desktop_url.replace("www.ting13.cc", "m.ting13.cc")
            for candidate in [desktop_url, mobile_url]:
                try:
                    content = fetch_page(candidate, referer=self.base_url + "/")
                    used_url = candidate
                    break
                except Exception:
                    pass

        # 策略3: Playwright 浏览器
        if content is None and _playwright_available:
            for candidate in [desktop_url, desktop_url.replace("www.ting13.cc", "m.ting13.cc")]:
                self._log_func(f"  [*] HTTP请求失败, 使用浏览器获取页面...")
                pw_content = self._fetch_page_via_playwright(candidate)
                if pw_content:
                    content = pw_content.encode("utf-8")
                    used_url = candidate
                    break
                self._interruptible_sleep(1)

        if content is None:
            raise RuntimeError("无法获取书籍页面")

        tree = lxml.html.fromstring(content)

        title = "未知书名"
        title_elems = tree.cssselect("h1")
        if title_elems:
            title = title_elems[0].text_content().strip()
            title = re.sub(r'\s*(有声小说|在线收听|全集).*$', '', title)
        if title == "未知书名":
            title_elems = tree.cssselect("title")
            if title_elems:
                title = title_elems[0].text_content().strip()
                title = re.sub(r'\s*[-–|].*$', '', title)
                title = re.sub(r'\s*(有声小说|在线收听|全集).*$', '', title)

        author = "未知作者"
        author_meta = tree.cssselect("meta[property='og:music:artist']")
        if author_meta and author_meta[0].get("content"):
            author = author_meta[0].get("content", "未知作者")
        else:
            author_elems = tree.cssselect(".author")
            if author_elems:
                author = author_elems[0].text_content().strip()
            else:
                for a in tree.cssselect("a[href*='/author/']"):
                    text = a.text_content().strip()
                    if text:
                        author = text
                        break

        cover_url = ""
        cover_elems = tree.cssselect("meta[property='og:image']")
        if cover_elems:
            cover_url = cover_elems[0].get("content", "")
        if not cover_url:
            cover_elems = tree.cssselect("img.cover") or tree.cssselect(".bookcover img")
            if cover_elems:
                cover_url = cover_elems[0].get("src", "")

        chapters = []
        tingdirs_url = self._find_tingdirs_url(tree, used_url)
        if tingdirs_url:
            chapters = self._fetch_all_chapters(tingdirs_url, used_url)
            if not chapters:
                play_links = tree.cssselect("a[href*='/play/']")
                chapters = self._extract_chapters_from_links(play_links, used_url)
                self._log_func(f"  [!] tingdirs 页面解析失败，从主页面获取到 {len(chapters)} 个章节")
        if not chapters:
            play_links = tree.cssselect("a[href*='/play/']")
            chapters = self._extract_chapters_from_links(play_links, used_url)

        return BookInfo(
            title=title, author=author, cover_url=cover_url,
            chapters=chapters, source_name=self.name,
        )

    def parse_book_incremental(self, url: str, batch_callback=None):
        """
        流式解析：先快速返回前几页章节，后台继续解析剩余页面

        Args:
            url: 书籍URL
            batch_callback: 可选回调函数 callback(chapters_batch, total_parsed, is_final)

        Yields:
            tuple: (chapters_list, metadata_dict)
                  - 第一批: ([前N章], {"phase": "initial", "total_estimated": 估算总数})
                  - 后续批次: ([新章节], {"phase": "increment", "page": 当前页码})
                  - 最后: ([], {"phase": "done", "total": 实际总数})
        """
        import queue as _queue

        desktop_url = url.replace("m.ting13.cc", "www.ting13.cc")

        content = None
        used_url = desktop_url

        # 获取主页面（复用原有逻辑）
        if _HAS_CFFI:
            try:
                s = cffi_requests.Session(impersonate="chrome120")
                resp = s.get(desktop_url, timeout=15, verify=False)
                s.close()
                if resp.status_code == 200 and len(resp.text) > 500:
                    content = resp.text.encode("utf-8")
                    used_url = desktop_url
                    self._log_func("  [*] curl_cffi 获取页面成功")
            except Exception:
                pass

        if content is None:
            mobile_url = desktop_url.replace("www.ting13.cc", "m.ting13.cc")
            for candidate in [desktop_url, mobile_url]:
                try:
                    content = fetch_page(candidate, referer=self.base_url + "/")
                    used_url = candidate
                    break
                except Exception:
                    pass

        if content is None and _playwright_available:
            for candidate in [desktop_url, desktop_url.replace("www.ting13.cc", "m.ting13.cc")]:
                pw_content = self._fetch_page_via_playwright(candidate)
                if pw_content:
                    content = pw_content.encode("utf-8")
                    used_url = candidate
                    break
                self._interruptible_sleep(1)

        if content is None:
            raise RuntimeError("无法获取书籍页面")

        tree = lxml.html.fromstring(content)

        title = "未知书名"
        title_elems = tree.cssselect("h1")
        if title_elems:
            title = title_elems[0].text_content().strip()
            title = re.sub(r'\s*(有声小说|在线收听|全集).*$', '', title)
        if title == "未知书名":
            title_elems = tree.cssselect("title")
            if title_elems:
                title = title_elems[0].text_content().strip()
                title = re.sub(r'\s*[-–|].*$', '', title)
                title = re.sub(r'\s*(有声小说|在线收听|全集).*$', '', title)

        author = "未知作者"
        author_meta = tree.cssselect("meta[property='og:music:artist']")
        if author_meta and author_meta[0].get("content"):
            author = author_meta[0].get("content", "未知作者")
        else:
            author_elems = tree.cssselect(".author")
            if author_elems:
                author = author_elems[0].text_content().strip()

        cover_url = ""
        cover_elems = tree.cssselect("meta[property='og:image']")
        if cover_elems:
            cover_url = cover_elems[0].get("content", "")
        if not cover_url:
            cover_elems = tree.cssselect("img.cover") or tree.cssselect(".bookcover img")
            if cover_elems:
                cover_url = cover_elems[0].get("src", "")

        tingdirs_url = self._find_tingdirs_url(tree, used_url)

        if not tingdirs_url:
            play_links = tree.cssselect("a[href*='/play/']")
            chapters = self._extract_chapters_from_links(play_links, used_url)
            yield (chapters, {
                "phase": "done",
                "total": len(chapters),
                "title": title,
                "author": author,
                "cover_url": cover_url,
            })
            return

        # 使用增量式章节解析（支持分批返回）
        first_batch_size = 3  # 先解析前3页
        all_chapters = []
        seen_urls = set()
        incremental_failed = False

        try:
            for batch_data in self._fetch_all_chapters_incremental(
                tingdirs_url, used_url, initial_pages=first_batch_size
            ):
                batch_chapters = batch_data["chapters"]
                phase = batch_data["phase"]

                new_chapters = []
                for ch in batch_chapters:
                    if ch.play_url not in seen_urls:
                        seen_urls.add(ch.play_url)
                        ch.index = len(all_chapters) + 1
                        all_chapters.append(ch)
                        new_chapters.append(ch)

                if phase == "initial" or phase == "increment":
                    meta = {
                        "phase": phase,
                        "total_parsed": len(all_chapters),
                        "title": title,
                        "author": author,
                        "cover_url": cover_url,
                    }
                    if "page" in batch_data:
                        meta["current_page"] = batch_data["page"]
                    if "total_pages" in batch_data:
                        meta["total_pages"] = batch_data["total_pages"]
                        meta["total_estimated"] = batch_data["total_pages"] * 30

                    if batch_callback:
                        batch_callback(new_chapters, len(all_chapters), phase == "final")

                    yield (new_chapters, meta)

                elif phase == "done":
                    # 检查是否成功获取到章节
                    if not all_chapters:
                        incremental_failed = True
                        break  # 增量解析失败，需要回退
                    else:
                        final_meta = {
                            "phase": "done",
                            "total": len(all_chapters),
                            "title": title,
                            "author": author,
                            "cover_url": cover_url,
                        }
                        if batch_callback:
                            batch_callback([], len(all_chapters), True)
                        yield ([], final_meta)
                        return

        except Exception as e:
            self._log_func(f"  [!] 增量解析异常: {e}")
            incremental_failed = True

        # 如果增量解析失败（0个章节或异常），回退到传统全量解析
        if incremental_failed or not all_chapters:
            self._log_func("  [*] 增量解析未获取到数据，回退到传统全量解析模式...")
            try:
                chapters_fallback = self._fetch_all_chapters(tingdirs_url, used_url)

                if chapters_fallback:
                    # 将传统解析结果包装成增量格式返回
                    fallback_batch = []
                    for idx, ch in enumerate(chapters_fallback):
                        if ch.play_url not in seen_urls:
                            seen_urls.add(ch.play_url)
                            ch.index = len(fallback_batch) + 1
                            fallback_batch.append(ch)

                    if fallback_batch:
                        total = len(fallback_batch)
                        self._log_func(f"  [*] 传统解析成功: {total} 章")

                        # 返回为单个批次（兼容增量接口）
                        yield (fallback_batch, {
                            "phase": "initial",
                            "total_parsed": total,
                            "title": title,
                            "author": author,
                            "cover_url": cover_url,
                            "total_estimated": total,
                            "total_pages": 1,
                        })

                        # 发送完成信号
                        yield ([], {
                            "phase": "done",
                            "total": total,
                            "title": title,
                            "author": author,
                            "cover_url": cover_url,
                        })
                        return

            except Exception as e:
                self._log_func(f"  [!] 传统解析也失败: {e}")

        # 如果所有方法都失败了
        if not all_chapters:
            yield ([], {
                "phase": "done",
                "total": 0,
                "title": title,
                "author": author,
                "cover_url": cover_url,
            })

    # ── 音频 URL 提取 ──

    def prefetch_audio_url(self, chapter: Chapter) -> Optional[str]:
        """
        快速预取: ting13.cc 不支持轻量预取，直接返回 None
        
        避免预取线程操作 Playwright Page 导致线程安全问题。
        让主线程统一处理所有 Playwright 操作。
        """
        return None

    def _init_http_session(self):
        """初始化HTTP session，先访问书页获取 session cookie，自动使用代理"""
        if self._http_session is None:
            self._http_session = build_session(user_agent=DEFAULT_UA)
            try:
                self._http_session.get(self.base_url, timeout=10, verify=False)
            except Exception:
                pass

    def _get_audio_url_http(self, chapter: Chapter) -> Optional[str]:
        """使用HTTP请求获取音频URL（含内部重试，curl_cffi失效时重建session）"""
        play_url = chapter.play_url
        if not play_url:
            return None

        m = re.search(r'/play/(\d+)_(\d+)(?:_\d+)?\.html', play_url)
        if not m:
            return None
        book_id, chapter_num = m.group(1), m.group(2)

        headers = {
            "Referer": play_url,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

        api_urls = [
            f"https://www.ting13.cc/api/mapi/play?book_id={book_id}&chapter_num={chapter_num}",
            f"https://m.ting13.cc/api/key/readplay?book_id={book_id}&chapter_num={chapter_num}",
        ]

        for attempt in range(2):
            if attempt > 0:
                self._interruptible_sleep(min(8 * attempt, 20))
                self._log_func(f"  [>] API重试 (第{attempt+1}次)...")

            for api_url in api_urls:
                data = self._try_api_with_session(api_url, headers)
                if data is not None:
                    break
                if attempt < 2:
                    self._interruptible_sleep(2)

            if data is None:
                continue

            if isinstance(data, dict) and data.get("status") == 468:
                clear_cookies()
                self._stored_cookies_cache = None
                return None

            for key in ["audioUrl", "mp3", "m4a", "url", "audio_url", "src", "play_url"]:
                val = data.get(key) if isinstance(data, dict) else None
                if val and isinstance(val, str) and (val.startswith("http") or val.startswith("//")):
                    audio_url = "https:" + val if val.startswith("//") else val
                    if not _is_blacklisted_audio_url(audio_url):
                        self._log_func(f"  [OK] HTTP API获取成功: {audio_url[:60]}...")
                        return audio_url

        return None

    def _try_api_with_session(self, api_url: str, headers: dict) -> Optional[dict]:
        """用 curl_cffi session 获取 API 数据，遇到cookie挑战自动解决"""
        global _cffi_session
        if not _HAS_CFFI:
            return fetch_json(api_url, headers=headers, timeout=10)

        try:
            resp = _cffi_session.get(api_url, headers=headers, timeout=15, verify=False)
            if resp.status_code in (200, 405) and resp.text:
                text = resp.text
                if "Loading..." in text and "var reversed" in text:
                    self._log_func("  [!] API返回cookie挑战，尝试解决...")
                    return self._solve_cffi_challenge(api_url, text)
                else:
                    try:
                        return json.loads(text)
                    except Exception:
                        return None
        except Exception:
            pass

        return None

    def _solve_cffi_challenge(self, api_url: str, challenge_text: str) -> Optional[dict]:
        """解决 curl_cffi 遇到的 cookie 挑战 (Loading... 页面)"""
        import base64 as _b64
        m = re.search(r'var\s+reversed\s*=\s*"([^"]+)"', challenge_text)
        if not m:
            m = re.search(r"var\s+reversed\s*=\s*'([^']+)'", challenge_text)
        if not m:
            return None
        try:
            reversed_str = m.group(1)
            b64_str = reversed_str[::-1]
            decoded = _b64.b64decode(b64_str).decode("utf-8", errors="replace")
            token_m = re.search(r"var\s+token\s*=\s*'([^']+)'", decoded)
            if token_m:
                token = token_m.group(1)
                _cffi_session.cookies.set("__51guid__", token, domain=".ting13.cc", path="/")
                _cffi_session.cookies.set("__51refresh__guid", "1", domain=".ting13.cc", path="/")
                self._interruptible_sleep(0.5)
                new_headers = {
                    "Referer": "https://www.ting13.cc/play/30969_1.html",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                }
                for retry in range(2):
                    resp = _cffi_session.get(api_url, headers=new_headers, timeout=15, verify=False)
                    if resp.status_code in (200, 405) and resp.text:
                        try:
                            return json.loads(resp.text)
                        except Exception:
                            if retry < 1:
                                self._interruptible_sleep(3)
        except Exception:
            pass
        return None

    def get_audio_url(self, chapter: Chapter) -> Optional[str]:
        """获取音频URL - 纯HTTP方式 (curl_cffi + cookie挑战解决)"""
        return self._get_audio_url_http(chapter)

    def _get_audio_url_playwright(self, chapter: Chapter) -> Optional[str]:
        """使用Playwright获取音频URL（备用方式, 所有请求由 raw socket 拦截）"""
        if self._stored_cookies_cache is None:
            self._stored_cookies_cache = load_cookies()
        stored_cookies = self._stored_cookies_cache

        target_url = chapter.play_url.replace("m.ting13.cc", "www.ting13.cc")

        audio_urls_found = []

        def handle_response(response):
            url = response.url
            if any(skip in url for skip in [
                'google', 'baidu', 'analytics', 'adsbygoogle',
                '.gif', '.png', '.jpg', '.css', '.ico', 'favicon'
            ]):
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "text" in ct:
                    body = None
                    try:
                        body = response.text()
                        if "status:468" in body or "login" in body:
                            self._log_func("  [!] API 返回需要登录")
                            clear_cookies()
                            self._stored_cookies_cache = None
                    except Exception:
                        pass
                    if body and ("mp3" in body or "m4a" in body or "audioUrl" in body):
                        try:
                            data = json.loads(body)
                            if "status" in data and data["status"] == 468:
                                self._log_func("  [!] API 返回 468，需要登录")
                                clear_cookies()
                                self._stored_cookies_cache = None
                            for key in ["audioUrl", "mp3", "m4a", "url", "audio_url", "src", "play_url"]:
                                if key in data and data[key]:
                                    val = data[key]
                                    if isinstance(val, str) and (val.startswith("http") or val.startswith("//")):
                                        audio_urls_found.append(val)
                        except json.JSONDecodeError:
                            pass
                if any(ext in url for ext in [".mp3", ".m4a", ".aac", ".wav", ".ogg"]):
                    if url.startswith("http"):
                        audio_urls_found.append(url)
            except Exception:
                pass

        self._page.on("response", handle_response)
        try:
            self._page.goto(target_url, wait_until="commit", timeout=60000)

            self._page.wait_for_timeout(3000)

            try:
                dom_url = self._page.evaluate("""() => {
                    const audio = document.querySelector('audio');
                    if (audio && audio.src) return audio.src;
                    if (audio && audio.currentSrc) return audio.currentSrc;
                    const source = document.querySelector('audio source');
                    if (source && source.src) return source.src;
                    return null;
                }""")
                if dom_url:
                    audio_urls_found.append(dom_url)
            except Exception:
                pass

            best = _pick_best_audio_url(audio_urls_found)
            if not best or not _is_trusted_audio_url(best):
                for attempt in range(15):
                    self._page.wait_for_timeout(1000)
                    best = _pick_best_audio_url(audio_urls_found)
                    if best and _is_trusted_audio_url(best):
                        break
                    if attempt % 3 == 0:
                        try:
                            audio_src = self._page.evaluate("""() => {
                                const audio = document.querySelector('audio');
                                return audio?.currentSrc || audio?.src || null;
                            }""")
                            if audio_src:
                                audio_urls_found.append(audio_src)
                                best = _pick_best_audio_url(audio_urls_found)
                                if best and _is_trusted_audio_url(best):
                                    break
                        except Exception:
                            pass
                    if attempt >= 10:
                        best = _pick_best_audio_url(audio_urls_found)
                        if best:
                            break

        except Exception as e:
            self._log_func(f"  [!] 页面加载出错: {e}")
        finally:
            self._page.remove_listener("response", handle_response)

        return _pick_best_audio_url(audio_urls_found)

    # ── 生命周期 ──

    def before_download(self, chapters, callbacks):
        super().before_download(chapters, callbacks)

        # 初始化 curl_cffi session（用于绕过 Cloudflare TLS 指纹检测）
        global _cffi_session
        if _HAS_CFFI and _cffi_session is None:
            try:
                _cffi_session = cffi_requests.Session(impersonate="chrome120")
                _cffi_session.headers.update({
                    "User-Agent": DEFAULT_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                })
                self._log_func("  [*] curl_cffi 已启用 (Chrome 120 TLS指纹)")
            except Exception as e:
                self._log_func(f"  [!] curl_cffi 初始化失败: {e}")

        # 预热 cookie：预先解决 cookie 挑战，避免每个章节重复解决
        if _HAS_CFFI and _cffi_session and chapters:
            try:
                self._log_func("  [*] 预热cookie...")
                m_warm = re.search(r'/play/(\d+)_(\d+)', chapters[0].play_url) if chapters[0].play_url else None
                if m_warm:
                    warm_url = f"https://www.ting13.cc/api/mapi/play?book_id={m_warm.group(1)}&chapter_num={m_warm.group(2)}"
                else:
                    warm_url = "https://www.ting13.cc/api/mapi/play?book_id=30969&chapter_num=1"
                warm_headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json,*/*"}
                resp = _cffi_session.get(warm_url, headers=warm_headers, timeout=15, verify=False)
                if "Loading..." in resp.text and "var reversed" in resp.text:
                    result = self._solve_cffi_challenge(warm_url, resp.text)
                    if result:
                        self._log_func("  [OK] Cookie预热完成")
                    else:
                        self._log_func("  [!] Cookie预热失败")
                else:
                    self._log_func("  [OK] 无需预热（已通过）")
            except Exception as e:
                self._log_func(f"  [!] Cookie预热异常: {e}")

        # 音频URL现在通过纯HTTP获取(curl_cffi)，无需Playwright浏览器
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        callbacks.on_log("  [*] 使用纯 HTTP 模式获取音频 URL")

    def after_download(self):
        global _cffi_session
        try:
            if _cffi_session:
                _cffi_session.close()
                _cffi_session = None
        except Exception:
            pass
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

    # ── 认证 ──

    def supports_login(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return has_cookies()

    def check_login_required(self, chapters: List[Chapter], callbacks: "DownloadCallbacks") -> bool:
        """
        检测书籍是否需要登录才能下载 (HTTP方式)
        
        在下载开始前调用，尝试获取第一个章节的音频URL。
        如果返回需要登录，提示用户进行登录。
        
        Returns:
            True: 需要登录但未登录
            False: 不需要登录或已登录
        """
        if not chapters:
            return False
            
        if has_cookies():
            callbacks.on_log("[*] 已检测到登录状态，跳过登录检测")
            return False
        
        callbacks.on_log("[*] 检测是否需要登录...")
        
        # 使用HTTP方式检测（快速且支持代理池）
        first_chapter = chapters[0]
        play_url = first_chapter.play_url
        
        # 从 play_url 提取 book_id 和 chapter_num
        m = re.search(r'/play/(\d+)_(\d+)(?:_\d+)?\.html', play_url)
        if not m:
            callbacks.on_log("[*] 检测完成：无法解析URL，将尝试下载")
            return False
        book_id, chapter_num = m.group(1), m.group(2)
        
        # 尝试移动版API
        api_url = f"https://m.ting13.cc/api/key/readplay?book_id={book_id}&chapter_num={chapter_num}"
        headers = {
            "User-Agent": MOBILE_UA,
            "Referer": play_url,
            "Accept": "application/json",
        }
        
        try:
            data = fetch_json(api_url, headers=headers, timeout=15)
            if data:
                # 检查是否需要登录
                if "status" in data and data["status"] == 468:
                    callbacks.on_log("[!] 检测完成：此书需要登录才能下载")
                    return True
                # 检查是否有音频URL
                for key in ["audioUrl", "mp3", "m4a", "url", "audio_url"]:
                    if key in data and data[key]:
                        callbacks.on_log("[*] 检测完成：无需登录即可下载")
                        return False
        except Exception as e:
            callbacks.on_log(f"[!] 检测出错: {e}")
        
        callbacks.on_log("[*] 检测完成：无法确定，将尝试下载")
        return False

    def prompt_login(self, callbacks: "DownloadCallbacks") -> bool:
        """
        提示用户进行登录操作
        
        Returns:
            True: 用户已登录
            False: 用户取消登录
        """
        callbacks.on_log("\n" + "=" * 60)
        callbacks.on_log("  ⚠️  此书需要登录才能下载")
        callbacks.on_log("=" * 60)
        callbacks.on_log("\n请按以下步骤登录：")
        callbacks.on_log("  1. 在浏览器中打开 https://www.ting13.cc")
        callbacks.on_log("  2. 登录你的账号")
        callbacks.on_log("  3. 按 F12 打开开发者工具")
        callbacks.on_log("  4. 切换到 Application (应用) 标签")
        callbacks.on_log("  5. 在左侧找到 Cookies > https://www.ting13.cc")
        callbacks.on_log("  6. 复制所有 cookies（JSON 格式）")
        callbacks.on_log("  7. 保存到文件: ~/.ting13_cookies.json")
        callbacks.on_log("\n或者使用浏览器导出 cookies 插件（如 EditThisCookie）")
        callbacks.on_log("=" * 60)
        
        return False

    # ── 内部方法: 章节列表解析 ──

    def _find_tingdirs_url(self, tree, base_url: str) -> Optional[str]:
        for link in tree.cssselect("a[href*='/tingdirs/']"):
            text = link.text_content().strip()
            if "章节" in text or "目录" in text:
                url = urljoin(base_url, link.get("href", ""))
                return url.replace("m.ting13.cc", "www.ting13.cc")
        for link in tree.cssselect("a"):
            text = link.text_content().strip()
            href = link.get("href", "")
            if ("全部章节" in text or "更多章节" in text) and href:
                bookdir_url = urljoin(base_url, href)
                bookdir_url = bookdir_url.replace("m.ting13.cc", "www.ting13.cc")
                try:
                    content = None
                    if _HAS_CFFI:
                        s = cffi_requests.Session(impersonate="chrome120")
                        resp = s.get(bookdir_url, timeout=10, verify=False)
                        s.close()
                        if resp.status_code == 200:
                            content = resp.text.encode("utf-8")
                    if content is None:
                        content = fetch_page(bookdir_url, referer=self.base_url + "/")
                    bookdir_tree = lxml.html.fromstring(content)
                    for sublink in bookdir_tree.cssselect("a[href*='/tingdirs/']"):
                        subtext = sublink.text_content().strip()
                        if "目录" in subtext or "章节" in subtext:
                            url = urljoin(base_url, sublink.get("href", ""))
                            return url.replace("m.ting13.cc", "www.ting13.cc")
                    for sublink in bookdir_tree.cssselect("a"):
                        subhref = sublink.get("href", "")
                        if "page=" in subhref and "sort=" in subhref:
                            return bookdir_url
                except Exception:
                    pass
        return None

    def _retry_tindirs_with_backoff(
        self, session, url: str, base_url: str, max_retries: int = 3
    ) -> str:
        """带退避重试 tingdirs 页面 (处理 429 限速)"""
        for attempt in range(max_retries):
            try:
                resp = session.get(url, headers={"Referer": base_url + "/"}, timeout=15, verify=False)
                content = resp.text
                if "请求过于频繁" in content or resp.status_code == 429:
                    wait = (attempt + 1) * 3
                    self._log_func(f"  [*] 429限速, {wait}秒后重试...")
                    self._interruptible_sleep(wait)
                    continue
                return content
            except Exception:
                if attempt < max_retries - 1:
                    self._interruptible_sleep(2)
                continue
        return ""

    def _fetch_all_chapters(self, chapter_list_url: str, base_url: str) -> List[Chapter]:
        # 策略1: curl_cffi (绕过Cloudflare TLS指纹) — 先访问主页建立session，再访问章节列表
        if _HAS_CFFI:
            try:
                s = cffi_requests.Session(impersonate="chrome120")
                # 先访问书页建立 session cookies
                s.get(base_url, timeout=15, verify=False)
                self._interruptible_sleep(0.5)
                # 再访问章节列表
                resp = s.get(chapter_list_url, headers={"Referer": base_url + "/"}, timeout=15, verify=False)
                content = resp.text

                if "Loading..." in content:
                    self._log_func("  [*] tingdirs 需要 Cookie 验证, 尝试解算...")
                    cookies = self._extract_cookie_from_loading_page(content)
                    if cookies:
                        # 在已有 session 上添加 cookie（保留 session cookies）
                        for name, value in cookies.items():
                            s.cookies.set(name, value, domain=".ting13.cc", path="/")
                        # 等待足够长时间避免429限速
                        self._interruptible_sleep(3)
                        content = self._retry_tindirs_with_backoff(
                            s, chapter_list_url, base_url, max_retries=3
                        )
                        if "Loading..." not in content and len(content) > 500:
                            self._log_func("  [*] Cookie 验证通过")
                            return self._parse_chapter_pages(content, chapter_list_url, base_url, s, cookies)
                        s.close()
                    else:
                        s.close()
                    self._log_func("  [!] Cookie 解算失败")

                if "Loading..." not in content and len(content) > 500:
                    self._log_func("  [*] curl_cffi 获取章节列表成功")
                    s.close()
                    return self._parse_chapter_pages(content, chapter_list_url, base_url, None)
                s.close()
            except Exception:
                pass

        # 策略2: Playwright 浏览器
        if _playwright_available:
            self._log_func("  [*] 使用浏览器获取章节列表...")
            return self._fetch_all_chapters_via_playwright(chapter_list_url, base_url)

        # 策略3: 标准 HTTP
        session = build_session()
        try:
            resp = session.get(chapter_list_url, headers={"Referer": self.base_url + "/"}, timeout=8)
            content = resp.text
        except Exception as e:
            self._log_func(f"  [!] tingdirs 请求失败: {e}")
            content = ""

        if "Loading..." in content:
            self._log_func("  [*] tingdirs 页面需要 Cookie 验证, 尝试解算...")
            content = self._try_solve_loading_cookie(session, chapter_list_url, content)
            if "Loading..." in content or len(content) < 500:
                self._log_func("  [!] 无法获取 tingdirs 页面内容")
                return []

        if len(content) < 500 and not content.strip():
            self._log_func("  [!] 无法获取 tingdirs 页面内容")
            return []

        return self._parse_chapter_pages(content, chapter_list_url, base_url, session)

    def _fetch_all_chapters_incremental(self, chapter_list_url: str, base_url: str,
                                        initial_pages: int = 3):
        """
        增量式章节解析：先快速返回前N页，然后逐页返回剩余页面

        优化点：
        - Session复用（避免每页重建）
        - 分批yield返回（支持流式处理）
        - 进度日志反馈
        - 智能反限流
        - 自动错误回退（失败时尝试更多策略）

        Yields:
            dict: {"chapters": [...], "phase": "initial"|"increment"|"done", ...}
        """
        last_error = None

        # 策略1: curl_cffi (绕过Cloudflare TLS指纹) — 复用Session
        if _HAS_CFFI:
            try:
                s = cffi_requests.Session(impersonate="chrome120")
                s.get(base_url, timeout=15, verify=False)
                self._interruptible_sleep(0.5)
                resp = s.get(chapter_list_url, headers={"Referer": base_url + "/"}, timeout=20, verify=False)  # 增加超时到20秒
                content = resp.text

                if "Loading..." in content:
                    self._log_func("  [*] tingdirs 需要 Cookie 验证, 尝试解算...")
                    cookies = self._extract_cookie_from_loading_page(content)
                    if cookies:
                        for name, value in cookies.items():
                            s.cookies.set(name, value, domain=".ting13.cc", path="/")
                        self._interruptible_sleep(3)
                        content = self._retry_tindirs_with_backoff(
                            s, chapter_list_url, base_url, max_retries=3
                        )
                        if "Loading..." not in content and len(content) > 500:
                            self._log_func("  [*] Cookie 验证通过")
                            yield from self._parse_chapter_pages_incremental(
                                content, chapter_list_url, base_url,
                                session=s, cookies=cookies,
                                initial_pages=initial_pages
                            )
                            s.close()
                            return
                    else:
                        s.close()
                    self._log_func("  [!] Cookie 解算失败")

                if "Loading..." not in content and len(content) > 500:
                    self._log_func("  [*] curl_cffi 获取章节列表成功")
                    yield from self._parse_chapter_pages_incremental(
                        content, chapter_list_url, base_url,
                        session=s, initial_pages=initial_pages
                    )
                    s.close()
                    return
                s.close()
            except Exception as e:
                last_error = f"curl_cffi: {e}"
                self._log_func(f"  [!] curl_cffi 策略失败: {e}")

        # 策略2: Playwright 浏览器（仅在curl_cffi完全不可用时使用）
        if _playwright_available:
            try:
                self._log_func("  [*] 使用浏览器获取章节列表...")
                chapters = self._fetch_all_chapters_via_playwright(chapter_list_url, base_url)
                if chapters:
                    yield {"chapters": chapters, "phase": "done", "total_pages": 1}
                    return
            except Exception as e:
                last_error = f"Playwright: {e}"
                self._log_func(f"  [!] Playwright 策略失败: {e}")

        # 策略3: 标准 HTTP（增加超时时间和重试）
        session = build_session()
        try:
            resp = session.get(chapter_list_url, headers={"Referer": self.base_url + "/"}, timeout=15)  # 增加到15秒
            content = resp.text
        except Exception as e:
            last_error = f"HTTP: {e}"
            self._log_func(f"  [!] tingdirs 首次请求失败: {e}")
            # 尝试重试一次
            try:
                self._interruptible_sleep(2)
                resp = session.get(chapter_list_url, headers={"Referer": self.base_url + "/"}, timeout=20)
                content = resp.text
                self._log_func("  [*] HTTP 重试成功")
            except Exception as retry_err:
                last_error = f"HTTP重试: {retry_err}"
                content = ""

        if "Loading..." in content:
            self._log_func("  [*] tingdirs 页面需要 Cookie 验证, 尝试解算...")
            content = self._try_solve_loading_cookie(session, chapter_list_url, content)

        if len(content) < 500 and not content.strip():
            self._log_func(f"  [!] 所有策略均失败: {last_error}")
            yield {"chapters": [], "phase": "done", "total_pages": 0}
            return

        yield from self._parse_chapter_pages_incremental(
            content, chapter_list_url, base_url,
            session=session, initial_pages=initial_pages
        )

    def _parse_chapter_pages_incremental(self, first_page_content: str, chapter_list_url: str,
                                         base_url: str, session=None, cookies: Optional[dict] = None,
                                         initial_pages: int = 3):
        """
        增量式分页解析：先返回前N页，然后逐页返回剩余内容

        优化：
        - Session复用（不再每页创建新Session）
        - 分批yield（支持流式处理）
        - 进度反馈（每10页打印一次）
        - 反限流延迟（避免429）
        """
        all_chapters_batch = []
        seen_urls = set()

        tree = lxml.html.fromstring(first_page_content)

        page_urls = []
        for link in tree.cssselect("a"):
            href = link.get("href", "")
            if "page=" in href and "sort=" in href:
                full_href = urljoin(base_url, href)
                if full_href not in page_urls:
                    page_urls.append(full_href)

        if not page_urls:
            page_urls = [chapter_list_url + "?page=1&sort=asc"]

        page_urls = [u.replace("sort=desc", "sort=asc") for u in page_urls]
        page_urls.sort(
            key=lambda u: int(re.search(r'page=(\d+)', u).group(1))
            if re.search(r'page=(\d+)', u) else 0
        )

        use_cffi = session is None and _HAS_CFFI
        total_pages = len(page_urls)

        if total_pages > 1:
            self._log_func(f"  [*] 共 {total_pages} 页章节列表 (增量模式, 首批{initial_pages}页)...")

        # 创建可复用的 cffi session（如果需要）
        cffi_session = None
        if use_cffi:
            try:
                cffi_session = cffi_requests.Session(impersonate="chrome120")
                if cookies:
                    for name, value in cookies.items():
                        cffi_session.cookies.set(name, value, domain=".ting13.cc", path="/")
            except Exception:
                pass

        # 智能反限流状态
        consecutive_429 = 0  # 连续429次数
        cooldown_count = 0   # 冷却次数
        max_consecutive_429 = 5  # 触发冷却的连续429阈值

        try:
            for page_idx, page_url in enumerate(page_urls):
                # 检查停止信号
                if self._should_stop():
                    self._log_func("  [!] 用户请求停止解析")
                    return

                retries = 0
                page_content = None
                page_429_retries = 0  # 当前页面的429重试次数

                while retries < 6:  # 增加到最多6次重试:
                    # 在每次重试前检查停止信号
                    if self._should_stop():
                        self._log_func("  [!] 用户请求停止解析")
                        return

                    try:
                        if page_idx == 0:
                            page_content = first_page_content
                        elif cffi_session:
                            resp = cffi_session.get(
                                page_url,
                                headers={"Referer": self.base_url + "/"},
                                timeout=20, verify=False
                            )
                            page_content = resp.text
                            if "请求过于频繁" in page_content or resp.status_code == 429:
                                page_429_retries += 1
                                retries += 1
                                consecutive_429 += 1

                                # 指数退避：2^n 秒（2, 4, 8, 16, 32, 60）
                                wait_time = min(2 ** page_429_retries, 60)

                                # 如果连续多次429，额外增加惩罚时间
                                if consecutive_429 >= max_consecutive_429:
                                    extra_penalty = min((consecutive_429 - max_consecutive_429 + 1) * 10, 30)
                                    wait_time += extra_penalty
                                    self._log_func(
                                        f"  [*] 第{page_idx+1}页连续{consecutive_429}次429限速, "
                                        f"进入冷却模式 (+{extra_penalty}s)..."
                                    )

                                self._log_func(
                                    f"  [*] 第{page_idx+1}页429限速 "
                                    f"(第{page_429_retries}次重试/本页, "
                                    f"连续{consecutive_429}次), "
                                    f"{wait_time}秒后重试..."
                                )
                                self._interruptible_sleep(wait_time)
                                continue

                            # 成功获取，重置计数器
                            consecutive_429 = 0
                            if "Loading..." in page_content or len(page_content) < 500:
                                raise RuntimeError("页面需要重新验证")
                        elif session:
                            page_resp = session.get(
                                page_url,
                                headers={"Referer": self.base_url + "/"},
                                timeout=30
                            )
                            page_content = page_resp.text
                            if "请求过于频繁" in page_content or page_resp.status_code == 429:
                                page_429_retries += 1
                                retries += 1
                                consecutive_429 += 1

                                # 指数退避
                                wait_time = min(2 ** page_429_retries, 60)

                                # 冷却机制
                                if consecutive_429 >= max_consecutive_429:
                                    extra_penalty = min((consecutive_429 - max_consecutive_429 + 1) * 10, 30)
                                    wait_time += extra_penalty
                                    self._log_func(
                                        f"  [*] 第{page_idx+1}页连续{consecutive_429}次429限速, "
                                        f"进入冷却模式 (+{extra_penalty}s)..."
                                    )

                                self._log_func(
                                    f"  [*] 第{page_idx+1}页429限速 "
                                    f"(第{page_429_retries}次重试/本页, "
                                    f"连续{consecutive_429}次), "
                                    f"{wait_time}秒后重试..."
                                )
                                self._interruptible_sleep(wait_time)
                                continue

                            # 成功，重置计数器
                            consecutive_429 = 0
                            if "Loading..." in page_content or len(page_content) < 500:
                                raise RuntimeError("页面需要重新验证")
                        else:
                            break

                        break
                    except Exception as e:
                        retries += 1
                        if retries < 3:
                            self._interruptible_sleep(2)
                        else:
                            self._log_func(f"  [!] 第{page_idx+1}页获取失败: {e}")
                            break

                # 如果当前页面完全失败（所有重试都429），记录但继续下一页
                if page_content is None and page_429_retries > 0:
                    self._log_func(f"  [!] 第{page_idx+1}页跳过 (连续{consecutive_429}次429)")
                    continue

                if page_content is None:
                    continue

                page_tree = lxml.html.fromstring(page_content)
                play_links = page_tree.cssselect("a[href*='/play/']")

                page_chapters = []
                for link in play_links:
                    href = link.get("href", "")
                    if not href or href in seen_urls:
                        continue
                    seen_urls.add(href)
                    title = link.text_content().strip()
                    if title in ["立即收听", ""]:
                        continue
                    full_url = urljoin(base_url, href).replace("m.ting13.cc", "www.ting13.cc")
                    page_chapters.append(Chapter(
                        index=0,
                        title=title, play_url=full_url,
                    ))

                all_chapters_batch.extend(page_chapters)

                # 判断是否应该立即返回这批数据
                should_yield = False
                phase = ""

                if page_idx < initial_pages:
                    # 初始批次：收集够 initial_pages 后一起返回
                    if page_idx == initial_pages - 1 or page_idx == len(page_urls) - 1:
                        should_yield = True
                        phase = "initial"
                elif page_idx >= initial_pages:
                    # 后续批次：每页都返回（或每隔几页）
                    should_yield = True
                    phase = "increment"

                if should_yield:
                    # 检查停止信号
                    if self._should_stop():
                        self._log_func("  [!] 用户请求停止解析，返回已解析数据")
                        # 返回已获取的数据，标记为done
                        yield {
                            "chapters": all_chapters_batch[:],
                            "phase": "done",
                            "total_pages": total_pages,
                        }
                        return

                    yield {
                        "chapters": all_chapters_batch[:],
                        "phase": phase,
                        "page": page_idx + 1,
                        "total_pages": total_pages,
                        "batch_size": len(all_chapters_batch),
                    }

                    # 清空已发送的批次，准备下一批
                    all_chapters_batch.clear()

                    # 打印进度（每10页一次）
                    if (page_idx + 1) % 10 == 0 or phase == "initial":
                        progress_pct = ((page_idx + 1) / total_pages) * 100
                        self._log_func(f"  [*] 解析进度: {page_idx+1}/{total_pages} 页 ({progress_pct:.0f}%)")

                # 页间延迟（避免触发限流）- 使用可中断睡眠支持停止功能
                if page_idx < len(page_urls) - 1 and page_idx >= initial_pages:
                    self._interruptible_sleep(0.8)

        finally:
            # 清理 cffi session
            if cffi_session:
                try:
                    cffi_session.close()
                except Exception:
                    pass

        # 返回最终完成信号
        yield {
            "chapters": [],
            "phase": "done",
            "total_pages": total_pages,
        }

    def _parse_chapter_pages(self, first_page_content: str, chapter_list_url: str,
                             base_url: str, session, cookies: Optional[dict] = None) -> List[Chapter]:
        all_chapters = []
        seen_urls = set()

        tree = lxml.html.fromstring(first_page_content)

        page_urls = []
        for link in tree.cssselect("a"):
            href = link.get("href", "")
            if "page=" in href and "sort=" in href:
                full_href = urljoin(base_url, href)
                if full_href not in page_urls:
                    page_urls.append(full_href)

        if not page_urls:
            page_urls = [chapter_list_url + "?page=1&sort=asc"]

        page_urls = [u.replace("sort=desc", "sort=asc") for u in page_urls]
        page_urls.sort(
            key=lambda u: int(re.search(r'page=(\d+)', u).group(1))
            if re.search(r'page=(\d+)', u) else 0
        )

        use_cffi = session is None and _HAS_CFFI
        total_pages = len(page_urls)
        if total_pages > 1:
            self._log_func(f"  [*] 共 {total_pages} 页章节列表, 开始解析...")

        for page_idx, page_url in enumerate(page_urls):
            retries = 0
            while retries < 3:
                try:
                    if page_idx == 0:
                        page_content = first_page_content
                    elif use_cffi:
                        s = cffi_requests.Session(impersonate="chrome120")
                        if cookies:
                            for name, value in cookies.items():
                                s.cookies.set(name, value, domain=".ting13.cc", path="/")
                        resp = s.get(page_url, headers={"Referer": self.base_url + "/"}, timeout=15, verify=False)
                        s.close()
                        page_content = resp.text
                        if "请求过于频繁" in page_content or resp.status_code == 429:
                            retries += 1
                            self._interruptible_sleep(retries * 2)
                            continue
                        if "Loading..." in page_content or len(page_content) < 500:
                            raise RuntimeError("页面需要重新验证")
                    elif session:
                        page_resp = session.get(page_url, headers={"Referer": self.base_url + "/"}, timeout=30)
                        page_content = page_resp.text
                        if "请求过于频繁" in page_content or page_resp.status_code == 429:
                            retries += 1
                            self._interruptible_sleep(retries * 2)
                            continue
                        if "Loading..." in page_content or len(page_content) < 500:
                            raise RuntimeError("页面需要重新验证")
                    else:
                        break

                    page_tree = lxml.html.fromstring(page_content)
                    play_links = page_tree.cssselect("a[href*='/play/']")

                    page_count = 0
                    for link in play_links:
                        href = link.get("href", "")
                        if not href or href in seen_urls:
                            continue
                        seen_urls.add(href)
                        title = link.text_content().strip()
                        if title in ["立即收听", ""]:
                            continue
                        full_url = urljoin(base_url, href).replace("m.ting13.cc", "www.ting13.cc")
                        all_chapters.append(Chapter(
                            index=len(all_chapters) + 1,
                            title=title, play_url=full_url,
                        ))
                        page_count += 1

                    if page_idx > 0 and page_count == 0 and retries < 2:
                        retries += 1
                        self._interruptible_sleep(1)
                        continue
                    break
                except Exception:
                    retries += 1
                    if retries < 2:
                        self._interruptible_sleep(1)
                    else:
                        break

            if page_idx > 0 and page_idx % 10 == 0:
                self._interruptible_sleep(0.5)

        # 清理 cffi session
        if session is not None and _HAS_CFFI:
            try:
                session.close()
            except Exception:
                pass

        return all_chapters

    def _try_solve_loading_cookie(self, session, url: str, content: str) -> str:
        import base64 as _b64
        from urllib.parse import quote as _url_quote
        m = re.search(r'var\s+reversed\s*=\s*"([^"]+)"', content)
        if not m:
            m = re.search(r"var\s+reversed\s*=\s*'([^']+)'", content)
        if not m:
            return content
        try:
            reversed_str = m.group(1)
            b64_str = reversed_str[::-1]
            decoded = _b64.b64decode(b64_str).decode("utf-8", errors="replace")
            token_m = re.search(r"var\s+token\s*=\s*'([^']+)'", decoded)
            if token_m:
                token = token_m.group(1)
                session.cookies.clear()
                session.cookies.set("__51guid__", _url_quote(token, safe=""), domain=".ting13.cc", path="/")
                session.cookies.set("__51refresh__guid", "1", domain=".ting13.cc", path="/")
                self._interruptible_sleep(0.5)
                resp = session.get(url, headers={"Referer": self.base_url + "/"}, timeout=10)
                if resp.status_code == 429:
                    self._log_func("  [!] 请求过于频繁 (429), 等待后重试...")
                    self._interruptible_sleep(3)
                    resp = session.get(url, headers={"Referer": self.base_url + "/"}, timeout=10)
                return resp.text
        except Exception as e:
            self._log_func(f"  [!] Cookie 解算失败: {e}")
        return content

    def _extract_cookie_from_loading_page(self, content: str) -> Optional[dict]:
        """从 Loading... 页面提取 Cookie 验证所需的 token"""
        import base64 as _b64
        m = re.search(r'var\s+reversed\s*=\s*"([^"]+)"', content)
        if not m:
            m = re.search(r"var\s+reversed\s*=\s*'([^']+)'", content)
        if not m:
            return None
        try:
            reversed_str = m.group(1)
            b64_str = reversed_str[::-1]
            decoded = _b64.b64decode(b64_str).decode("utf-8", errors="replace")
            token_m = re.search(r"var\s+token\s*=\s*'([^']+)'", decoded)
            if token_m:
                return {"__51guid__": token_m.group(1), "__51refresh__guid": "1"}
        except Exception:
            pass
        return None

    def _fetch_all_chapters_via_playwright(self, chapter_list_url: str, base_url: str) -> List[Chapter]:
        all_chapters = []
        seen_urls = set()
        pw = None
        browser = None
        context = None
        page = None

        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            launch_kwargs = self._build_playwright_launch_kwargs()
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=DEFAULT_UA,
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            self._setup_pw_ting13_route(page)

            self._log_func("  [*] 使用浏览器获取章节列表...")

            # Navigate to base page first to get cookies, then go to chapter list
            page.goto(base_url, wait_until="commit", timeout=60000)
            page.wait_for_timeout(3000)

            # 从浏览器页面重新获取 tingdirs URL（每次加载随机路径不同）
            new_tingdirs_url = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a'));
                for (const link of links) {
                    const href = link.getAttribute('href') || '';
                    if (href.includes('/tingdirs/')) {
                        let url = href.startsWith('http') ? href : 'https://www.ting13.cc' + href;
                        return url.replace('m.ting13.cc', 'www.ting13.cc');
                    }
                }
                return null;
            }""")
            if new_tingdirs_url:
                chapter_list_url = new_tingdirs_url

            page.goto(chapter_list_url, wait_until="commit", timeout=60000)
            page.wait_for_timeout(3000)

            def _safe_content(target_page=None):
                p = target_page or page
                for retry in range(10):
                    try:
                        return p.content()
                    except Exception:
                        page.wait_for_timeout(1000)
                return ""

            first_content = ""
            for _ in range(15):
                first_content = _safe_content()
                if not first_content:
                    continue
                if "请求过于频繁" in first_content:
                    self._log_func("  [!] Playwright 获取首章失败 (429)")
                    return []
                if "Loading..." not in first_content and len(first_content) > 1000:
                    break
                page.wait_for_timeout(1000)

            if not first_content or "Loading..." in first_content or "请求过于频繁" in first_content or len(first_content) < 500:
                self._log_func("  [!] Playwright 未通过 Cookie 验证")
                return []

            tree = lxml.html.fromstring(first_content)

            page_urls = []
            for link in tree.cssselect("a"):
                href = link.get("href", "")
                if "page=" in href and "sort=" in href:
                    full_href = urljoin(base_url, href)
                    if full_href not in page_urls:
                        page_urls.append(full_href)

            if not page_urls:
                page_urls = [chapter_list_url + "?page=1&sort=asc"]

            page_urls = [u.replace("sort=desc", "sort=asc") for u in page_urls]
            page_urls.sort(
                key=lambda u: int(re.search(r'page=(\d+)', u).group(1))
                if re.search(r'page=(\d+)', u) else 0
            )

            total_pages = len(page_urls)
            self._log_func(f"  [*] 共 {total_pages} 页, 使用 JS fetch() 批量获取...")

            js_code = """
async (params) => {
    const {pageUrls, baseUrl} = params;
    
    function extractChapters(html) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        const links = doc.querySelectorAll('a[href*="/play/"]');
        const chapters = [];
        for (const link of links) {
            const href = link.getAttribute('href');
            const title = link.textContent.trim();
            if (!href || title === '立即收听' || !title) continue;
            let fullUrl = href.startsWith('http') ? href : baseUrl + href;
            fullUrl = fullUrl.replace('m.ting13.cc', 'www.ting13.cc');
            chapters.push({href: fullUrl, title: title});
        }
        return chapters;
    }

    const allChapters = [];
    const seen = new Set();

    // Page 1 from current DOM
    const firstHtml = document.documentElement.outerHTML;
    for (const ch of extractChapters(firstHtml)) {
        if (!seen.has(ch.href)) {
            seen.add(ch.href);
            allChapters.push(ch);
        }
    }

    // Fetch remaining pages via JS fetch()
    let consecutive429 = 0;
    let cooldownCount = 0;

    for (let i = 1; i < pageUrls.length; i++) {
        // Cooldown check
        if (consecutive429 >= 5) {
            cooldownCount++;
            if (cooldownCount > 2) break; // Give up after 2 cooldown cycles
            consecutive429 = 0;
            await new Promise(r => setTimeout(r, 30000)); // 30s cooldown
        }

        let success = false;
        for (let retry = 0; retry < 2 && !success; retry++) {
            try {
                if (retry > 0) {
                    await new Promise(r => setTimeout(r, 8000));
                } else if (i > 1) {
                    await new Promise(r => setTimeout(r, 1200));
                }

                const resp = await fetch(pageUrls[i], {
                    headers: {'Referer': baseUrl + '/'},
                    credentials: 'include'
                });

                if (resp.status === 429) {
                    consecutive429++;
                    continue;
                }

                const html = await resp.text();
                if (html.includes('请求过于频繁') || html.includes('Loading...')) {
                    consecutive429++;
                    continue;
                }

                consecutive429 = 0;
                success = true;

                for (const ch of extractChapters(html)) {
                    if (!seen.has(ch.href)) {
                        seen.add(ch.href);
                        allChapters.push(ch);
                    }
                }
            } catch(e) {
                continue;
            }
        }

        // Progress report every 20 pages
        if ((i + 1) % 20 === 0 || i + 1 === pageUrls.length) {
            // Signal progress via return value
        }
    }

    return JSON.stringify({
        totalPages: pageUrls.length,
        totalChapters: allChapters.length,
        gaveUp: consecutive429 >= 5 && cooldownCount > 2,
        chapters: allChapters
    });
}
"""

            result_json = page.evaluate(js_code, {"pageUrls": page_urls, "baseUrl": base_url})
            data = json.loads(result_json)

            if data.get("gaveUp") and len(page_urls) > 20:
                self._log_func(f"  [!] 部分页面被限流, 获取到 {data['totalChapters']} 章")

            for ch in data.get("chapters", []):
                if ch["href"] not in seen_urls:
                    seen_urls.add(ch["href"])
                    all_chapters.append(Chapter(
                        index=len(all_chapters) + 1,
                        title=ch["title"], play_url=ch["href"],
                    ))

            if total_pages > 1:
                self._log_func(f"  [*] 解析完成: {len(all_chapters)} 章 / {total_pages} 页")

        except Exception as e:
            self._log_func(f"  [!] Playwright 初始化失败: {e}")
            return []
        finally:
            try:
                if context:
                    context.close()
                if browser:
                    browser.close()
                if pw:
                    pw.stop()
            except Exception:
                pass

        return all_chapters

    def _extract_chapters_from_links(self, play_links, base_url: str) -> List[Chapter]:
        chapters = []
        seen = set()
        for link in play_links:
            href = link.get("href", "")
            if not href or href in seen:
                continue
            seen.add(href)
            title = link.text_content().strip()
            if title in ["立即收听", ""]:
                continue
            full_url = urljoin(base_url, href).replace("m.ting13.cc", "www.ting13.cc")
            chapters.append(Chapter(
                index=len(chapters) + 1,
                title=title or f"Chapter {len(chapters) + 1}",
                play_url=full_url,
            ))
        return chapters
