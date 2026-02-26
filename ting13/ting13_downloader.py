#!/usr/bin/env python3
"""
ting13.cc 有声小说下载器
使用 Playwright 浏览器自动化获取音频 URL 并下载

用法:
    # 下载整本书（提供书籍页面 URL）
    python ting13_downloader.py "https://www.ting13.cc/youshengxiaoshuo/10408/"

    # 下载单集（提供播放页面 URL）
    python ting13_downloader.py "https://www.ting13.cc/play/10408_1_253355.html"

    # 指定输出目录
    python ting13_downloader.py -o "./downloads" "https://www.ting13.cc/youshengxiaoshuo/10408/"

    # 指定下载范围（第5集到第10集）
    python ting13_downloader.py --start 5 --end 10 "https://www.ting13.cc/youshengxiaoshuo/10408/"

安装依赖:
    pip install playwright requests lxml
    playwright install chromium
"""

import argparse
import io
import os
import re
import sys
import time
import json
import ssl
import random
import socket
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin, urlparse, quote

# 禁用 SSL 未验证警告（服务器 TLS 配置不规范）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple, Dict

# 修复 Windows 控制台编码问题
if sys.platform == "win32":
    if hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer") and getattr(sys.stderr, "encoding", "").lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ──────────────────────────────────────────────────────────────
# PyInstaller 打包支持
# ──────────────────────────────────────────────────────────────

def _is_frozen() -> bool:
    """检测是否在 PyInstaller 打包的 exe 中运行"""
    return getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


def _get_bundled_base() -> str:
    """获取 PyInstaller 解压的临时目录"""
    return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))


def _setup_playwright_env():
    """
    在打包模式下，设置 Playwright 所需的环境变量，
    让它能找到内嵌的 Chromium 浏览器和 Node 驱动。
    """
    if not _is_frozen():
        return

    base = _get_bundled_base()

    # 设置浏览器搜索路径：告诉 Playwright 在打包目录中查找浏览器
    browsers_path = os.path.join(base, "ms-playwright")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

    # 设置 Playwright driver 路径（node.exe + package）
    driver_path = os.path.join(base, "playwright", "driver")
    if os.path.isdir(driver_path):
        os.environ["PLAYWRIGHT_DRIVER_PATH"] = driver_path


_setup_playwright_env()


try:
    from playwright.sync_api import sync_playwright, Page, Browser
except ImportError:
    print("错误: 请先安装 playwright")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)

try:
    import lxml.html
except ImportError:
    print("错误: 请先安装 lxml")
    print("  pip install lxml")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────

class Chapter:
    """表示一个章节"""
    def __init__(self, index: int, title: str, play_url: str):
        self.index = index
        self.title = title
        self.play_url = play_url
        self.audio_url: Optional[str] = None
        self.downloaded: bool = False

    def __repr__(self):
        return f"Chapter({self.index}, '{self.title}', audio={'Yes' if self.audio_url else 'No'})"


class BookInfo:
    """表示一本有声书"""
    def __init__(self, title: str, author: str, cover_url: str, chapters: List[Chapter]):
        self.title = title
        self.author = author
        self.cover_url = cover_url
        self.chapters = chapters

    def __repr__(self):
        return f"BookInfo('{self.title}', chapters={len(self.chapters)})"


# ──────────────────────────────────────────────────────────────
# 页面解析
# ──────────────────────────────────────────────────────────────

class _TLSAdapter(HTTPAdapter):
    """自定义 TLS 适配器，解决部分服务器 SSL 握手失败的问题"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# ── Cookie 管理（登录状态保持） ───────────────────────────

_cookies_store: List[dict] = []
_COOKIE_FILE = os.path.join(os.path.expanduser("~"), ".ting13_cookies.json")


def save_cookies(cookies: List[dict]):
    """保存 cookies（Playwright 格式列表）"""
    global _cookies_store
    _cookies_store = cookies
    try:
        with open(_COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_cookies() -> List[dict]:
    """从文件加载 cookies"""
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
    """清除已保存的 cookies"""
    global _cookies_store
    _cookies_store = []
    try:
        if os.path.isfile(_COOKIE_FILE):
            os.remove(_COOKIE_FILE)
    except Exception:
        pass


def has_cookies() -> bool:
    """检查是否已有 cookies"""
    return bool(load_cookies())


def _cookies_for_requests() -> dict:
    """将 Playwright 格式 cookies 转换为 requests 可用的 dict"""
    cookies = load_cookies()
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}


# ── 代理管理 ─────────────────────────────────────────────

_proxy: Optional[str] = None


def set_proxy(proxy: Optional[str]):
    """设置代理，格式: http://127.0.0.1:7890 或 socks5://127.0.0.1:1080"""
    global _proxy
    _proxy = proxy.strip() if proxy and proxy.strip() else None


def get_proxy() -> Optional[str]:
    return _proxy


# ── DoH 智能 DNS（防 DNS 污染）────────────────────────────

_dns_cache: Dict[str, str] = {}


def resolve_via_doh(domain: str) -> Optional[str]:
    """通过 DoH (DNS over HTTPS) 解析域名，绕过本地 DNS 污染"""
    if domain in _dns_cache:
        return _dns_cache[domain]

    doh_servers = [
        f"https://1.1.1.1/dns-query?name={domain}&type=A",
        f"https://8.8.8.8/resolve?name={domain}&type=A",
    ]
    for url in doh_servers:
        try:
            resp = requests.get(
                url, headers={"Accept": "application/dns-json"},
                timeout=8, verify=False,
            )
            data = resp.json()
            ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
            if ips:
                _dns_cache[domain] = ips[0]
                return ips[0]
        except Exception:
            continue
    return None


