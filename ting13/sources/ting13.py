"""
ting13.cc 有声小说源

特点:
- 使用 Playwright 浏览器自动化提取音频 URL
- 支持登录 (cookies) 和未登录两种模式
- 桌面版 API: /api/mapi/play (需登录)
- 移动版 API: /api/key/readplay (免费章节)
"""

import json
import os
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin

import lxml.html

from .base import Source
from ting13.core.models import BookInfo, Chapter
from ting13.core.network import build_session, fetch_page, get_proxy, MOBILE_UA, DEFAULT_UA
from ting13.core.utils import is_frozen, get_bundled_base, get_chrome_exe_path

# Playwright 延迟导入 (仅在需要时)
_playwright_available = True
try:
    from playwright.sync_api import sync_playwright, Page
except ImportError:
    _playwright_available = False


# ══════════════════════════════════════════════════════════════
# Cookie 管理 (ting13.cc 登录)
# ══════════════════════════════════════════════════════════════

_COOKIE_FILE = os.path.join(os.path.expanduser("~"), ".ting13_cookies.json")
_cookies_store: List[dict] = []


def save_cookies(cookies: List[dict]):
    global _cookies_store
    _cookies_store = cookies
    try:
        with open(_COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_cookies() -> List[dict]:
    global _cookies_store
    if _cookies_store:
        return _cookies_store
    try:
        if os.path.isfile(_COOKIE_FILE):
            with open(_COOKIE_FILE, "r", encoding="utf-8") as f:
                _cookies_store = json.load(f)
                return _cookies_store
    except Exception:
        pass
    return []


def clear_cookies():
    global _cookies_store
    _cookies_store = []
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

_TRUSTED_DOMAINS = ["ysxs.top", "ting13.cc", "tingchina.com"]
_BLACKLISTED_DOMAINS = [
    "xmcdn.com", "ximalaya.com", "qtfm.cn", "lrts.me",
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
    base_url = "https://www.ting13.cc"

    def __init__(self):
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._headless = True

    # ── 配置 ──

    def set_headless(self, headless: bool):
        self._headless = headless

    # ── URL 识别 ──

    def detect_url_type(self, url: str) -> str:
        if "/play/" in url:
            return "play"
        elif "/youshengxiaoshuo/" in url or "/book/" in url:
            return "book"
        return "unknown"

    # ── 页面解析 ──

    def parse_book(self, url: str) -> BookInfo:
        desktop_url = url.replace("m.ting13.cc", "www.ting13.cc")
        content = fetch_page(desktop_url, referer=self.base_url + "/")
        tree = lxml.html.fromstring(content)

        # 书名
        title = "未知书名"
        title_elems = tree.cssselect("h1") or tree.cssselect(".title") or tree.cssselect("title")
        if title_elems:
            title = title_elems[0].text_content().strip()
            title = re.sub(r'\s*(有声小说|在线收听|全集).*$', '', title)

        # 作者
        author = "未知作者"
        author_elems = tree.cssselect(".author") or tree.cssselect("meta[property='og:music:artist']")
        if author_elems:
            if author_elems[0].tag == "meta":
                author = author_elems[0].get("content", "未知作者")
            else:
                author = author_elems[0].text_content().strip()

        # 封面
        cover_url = ""
        cover_elems = tree.cssselect("img.cover") or tree.cssselect(".bookcover img") or tree.cssselect("meta[property='og:image']")
        if cover_elems:
            if cover_elems[0].tag == "meta":
                cover_url = cover_elems[0].get("content", "")
            else:
                cover_url = cover_elems[0].get("src", "")

        # 章节列表
        chapters = []
        tingdirs_url = self._find_tingdirs_url(tree, desktop_url)
        if tingdirs_url:
            chapters = self._fetch_all_chapters(tingdirs_url, desktop_url)
        else:
            play_links = tree.cssselect("a[href*='/play/']")
            chapters = self._extract_chapters_from_links(play_links, desktop_url)

        return BookInfo(
            title=title, author=author, cover_url=cover_url,
            chapters=chapters, source_name=self.name,
        )

    # ── 音频 URL 提取 ──

    def get_audio_url(self, chapter: Chapter) -> Optional[str]:
        """使用 Playwright 打开播放页面提取音频 URL"""
        if not self._page:
            return None

        if has_cookies():
            target_url = chapter.play_url.replace("m.ting13.cc", "www.ting13.cc")
        else:
            target_url = chapter.play_url.replace("www.ting13.cc", "m.ting13.cc")

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
                    try:
                        body = response.text()
                        if body and ("mp3" in body or "m4a" in body or "audioUrl" in body):
                            try:
                                data = json.loads(body)
                                for key in ["audioUrl", "mp3", "m4a", "url", "audio_url", "src", "play_url"]:
                                    if key in data and data[key]:
                                        val = data[key]
                                        if isinstance(val, str) and (val.startswith("http") or val.startswith("//")):
                                            audio_urls_found.append(val)
                            except json.JSONDecodeError:
                                pass
                    except Exception:
                        pass
                if any(ext in url for ext in [".mp3", ".m4a", ".aac", ".wav", ".ogg"]):
                    if url.startswith("http"):
                        audio_urls_found.append(url)
            except Exception:
                pass

        self._page.on("response", handle_response)
        try:
            self._page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(4000)

            # DOM audio 元素
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

            # 等待可信 URL
            best = _pick_best_audio_url(audio_urls_found)
            if not best or not _is_trusted_audio_url(best):
                for _ in range(41):
                    self._page.wait_for_timeout(1000)
                    best = _pick_best_audio_url(audio_urls_found)
                    if best and _is_trusted_audio_url(best):
                        break
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

        except Exception as e:
            print(f"  [!] 页面加载出错: {e}")
        finally:
            self._page.remove_listener("response", handle_response)

        return _pick_best_audio_url(audio_urls_found)

    # ── 生命周期 ──

    def before_download(self, chapters, callbacks):
        """启动 Playwright 浏览器"""
        if not _playwright_available:
            callbacks.on_log("[FAIL] Playwright 未安装, 无法下载 ting13.cc")
            return

        callbacks.on_log("[*] 启动浏览器...")

        self._pw = sync_playwright().start()

        launch_kwargs: Dict = {"headless": self._headless}
        chrome_exe = get_chrome_exe_path()
        if chrome_exe:
            launch_kwargs["executable_path"] = chrome_exe
            callbacks.on_log(f"  [*] 使用内嵌浏览器")

        proxy = get_proxy()
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
            callbacks.on_log(f"  [*] 浏览器代理: {proxy}")

        self._browser = self._pw.chromium.launch(**launch_kwargs)

        stored_cookies = load_cookies()
        if stored_cookies:
            self._context = self._browser.new_context(user_agent=DEFAULT_UA)
            self._context.add_cookies(stored_cookies)
            callbacks.on_log(f"  [*] 已登录模式 ({len(stored_cookies)} cookies)")
        else:
            self._context = self._browser.new_context(
                user_agent=MOBILE_UA,
                viewport={"width": 375, "height": 812},
            )
            callbacks.on_log("  [*] 未登录模式 (移动版)")

        self._page = self._context.new_page()

    def after_download(self):
        """关闭浏览器"""
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

    # ── 认证 ──

    def supports_login(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return has_cookies()

    # ── 内部方法: 章节列表解析 ──

    def _find_tingdirs_url(self, tree, base_url: str) -> Optional[str]:
        for link in tree.cssselect("a[href*='/tingdirs/']"):
            text = link.text_content().strip()
            if "章节" in text or "目录" in text:
                return urljoin(base_url, link.get("href", ""))
        for link in tree.cssselect("a"):
            text = link.text_content().strip()
            href = link.get("href", "")
            if ("全部章节" in text or "更多章节" in text) and href:
                bookdir_url = urljoin(base_url, href)
                try:
                    content = fetch_page(bookdir_url, referer=self.base_url + "/")
                    bookdir_tree = lxml.html.fromstring(content)
                    for sublink in bookdir_tree.cssselect("a[href*='/tingdirs/']"):
                        subtext = sublink.text_content().strip()
                        if "目录" in subtext or "章节" in subtext:
                            return urljoin(base_url, sublink.get("href", ""))
                    for sublink in bookdir_tree.cssselect("a"):
                        subhref = sublink.get("href", "")
                        if "page=" in subhref and "sort=" in subhref:
                            return bookdir_url
                except Exception:
                    pass
        return None

    def _fetch_all_chapters(self, chapter_list_url: str, base_url: str) -> List[Chapter]:
        all_chapters = []
        seen_urls = set()

        content = fetch_page(chapter_list_url, referer=self.base_url + "/")
        tree = lxml.html.fromstring(content)

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

        for page_idx, page_url in enumerate(page_urls):
            try:
                content = fetch_page(page_url, referer=self.base_url + "/")
                tree = lxml.html.fromstring(content)
                play_links = tree.cssselect("a[href*='/play/']")

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