def _is_dns_poisoned(domain: str) -> bool:
    """检测本地 DNS 是否被污染（返回的 IP 与 DoH 不一致）"""
    try:
        local_ip = socket.getaddrinfo(domain, 443, socket.AF_INET)[0][4][0]
        real_ip = resolve_via_doh(domain)
        if real_ip and local_ip != real_ip:
            return True
    except Exception:
        pass
    return False


# ── 系统代理自动检测 ──────────────────────────────────────

def detect_system_proxy() -> Optional[str]:
    """自动检测系统代理（Windows 注册表 → 环境变量 → 常见端口探测）"""
    # 1. Windows 注册表
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enable:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if server:
                    # 处理 "http=host:port;https=host:port" 格式
                    if "=" in server:
                        for part in server.split(";"):
                            if part.strip().startswith("http="):
                                server = part.strip()[5:]
                                break
                    if not server.startswith(("http://", "https://", "socks")):
                        server = "http://" + server
                    return server
    except Exception:
        pass

    # 2. 环境变量
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        val = os.environ.get(var)
        if val:
            return val

    # 3. 探测常见 Clash 端口
    for port in (7890, 7891, 7897, 1080):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            proto = "socks5" if port == 1080 else "http"
            return f"{proto}://127.0.0.1:{port}"
        except Exception:
            continue

    return None


# ── Clash API 集成（自动换 IP）────────────────────────────

class ClashRotator:
    """通过 Clash External Controller API 自动轮换代理节点"""

    # 常见 Clash / Clash Verge 控制端口
    COMMON_PORTS = [9090, 9097, 36925]

    def __init__(self, api_url: Optional[str] = None, secret: str = ""):
        self.api_url = api_url.rstrip("/") if api_url else ""
        self.secret = secret
        self.group_name: Optional[str] = None
        self.nodes: List[str] = []
        self.current_idx: int = -1
        self._headers: dict = {"Content-Type": "application/json"}
        if secret:
            self._headers["Authorization"] = f"Bearer {secret}"

    # ── 检测 ──

    def auto_detect(self) -> bool:
        """自动检测 Clash API 地址和密钥"""
        # 尝试从 Clash Verge 配置文件读取 API 地址和密钥
        self._try_read_clash_config()

        if self.api_url and self._ping(self.api_url):
            return True
        for port in self.COMMON_PORTS:
            url = f"http://127.0.0.1:{port}"
            if self._ping(url):
                self.api_url = url
                return True
        return False

    def _try_read_clash_config(self):
        """尝试从 Clash Verge 配置文件读取 API 地址和密钥"""
        config_dirs = [
            os.path.join(os.path.expanduser("~"), "AppData", "Roaming",
                         "io.github.clash-verge-rev.clash-verge-rev"),
            os.path.join(os.path.expanduser("~"), ".config", "clash-verge-rev"),
            os.path.join(os.path.expanduser("~"), ".config", "clash-verge"),
        ]
        for config_dir in config_dirs:
            config_file = os.path.join(config_dir, "config.yaml")
            if os.path.isfile(config_file):
                try:
                    with open(config_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    # 简单解析 YAML（不依赖 PyYAML）
                    for line in content.splitlines():
                        line = line.strip()
                        if line.startswith("external-controller:"):
                            addr = line.split(":", 1)[1].strip()
                            # addr like "127.0.0.1:9097"
                            if addr and not self.api_url:
                                self.api_url = f"http://{addr}"
                        if line.startswith("secret:"):
                            val = line.split(":", 1)[1].strip()
                            if val and not self.secret:
                                self.secret = val
                                self._headers["Authorization"] = f"Bearer {val}"
                    if self.api_url:
                        break
                except Exception:
                    continue

    def _ping(self, url: str) -> bool:
        try:
            resp = requests.get(
                f"{url}/version", headers=self._headers, timeout=2
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ── 加载节点 ──

    def load_nodes(self) -> List[str]:
        """获取可切换的代理节点列表"""
        if not self.api_url:
            return []
        try:
            resp = requests.get(
                f"{self.api_url}/proxies",
                headers=self._headers,
                timeout=5,
            )
            data = resp.json()
            proxies = data.get("proxies", {})

            # 寻找 Selector 类型的代理组（排除 GLOBAL）
            group_types = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
            special = {"DIRECT", "REJECT", "GLOBAL", "COMPATIBLE"}

            selector_groups = [
                (name, info)
                for name, info in proxies.items()
                if info.get("type") == "Selector" and name not in special
            ]
            if not selector_groups:
                return []

            # 选节点最多的 Selector 组
            selector_groups.sort(key=lambda x: len(x[1].get("all", [])), reverse=True)
            group_name, group_info = selector_groups[0]
            self.group_name = group_name

            # 过滤出实际代理节点（排除组和特殊节点）
            self.nodes = [
                n
                for n in group_info.get("all", [])
                if n not in special
                and proxies.get(n, {}).get("type", "") not in group_types
            ]

            # 定位当前节点
            current = group_info.get("now", "")
            if current in self.nodes:
                self.current_idx = self.nodes.index(current)
            else:
                self.current_idx = 0

            return self.nodes
        except Exception:
            return []

    # ── 切换 ──

    def rotate(self) -> Optional[str]:
        """切换到下一个节点，返回新节点名"""
        if not self.nodes or not self.group_name:
            return None
        self.current_idx = (self.current_idx + 1) % len(self.nodes)
        node_name = self.nodes[self.current_idx]
        try:
            encoded_group = quote(self.group_name, safe="")
            resp = requests.put(
                f"{self.api_url}/proxies/{encoded_group}",
                json={"name": node_name},
                headers=self._headers,
                timeout=5,
            )
            if resp.status_code in (200, 204):
                return node_name
        except Exception:
            pass
        return None

    def get_current_node(self) -> Optional[str]:
        if self.nodes and 0 <= self.current_idx < len(self.nodes):
            return self.nodes[self.current_idx]
        return None


def _build_session() -> requests.Session:
    """构建带重试、TLS 容错、cookies 和代理的 Session"""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
    adapter = _TLSAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # 注入 cookies
    cookie_dict = _cookies_for_requests()
    if cookie_dict:
        session.cookies.update(cookie_dict)
    # 注入代理
    if _proxy:
        session.proxies = {"http": _proxy, "https": _proxy}
    return session


def _rewrite_url_with_doh(url: str) -> tuple:
    """如果 DNS 被污染，用 DoH 解析真实 IP 并改写 URL，返回 (new_url, host_header_or_None)"""
    parsed = urlparse(url)
    domain = parsed.hostname
    if not domain:
        return url, None

    # 检测 DNS 污染
    real_ip = resolve_via_doh(domain)
    if not real_ip:
        return url, None

    try:
        local_ip = socket.getaddrinfo(domain, 443, socket.AF_INET)[0][4][0]
    except Exception:
        local_ip = None

    if local_ip and local_ip != real_ip:
        # DNS 被污染，改写为真实 IP
        new_url = url.replace(f"://{domain}", f"://{real_ip}", 1)
        return new_url, domain
    return url, None


def fetch_page(url: str) -> bytes:
    """获取页面内容（带 SSL 容错、重试、cookies 和 DoH DNS 防污染）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    session = _build_session()

    # 先尝试直接请求
    try:
        resp = session.get(url, headers=headers, timeout=30, verify=False)
        resp.raise_for_status()
        return resp.content
    except Exception:
        pass

    # 如果失败，尝试 DoH 解析真实 IP
    new_url, host = _rewrite_url_with_doh(url)
    if host:
        headers["Host"] = host
        resp = session.get(new_url, headers=headers, timeout=30, verify=False)
        resp.raise_for_status()
        return resp.content

    # 最后兜底，抛出原始异常
    resp = session.get(url, headers=headers, timeout=30, verify=False)
    resp.raise_for_status()
    return resp.content


def parse_book_page(url: str) -> BookInfo:
    """
    解析书籍详情页，获取书名、作者和章节列表

    支持的 URL 格式:
    - https://www.ting13.cc/youshengxiaoshuo/10408/
    - https://www.ting13.cc/book/10408.html
    - https://m.ting13.cc/youshengxiaoshuo/10408/
    """
    print(f"[*] 正在解析书籍页面: {url}")

    # 多域名/协议兜底，避免单一入口不可用导致解析失败
    parsed = urlparse(url)
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    candidates = [
        f"http://m.ting13.cc{path}{query}",
        f"http://www.ting13.cc{path}{query}",
        f"https://www.ting13.cc{path}{query}",
        url,
    ]
    seen = set()
    content = None
    page_base_url = url
    last_err = None
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            content = fetch_page(candidate)
            page_base_url = candidate
            break
        except Exception as e:
            last_err = e
            continue
    if content is None:
        raise RuntimeError(f"获取书籍页面失败: {last_err}")
    tree = lxml.html.fromstring(content)

    # 提取书名
    title_elems = tree.cssselect("h1") or tree.cssselect(".title") or tree.cssselect("title")
    title = "未知书名"
    if title_elems:
        title = title_elems[0].text_content().strip()
        # 清理标题中的"有声小说"等后缀
        title = re.sub(r'\s*(有声小说|在线收听|全集).*$', '', title)
    print(f"  书名: {title}")

    # 提取作者
    author = "未知作者"
    author_elems = tree.cssselect(".author") or tree.cssselect("meta[property='og:music:artist']")
    if author_elems:
        if author_elems[0].tag == "meta":
            author = author_elems[0].get("content", "未知作者")
        else:
            author = author_elems[0].text_content().strip()
    print(f"  作者/播音: {author}")

    # 提取封面
    cover_url = ""
    cover_elems = tree.cssselect("img.cover") or tree.cssselect(".bookcover img") or tree.cssselect("meta[property='og:image']")
    if cover_elems:
        if cover_elems[0].tag == "meta":
            cover_url = cover_elems[0].get("content", "")
        else:
            cover_url = cover_elems[0].get("src", "")

    # ──────────────────────────────────────────
    # 提取完整章节列表
    # ──────────────────────────────────────────

    chapters = []
    tingdirs_url = _find_tingdirs_url(tree, page_base_url)

    if tingdirs_url:
        print(f"  找到完整章节目录: {tingdirs_url}")
        chapters = _fetch_all_chapters(tingdirs_url, page_base_url)
    else:
        # 回退: 直接从书籍页面提取（只有最新几集）
        print("  未找到完整目录，从书籍页面提取章节...")
        play_links = tree.cssselect("a[href*='/play/']")
        chapters = _extract_chapters_from_links(play_links, page_base_url)

    print(f"  共找到 {len(chapters)} 个章节")
    return BookInfo(title=title, author=author, cover_url=cover_url, chapters=chapters)


def _find_tingdirs_url(tree, base_url: str) -> Optional[str]:
    """
    从书籍页面中找到完整章节目录的 tingdirs URL

    路径可能是:
    - 直接: 书籍页 -> tingdirs 链接
    - 间接: 书籍页 -> bookdir 链接 -> tingdirs 链接
    """
    # 方法1: 直接在页面上查找 tingdirs 链接
    for link in tree.cssselect("a[href*='/tingdirs/']"):
        text = link.text_content().strip()
        if "章节" in text or "目录" in text:
            return urljoin(base_url, link.get("href", ""))

    # 方法2: 通过 bookdir 页面间接获取
    for link in tree.cssselect("a"):
        text = link.text_content().strip()
        href = link.get("href", "")
        if ("全部章节" in text or "更多章节" in text) and href:
            bookdir_url = urljoin(base_url, href)
            print(f"  跟踪目录链接: {bookdir_url}")
            try:
                bookdir_content = fetch_page(bookdir_url)
                bookdir_tree = lxml.html.fromstring(bookdir_content)
                # 在 bookdir 页面中查找 tingdirs 链接
                for sublink in bookdir_tree.cssselect("a[href*='/tingdirs/']"):
                    subtext = sublink.text_content().strip()
                    if "目录" in subtext or "章节" in subtext:
                        return urljoin(base_url, sublink.get("href", ""))
                # 如果 bookdir 本身有分页章节，直接用它
                for sublink in bookdir_tree.cssselect("a"):
                    subhref = sublink.get("href", "")
                    if "page=" in subhref and "sort=" in subhref:
                        return bookdir_url
            except Exception as e:
                print(f"  [!] 获取目录页面失败: {e}")

    return None


def _fetch_all_chapters(chapter_list_url: str, base_url: str) -> List[Chapter]:
    """
    从完整章节目录页面获取所有章节（自动处理分页）

    分页格式: ?page=1&sort=asc, ?page=2&sort=asc, ...
    """
    all_chapters = []
    seen_urls = set()

    # 先获取第一页，找到分页信息
    content = fetch_page(chapter_list_url)
    tree = lxml.html.fromstring(content)

    # 查找分页链接，确定总页数
    page_urls = []
    for link in tree.cssselect("a"):
        href = link.get("href", "")
        if "page=" in href and "sort=" in href:
            full_href = urljoin(chapter_list_url, href)
            if full_href not in page_urls:
                page_urls.append(full_href)

    if not page_urls:
        # 没有分页，只有一页
        page_urls = [chapter_list_url + "?page=1&sort=asc"]

    # 确保使用 sort=asc（正序）
    page_urls = [u.replace("sort=desc", "sort=asc") for u in page_urls]
    # 按页码排序
    page_urls.sort(key=lambda u: int(re.search(r'page=(\d+)', u).group(1)) if re.search(r'page=(\d+)', u) else 0)

    print(f"  共 {len(page_urls)} 页章节目录")

    for page_idx, page_url in enumerate(page_urls):
        print(f"    获取第 {page_idx + 1}/{len(page_urls)} 页...", end="", flush=True)
        try:
            content = fetch_page(page_url)
            tree = lxml.html.fromstring(content)
            play_links = tree.cssselect("a[href*='/play/']")

            count = 0
            for link in play_links:
                href = link.get("href", "")
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)

                chapter_title = link.text_content().strip()
                # 跳过非章节链接
                if chapter_title in ["立即收听", ""]:
                    continue

                full_url = urljoin(base_url, href)
                full_url = full_url.replace("m.ting13.cc", "www.ting13.cc")

                all_chapters.append(Chapter(
                    index=len(all_chapters) + 1,
                    title=chapter_title,
                    play_url=full_url
                ))
                count += 1

            print(f" {count} 个章节")
        except Exception as e:
            print(f" 出错: {e}")

    return all_chapters


def _extract_chapters_from_links(play_links, base_url: str) -> List[Chapter]:
    """从页面中的播放链接提取章节"""
    chapters = []
    seen_urls = set()

    for i, link in enumerate(play_links):
        href = link.get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        chapter_title = link.text_content().strip()
        if chapter_title in ["立即收听", ""]:
            continue

        full_url = urljoin(base_url, href)
        full_url = full_url.replace("m.ting13.cc", "www.ting13.cc")

        chapters.append(Chapter(
            index=len(chapters) + 1,
            title=chapter_title or f"Chapter {len(chapters) + 1}",
            play_url=full_url
        ))

    return chapters


# ──────────────────────────────────────────────────────────────
# 音频 URL 提取（使用 Playwright）
# ──────────────────────────────────────────────────────────────

def _is_trusted_audio_url(url: str) -> bool:
    """判断音频 URL 是否来自 ting13 的可信音频 CDN"""
    trusted_domains = [
        "ysxs.top",         # ting13 主要音频 CDN
        "ting13.cc",        # ting13 自身域名
        "tingchina.com",    # 听中国
    ]
    url_lower = url.lower()
    return any(domain in url_lower for domain in trusted_domains)


def _is_blacklisted_audio_url(url: str) -> bool:
    """判断音频 URL 是否来自第三方广告/嵌入（应排除）"""
    blacklisted_domains = [
        "xmcdn.com",        # 喜马拉雅 CDN
        "ximalaya.com",     # 喜马拉雅
        "qtfm.cn",          # 蜻蜓 FM
        "lrts.me",          # 荔枝 FM
        "kaolafm.net",      # 考拉 FM
        "kugou.com",        # 酷狗
        "kuwo.cn",          # 酷我
        "163.com",          # 网易云
        "qqmusic.qq.com",   # QQ 音乐
        "douyin.com",       # 抖音
        "bytedance",        # 字节跳动
        "googlesyndication",# Google 广告
        "googleads",        # Google 广告
    ]
    url_lower = url.lower()
    return any(domain in url_lower for domain in blacklisted_domains)


def _pick_best_audio_url(audio_urls: List[str]) -> Optional[str]:
    """
    从候选音频 URL 列表中选出最佳 URL

    优先级：
    1. 可信域名的 .mp3
    2. 可信域名的其他格式
    3. 非黑名单域名的 .mp3
    4. 非黑名单域名的其他格式
    5. 放弃黑名单域名的 URL（不返回）
    """
    if not audio_urls:
        return None

    # 排除黑名单
    clean_urls = [u for u in audio_urls if not _is_blacklisted_audio_url(u)]
    # 可信域名
    trusted_urls = [u for u in clean_urls if _is_trusted_audio_url(u)]

    # 优先级 1: 可信 + mp3
    trusted_mp3 = [u for u in trusted_urls if ".mp3" in u]
    if trusted_mp3:
        return trusted_mp3[0]

    # 优先级 2: 可信 + 任意格式
    if trusted_urls:
        return trusted_urls[0]

    # 优先级 3: 非黑名单 + mp3
    clean_mp3 = [u for u in clean_urls if ".mp3" in u]
    if clean_mp3:
        return clean_mp3[0]

    # 优先级 4: 非黑名单 + 任意格式
    if clean_urls:
        return clean_urls[0]

    # 全部是黑名单域名，不返回
    return None


def extract_audio_url_fast(play_url: str, session: "Optional[requests.Session]" = None) -> Optional[str]:
    """
    直接通过 HTTP API 调用获取音频 URL，无需 Playwright。
    比浏览器模式快 10-50 倍（~0.3s vs 5-30s）。

    尝试多个 API 端点和域名组合，返回第一个有效的可信音频 URL。
    可传入已有 session 以复用 TCP 连接。
    """
    parsed = urlparse(play_url)
    play_path = parsed.path

    if session is None:
        session = _build_session()
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                      "Version/16.0 Mobile/15E148 Safari/604.1",
        "Referer": play_url,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    # 从 play_url 路径提取 book_id 和 chapter_num
    # /play/10408_1.html → book_id=10408, num=1
    m = re.search(r'/play/(\d+)_(\d+)(?:_\d+)?\.html', play_path)
    if not m:
        return None
    book_id, chapter_num = m.group(1), m.group(2)

    # API 端点列表（按优先级排列）
    api_endpoints = [
        ("http://m.ting13.cc", f"/api/key/readplay?book_id={book_id}&chapter_num={chapter_num}"),
        ("http://www.ting13.cc", f"/api/key/readplay?book_id={book_id}&chapter_num={chapter_num}"),
        ("http://m.ting13.cc", f"/api/mapi/play?book_id={book_id}&chapter_num={chapter_num}"),
        ("https://www.ting13.cc", f"/api/key/readplay?book_id={book_id}&chapter_num={chapter_num}"),
        ("https://www.ting13.cc", f"/api/mapi/play?book_id={book_id}&chapter_num={chapter_num}"),
    ]

    for base, endpoint in api_endpoints:
        url = base + endpoint
        try:
            resp = session.get(url, headers=headers, timeout=10, verify=False)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for key in ["audioUrl", "mp3", "m4a", "url", "audio_url", "src", "play_url"]:
                if key in data and data[key]:
                    audio_url = data[key]
                    if isinstance(audio_url, str) and (
                        audio_url.startswith("http") or audio_url.startswith("//")
                    ):
                        if _is_trusted_audio_url(audio_url) and not _is_blacklisted_audio_url(audio_url):
                            return audio_url
        except Exception:
            continue

    return None


def extract_audio_url(page: Page, play_url: str, timeout: int = 30) -> Optional[str]:
    """
    使用 Playwright 打开播放页面，截获音频 URL

    策略：
    - 已登录时：使用桌面版页面（www.ting13.cc），调用 /api/mapi/play
      移动版 /api/key/readplay 不识别登录 cookies，仍返回 406
    - 未登录时：使用移动版页面（m.ting13.cc），调用 /api/key/readplay
      该 API 返回 ysxs.top 的正确音频（仅限免费章节）
    """
    # 根据登录状态选择版本
    if has_cookies():
        # 已登录 → 桌面版（/api/mapi/play 支持登录态）
        target_url = play_url.replace("m.ting13.cc", "www.ting13.cc")
    else:
        # 未登录 → 移动版（/api/key/readplay 免费章节可用）
        target_url = play_url.replace("www.ting13.cc", "m.ting13.cc")

    audio_urls_found = []
    api_responses_log = []  # 记录关键 API 响应

    def handle_response(response):
        """拦截网络响应，查找包含音频 URL 的 API 响应"""
        url = response.url

        # 跳过明显不相关的请求
        if any(skip in url for skip in [
            'google', 'baidu', 'analytics', 'adsbygoogle',
            '.gif', '.png', '.jpg', '.css', '.ico', 'favicon'
        ]):
            return

        try:
            content_type = response.headers.get("content-type", "")

            # 记录关键 API 响应（play 相关）
            is_play_api = any(kw in url for kw in ['/api/', 'play', 'audio', 'readplay', 'mapi'])
            if is_play_api and ("json" in content_type or "text" in content_type):
                try:
                    body_text = response.text()
                    api_responses_log.append(f"  [API] {response.status} {url[:80]}")
                    if body_text and len(body_text) < 500:
                        api_responses_log.append(f"        body: {body_text[:200]}")
                except Exception:
                    pass

            # 检查 JSON 响应中是否包含音频 URL
            if "json" in content_type or "text" in content_type:
                try:
                    body = response.text()
                    if body and ("mp3" in body or "m4a" in body or "audio" in body or "audioUrl" in body):
                        try:
                            data = json.loads(body)
                            # 移动版 API 返回 audioUrl 字段
                            for key in ["audioUrl", "mp3", "m4a", "url", "audio_url", "src", "play_url"]:
                                if key in data and data[key]:
                                    url_val = data[key]
                                    if isinstance(url_val, str) and (
                                        url_val.startswith("http") or url_val.startswith("//")
                                    ):
                                        audio_urls_found.append(url_val)
                            # 检查是否有 status 字段指示错误
                            if "status" in data and data["status"] != 200:
                                print(f"  [!] API 返回非200状态: status={data.get('status')}, msg={data.get('msg', '')}")
                        except json.JSONDecodeError:
                            pass
                except Exception:
                    pass

            # 直接检查是否是音频文件请求
            if any(ext in url for ext in [".mp3", ".m4a", ".aac", ".wav", ".ogg"]):
                if url.startswith("http"):
                    audio_urls_found.append(url)

        except Exception:
            pass

    # 注册响应监听器
    page.on("response", handle_response)

    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

        # 等待页面 JS 执行（音频 URL 是通过 AJAX 获取的）
        page.wait_for_timeout(4000)

        # 尝试从 DOM audio 元素获取
        try:
            dom_audio_url = page.evaluate("""() => {
                const audio = document.querySelector('audio');
                if (audio && audio.src) return audio.src;
                if (audio && audio.currentSrc) return audio.currentSrc;
                const source = document.querySelector('audio source');
                if (source && source.src) return source.src;
                return null;
            }""")
            if dom_audio_url:
                audio_urls_found.append(dom_audio_url)
        except Exception:
            pass

        # 等待可信 URL 出现
        best = _pick_best_audio_url(audio_urls_found)
        if not best or not _is_trusted_audio_url(best):
            for _ in range(timeout - 4):
                page.wait_for_timeout(1000)

                best = _pick_best_audio_url(audio_urls_found)
                if best and _is_trusted_audio_url(best):
                    break

                # 再次从 DOM 获取
                try:
                    audio_src = page.evaluate("""() => {
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
        page.remove_listener("response", handle_response)

    # 从所有候选 URL 中选出最佳
    result = _pick_best_audio_url(audio_urls_found)

    # 调试信息
    blacklisted = [u for u in audio_urls_found if _is_blacklisted_audio_url(u)]
    if blacklisted:
        print(f"  [!] 已过滤 {len(blacklisted)} 个第三方音频 URL")
        for bu in blacklisted:
            print(f"      过滤: {bu[:100]}")

    # 如果没有可信结果，打印调试信息
    if not result:
        if audio_urls_found:
            print(f"  [DEBUG] 所有候选 URL ({len(audio_urls_found)} 个):")
            for u in audio_urls_found:
                print(f"      {u[:150]}")
        else:
            print("  [DEBUG] 未捕获到任何音频 URL")
        if api_responses_log:
            print("  [DEBUG] 关键 API 响应:")
            for line in api_responses_log:
                print(line)

    return result


# ──────────────────────────────────────────────────────────────
# 下载
# ──────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符"""
    # 替换 Windows 不允许的字符
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    # 移除前后空格和点号
    name = name.strip('. ')
    return name or "untitled"


MIN_VALID_FILE_SIZE = 10240  # 10KB — smaller files are likely error pages


def download_file(url: str, filepath: str, session: "Optional[requests.Session]" = None) -> bool:
    """下载文件到指定路径"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.ting13.cc/"
        }

        # 处理 // 开头的 URL
        if url.startswith("//"):
            url = "https:" + url

        if session is None:
            session = _build_session()
        resp = session.get(url, headers=headers, stream=True, timeout=60, verify=False)
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded / total * 100
                        print(f"\r    下载进度: {pct:.1f}% ({downloaded}/{total})", end="", flush=True)
        print()

        if downloaded < MIN_VALID_FILE_SIZE:
            print(f"    [!] 文件过小 ({downloaded} bytes)，可能不是有效音频")
            try:
                os.remove(filepath)
            except OSError:
                pass
            return False

        return True

    except Exception as e:
        print(f"\n    [FAIL] 下载失败: {e}")
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass
        return False


def download_cover(cover_url: str, output_dir: str) -> Optional[str]:
    """下载封面图片"""
    if not cover_url:
        return None
    try:
        ext = os.path.splitext(urlparse(cover_url).path)[1] or ".jpg"
        filepath = os.path.join(output_dir, f"cover{ext}")
        if download_file(cover_url, filepath):
            return filepath
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────

def detect_url_type(url: str) -> str:
    """检测 URL 类型: 'book' 或 'play'"""
    if "/play/" in url:
        return "play"
    elif "/youshengxiaoshuo/" in url or "/book/" in url:
        return "book"
    else:
        return "unknown"


def download_book(
    url: str,
    output_dir: str = ".",
    start: int = 1,
    end: Optional[int] = None,
    headless: bool = True,
    clash_rotator: Optional[ClashRotator] = None,
    rotate_interval: int = 30,
):
    """
    下载有声书

    :param url: 书籍页面或播放页面 URL
    :param output_dir: 输出目录
    :param start: 起始章节（1-based）
    :param end: 结束章节（含，None 表示全部）
    :param headless: 是否使用无头浏览器
    """
    url_type = detect_url_type(url)

    if url_type == "play":
        # 单集下载
        print("=" * 60)
        print("[*] 下载单集音频")
        print("=" * 60)
        chapters = [Chapter(index=1, title="单集", play_url=url)]
        book_title = "ting13_audio"
    elif url_type == "book":
        # 整本书下载
        book_info = parse_book_page(url)
        chapters = book_info.chapters
        book_title = book_info.title

        if not chapters:
            print("[FAIL] 未找到任何章节，请检查 URL 是否正确")
            return

        # 处理范围
        start_idx = max(0, start - 1)
        end_idx = end if end else len(chapters)
        chapters = chapters[start_idx:end_idx]

        print(f"\n[*] 准备下载: {book_title}")
        print(f"   共 {len(chapters)} 个章节 (第{start}集 到 第{start + len(chapters) - 1}集)")

        # 下载封面
        if book_info.cover_url:
            book_dir = os.path.join(output_dir, sanitize_filename(book_title))
            os.makedirs(book_dir, exist_ok=True)
            print(f"\n[*] 下载封面...")
            download_cover(book_info.cover_url, book_dir)
    else:
        print(f"[FAIL] 无法识别的 URL 类型: {url}")
        print("  支持的格式:")
        print("  - https://www.ting13.cc/youshengxiaoshuo/书籍ID/")
        print("  - https://www.ting13.cc/play/书籍ID_源ID_章节ID.html")
        return

    # 创建输出目录
    book_dir = os.path.join(output_dir, sanitize_filename(book_title))
    os.makedirs(book_dir, exist_ok=True)

    print(f"\n[*] 输出目录: {os.path.abspath(book_dir)}")

    # 扫描缺失集数，优先补齐
    try:
        existing_files = os.listdir(book_dir)
    except OSError:
        existing_files = []

    downloaded_indices = set()
    for f in existing_files:
        parts = f.split("_", 1)
        if parts[0].isdigit():
            downloaded_indices.add(int(parts[0]))

    missing_chapters = [ch for ch in chapters if ch.index not in downloaded_indices]

    if downloaded_indices and missing_chapters:
        min_dl = min(downloaded_indices)
        max_dl = max(downloaded_indices)
        gaps = [ch for ch in missing_chapters if min_dl <= ch.index <= max_dl]
        continuations = [ch for ch in missing_chapters if ch.index > max_dl]

        if gaps:
            gap_nums = [str(ch.index) for ch in gaps]
            print(f"[*] 检测到 {len(gaps)} 个缺失集: {', '.join(gap_nums[:20])}"
                  + ("... 等" if len(gaps) > 20 else ""))
            chapters = gaps + continuations
        else:
            chapters = missing_chapters
    elif missing_chapters:
        chapters = missing_chapters
    else:
        print("[*] 所有章节均已下载，无需重复下载")
        return

    print(f"[*] 待下载: {len(chapters)} 集")
    print("=" * 60)

    # 使用 Playwright 提取音频 URL 并下载
    success_count = 0
    fail_count = 0
    consecutive_fails = 0

    with sync_playwright() as pw:
        print("\n[*] 启动浏览器...")

        # 打包模式下，显式指定 Chromium 可执行文件路径
        launch_kwargs: Dict = {"headless": headless}
        if _is_frozen():
            base = _get_bundled_base()
            chrome_exe = os.path.join(
                base, "ms-playwright", "chromium-1208", "chrome-win64", "chrome.exe"
            )
            if os.path.isfile(chrome_exe):
                launch_kwargs["executable_path"] = chrome_exe
                print(f"  [*] 使用内嵌浏览器: {chrome_exe}")
            else:
                print(f"  [!] 警告: 未找到内嵌浏览器，尝试使用系统默认...")

        if _proxy:
            launch_kwargs["proxy"] = {"server": _proxy}
            print(f"  [*] 浏览器代理: {_proxy}")
        browser = pw.chromium.launch(**launch_kwargs)
        # 根据登录状态选择 UA:
        # - 已登录: 桌面版 → /api/mapi/play 支持登录态
        # - 未登录: 移动版 → /api/key/readplay 免费章节可用
        stored_cookies = load_cookies()
        if stored_cookies:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36",
            )
            context.add_cookies(stored_cookies)
            print(f"  [*] 已登录模式（桌面版），注入 {len(stored_cookies)} 个 cookies")
        else:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                           "Version/16.0 Mobile/15E148 Safari/604.1",
                viewport={"width": 375, "height": 812},
            )
            print("  [*] 未登录模式（移动版）")
        page = context.new_page()

        for i, chapter in enumerate(chapters):
            print(f"\n[{i+1}/{len(chapters)}] {chapter.title}")
            print(f"  URL: {chapter.play_url}")

            # 检查是否已下载
            safe_title = sanitize_filename(chapter.title)
            # 检查已存在的文件
            existing = [f for f in os.listdir(book_dir) if f.startswith(f"{chapter.index:04d}_")]
            if existing:
                print(f"  [SKIP] 已存在，跳过: {existing[0]}")
                success_count += 1
                continue

            # 提取音频 URL（无限重试直到成功）
            print(f"  [>] 提取音频 URL...")
            audio_url = None
            attempt = 0
            while not audio_url:
                attempt += 1
                try:
                    audio_url = extract_audio_url(page, chapter.play_url, timeout=45)
                except Exception as e:
                    print(f"  [!] 提取出错: {e}")

                if audio_url:
                    break

                # 等待时间：前3次 10s/30s/60s，之后固定 60s
                if attempt <= 3:
                    wait = min(10 * (3 ** (attempt - 1)), 60)
                else:
                    wait = 60

                print(f"  [!] 第{attempt}次失败，等待 {wait}s 后重试...")
                time.sleep(wait)

                # 每3次失败尝试换节点
                if attempt % 3 == 0 and clash_rotator and rotate_interval > 0:
                    new_node = clash_rotator.rotate()
                    if new_node:
                        print(f"  [*] 多次失败，已切换节点: {new_node}")
                        time.sleep(3)

                # 重新创建页面
                try:
                    page.close()
                    page = context.new_page()
                except Exception:
                    pass

            consecutive_fails = 0

            chapter.audio_url = audio_url
            print(f"  [OK] 音频 URL: {audio_url[:80]}...")

            # 确定文件扩展名
            ext = ".mp3"
            if ".m4a" in audio_url:
                ext = ".m4a"
            elif ".aac" in audio_url:
                ext = ".aac"

            # 下载音频
            filename = f"{chapter.index:04d}_{safe_title}{ext}"
            filepath = os.path.join(book_dir, filename)

            print(f"  [>] 下载中...")
            dl_attempt = 0
            dl_ok = False
            while not dl_ok:
                dl_attempt += 1
                dl_ok = download_file(audio_url, filepath)
                if dl_ok:
                    break
                wait = min(10 * dl_attempt, 60)
                print(f"  [!] 下载第{dl_attempt}次失败，等待 {wait}s 后重试...")
                time.sleep(wait)

            if dl_ok:
                chapter.downloaded = True
                success_count += 1
                print(f"  [OK] 保存为: {filename}")
            else:
                fail_count += 1

            # 反限流延迟：随机 2~5s，每20集长休息 15~30s
            base_delay = random.uniform(2.0, 5.0)
            if (i + 1) % 20 == 0:
                pause = random.uniform(15, 30)
                print(f"  [*] 已完成 {i+1} 集，休息 {pause:.0f}s...")
                time.sleep(pause)
            else:
                time.sleep(base_delay)

            # ── Clash 自动换 IP ──
            if clash_rotator and rotate_interval > 0 and (i + 1) % rotate_interval == 0:
                new_node = clash_rotator.rotate()
                if new_node:
                    print(f"  [*] 已切换代理节点: {new_node}")
                    time.sleep(3)  # 等待节点生效

        browser.close()

    # 保存下载记录
    record = {
        "title": book_title,
        "url": url,
        "chapters": [
            {
                "index": ch.index,
                "title": ch.title,
                "play_url": ch.play_url,
                "audio_url": ch.audio_url,
                "downloaded": ch.downloaded
            }
            for ch in chapters
        ]
    }
    record_file = os.path.join(book_dir, "download_record.json")
    with open(record_file, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    # 打印总结
    print("\n" + "=" * 60)
    print(f"[DONE] 下载完成!")
    print(f"   成功: {success_count}")
    print(f"   失败: {fail_count}")
    print(f"   输出: {os.path.abspath(book_dir)}")
    print("=" * 60)


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ting13.cc 有声小说下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 下载整本书
  python ting13_downloader.py "https://www.ting13.cc/youshengxiaoshuo/10408/"

  # 下载第5集到第10集
  python ting13_downloader.py --start 5 --end 10 "https://www.ting13.cc/youshengxiaoshuo/10408/"

  # 下载单集
  python ting13_downloader.py "https://www.ting13.cc/play/10408_1_253355.html"

  # 指定输出目录，使用非无头模式（显示浏览器窗口）
  python ting13_downloader.py -o "./audiobooks" --no-headless "https://www.ting13.cc/youshengxiaoshuo/10408/"
        """
    )
    parser.add_argument("url", help="书籍页面或播放页面 URL")
    parser.add_argument("-o", "--output", default=".", help="输出目录 (默认: 当前目录)")
    parser.add_argument("--start", type=int, default=1, help="起始章节 (默认: 1)")
    parser.add_argument("--end", type=int, default=None, help="结束章节 (默认: 全部)")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器窗口 (调试用)")
    parser.add_argument("--proxy", default=None, help="代理地址 (如 http://127.0.0.1:7890 或 auto 自动检测)")
    parser.add_argument("--rotate", type=int, default=0,
                        help="通过 Clash API 每 N 集自动换 IP (如 --rotate 30)")

    args = parser.parse_args()

    # 代理设置
    if args.proxy:
        if args.proxy.lower() == "auto":
            detected = detect_system_proxy()
            if detected:
                set_proxy(detected)
                print(f"[*] 自动检测到代理: {detected}")
            else:
                print("[!] 未检测到系统代理，将使用直连")
        else:
            set_proxy(args.proxy)

    # Clash 自动换 IP
    rotator = None
    if args.rotate > 0:
        rotator = ClashRotator()
        if rotator.auto_detect():
            nodes = rotator.load_nodes()
            print(f"[*] Clash API: {rotator.api_url}  代理组: {rotator.group_name}")
            print(f"[*] 可用节点: {len(nodes)} 个，每 {args.rotate} 集自动切换")
        else:
            print("[!] 未检测到 Clash API，自动换 IP 功能不可用")
            rotator = None

    print("=" * 60)
    print("  ting13.cc 有声小说下载器")
    print("=" * 60)

    download_book(
        url=args.url,
        output_dir=args.output,
        start=args.start,
        end=args.end,
        headless=not args.no_headless,
        clash_rotator=rotator,
        rotate_interval=args.rotate,
    )


if __name__ == "__main__":
    main()
