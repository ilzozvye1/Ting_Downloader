"""
huanting.cc (ting22.com) 有声小说源

特点:
- 双 API 策略:
  1. 移动端 apiM1.php (需 s1/s2/s3 认证头, 通过 curl_cffi 绕过 TLS 指纹检测)
  2. 桌面端 apiP2.php (章节 1~150 无需验证码, 151+ 需要 PHPATSSD cookie)
- 自动检测限流并轮换 IP
- 支持 curl_cffi TLS 指纹伪装 (safari17_2_ios)
- 章节 151+ 自动解算桌面端滑块验证码
- 音频: base64 编码的 xmcdn.com URL (m4a 格式)
"""

import base64
import hashlib
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from lxml import html as lxml_html

from .base import Source
from ting13.core.models import BookInfo, Chapter
from ting13.core.network import build_session, get_proxy, MOBILE_UA, ClashRotator, random_ua

# curl_cffi 用于绕过 TLS 指纹检测 (可选但强烈推荐)
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False

# OpenCV 用于验证码解算 (可选)
try:
    import numpy as np
    import cv2
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False

# Playwright (可选, 用于验证码后提取)
try:
    from playwright.sync_api import sync_playwright, Page
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False


# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

HUANTING_DOMAINS = ["ting22.com", "huanting.cc"]
BASE = "https://www.huanting.cc"
MOBILE_BASE = "https://m.huanting.cc"

# curl_cffi 浏览器指纹 (iOS Safari 效果最佳)
CFFI_BROWSER = "safari17_2_ios"


# ══════════════════════════════════════════════════════════════
# Source 实现
# ══════════════════════════════════════════════════════════════

class HuantingSource(Source):
    """huanting.cc / ting22.com 有声小说"""

    match = [
        r"ting22\.com",
        r"huanting\.cc",
    ]
    names = ["ting22.com", "huanting.cc"]
    base_url = "https://www.huanting.cc"

    # 验证码 cookie 有效次数 (每次 API 调用消耗 1 次, 到阈值时提前刷新)
    _COOKIE_REFRESH_THRESHOLD = 15

    def __init__(self):
        self._book_id: Optional[str] = None
        self._captcha_cookies: Optional[Dict[str, str]] = None  # 验证码 cookies 缓存
        self._clash_rotator: Optional[ClashRotator] = None
        self._cookie_use_count: int = 0   # cookie 使用次数
        self._last_captcha_url: str = ""  # 上次解算的 URL (用于主动刷新)

    def set_clash_rotator(self, rotator: Optional[ClashRotator]):
        """设置 Clash 节点轮换器 (用于验证码解算时切换 IP)"""
        self._clash_rotator = rotator

    # ── URL 识别 ──

    def detect_url_type(self, url: str) -> str:
        path = urlparse(url).path
        if "/book/" in path:
            return "book"
        if "/ting/" in path:
            return "play"
        return "unknown"

    # ── 书籍解析 ──

    def parse_book(self, url: str) -> BookInfo:
        url = url.replace("ting22.com", "huanting.cc")
        if not url.startswith("http"):
            url = "https://" + url
        if "www." not in url:
            url = url.replace("://huanting.cc", "://www.huanting.cc")

        book_id = _extract_book_id(url)
        if not book_id:
            raise ValueError(f"无法从 URL 提取书籍 ID: {url}")
        self._book_id = book_id

        session = build_session(referer=BASE + "/")
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        tree = lxml_html.fromstring(resp.text)

        # 书名
        title = ""
        h1 = tree.xpath("//h1/text()")
        if h1:
            title = h1[0].strip().replace("有声小说", "").strip()
        if not title:
            title = f"book_{book_id}"

        # 作者 / 播音
        author = ""
        author_el = tree.xpath('//span[@class="author"]/text()')
        if author_el:
            author = author_el[0].replace("作者：", "").strip()

        announcer = ""
        ann_el = tree.xpath('//span[@class="announcer"]/text()')
        if ann_el:
            announcer = ann_el[0].replace("主播：", "").strip()

        # 封面
        cover_url = ""
        img = tree.xpath('//div[@class="img"]/img/@src')
        if img:
            cover_url = img[0]

        # 分页章节列表
        def parse_chapter_list(page_tree):
            items = []
            for a in page_tree.xpath('//ul[@id="vlink"]/li/a'):
                ch_title = a.text_content().strip()
                ch_href = a.get("href", "")
                if ch_href and ch_title:
                    if not ch_href.startswith("http"):
                        ch_href = f"{BASE}{ch_href}"
                    items.append((ch_title, ch_href))
            return items

        page_links = tree.xpath('//div[@class="play_navs"]/a')
        total_pages = len(page_links) if page_links else 1

        all_raw = parse_chapter_list(tree)

        if total_pages > 1:
            print(f"  [*] 解析分页: 共 {total_pages} 页...")

        for p in range(2, total_pages + 1):
            page_url = f"{BASE}/book/{book_id}.html?p={p}"
            try:
                time.sleep(0.15)  # 缩短延迟 (0.3 → 0.15s)
                r = session.get(page_url, timeout=15)
                r.encoding = "utf-8"
                new_items = parse_chapter_list(lxml_html.fromstring(r.text))
                all_raw.extend(new_items)
                if p % 5 == 0 or p == total_pages:
                    print(f"  [*] 已解析 {p}/{total_pages} 页 ({len(all_raw)} 章)")
            except Exception as e:
                print(f"  [!] 第 {p} 页解析失败: {e}")

        chapters = [
            Chapter(index=i, title=t, play_url=href)
            for i, (t, href) in enumerate(all_raw, 1)
        ]

        return BookInfo(
            title=title, author=author, announcer=announcer,
            cover_url=cover_url, chapters=chapters,
            source_name=self.name,
            extra={"book_id": book_id},
        )

    # ── 音频 URL 提取 ──

    def get_audio_url(self, chapter: Chapter) -> Optional[str]:
        """
        获取音频 URL

        策略:
        1. 主动刷新: cookie 使用超过阈值时提前刷新
        2. 带 cookies 尝试 API
        3. API 返回 fail → 并行解算验证码 (3 并发)
        4. 用新 cookie 重试 API
        """
        book_id = self._book_id
        if not book_id:
            return None

        # 0. 主动刷新 cookie (不等过期)
        if (self._captcha_cookies
                and self._cookie_use_count >= self._COOKIE_REFRESH_THRESHOLD
                and self._last_captcha_url):
            print(f"  [验证码] cookie 已用 {self._cookie_use_count} 次, 主动刷新...")
            cookies = solve_desktop_captcha(
                self._last_captcha_url, proxy=get_proxy(),
                clash_rotator=self._clash_rotator, max_retries=6,
            )
            if cookies:
                self._captcha_cookies = cookies
                self._cookie_use_count = 0
                print(f"  [验证码] cookie 刷新成功")

        # 1. 带缓存的 cookies 尝试 API
        result = _api_get_audio(book_id, chapter.index,
                                cookies=self._captcha_cookies)
        if result and result != "RATE_LIMITED":
            self._cookie_use_count += 1
            return result

        if result == "RATE_LIMITED":
            print(f"  [!] API 被限流")
            return None

        # 2. API 失败 → 需要验证码
        if not _HAS_CV2:
            print(f"  [!] 章节 {chapter.index} 需要验证码, 但 OpenCV 未安装")
            print(f"  [!] 请安装: pip install opencv-python-headless numpy")
            return None

        proxy_info = get_proxy() or "直连"
        clash_info = "有" if self._clash_rotator else "无"
        print(f"  [验证码] 章节 {chapter.index} 需要验证码, 自动解算中... "
              f"(代理={proxy_info}, Clash={clash_info})")
        self._last_captcha_url = chapter.play_url
        cookies = solve_desktop_captcha(
            chapter.play_url, proxy=get_proxy(),
            clash_rotator=self._clash_rotator, max_retries=10,
        )

        if cookies:
            self._captcha_cookies = cookies
            self._cookie_use_count = 0
            print(f"  [验证码] 解算成功, 重试 API...")
            result = _api_get_audio(book_id, chapter.index, cookies=cookies)
            if result and result != "RATE_LIMITED":
                self._cookie_use_count += 1
                return result
            print(f"  [!] 验证码通过但 API 仍返回 fail")
        else:
            print(f"  [!] 验证码解算失败")

        return None

    def prefetch_audio_url(self, chapter: Chapter) -> Optional[str]:
        """
        快速预取: 仅做 API 调用, 不触发验证码解算

        用于流水线下载: 在后台线程中预取下一章 URL,
        避免预取线程和主线程同时解验证码 / 轮换 Clash 节点。
        """
        book_id = self._book_id
        if not book_id:
            return None

        result = _api_get_audio(book_id, chapter.index,
                                cookies=self._captcha_cookies)
        if result and result != "RATE_LIMITED":
            return result

        # API 失败 → 不触发验证码, 返回 None, 让主线程处理
        return None


# ══════════════════════════════════════════════════════════════
# 内部函数
# ══════════════════════════════════════════════════════════════

def _extract_book_id(url: str) -> Optional[str]:
    m = re.search(r"/book/(\d+)", url)
    return m.group(1) if m else None


# ── 移动端 API 认证头 (从 MTaudio2024.js 逆向) ──

def _compute_mobile_auth(book_id: str, chapter_id: str) -> Dict[str, str]:
    """
    从 MTaudio2024.js 逆向的认证算法:
    - hex_ts = Math.round(Date.now() / 1000).toString(16)
    - md5 = CryptoJS.MD5(book_id + '$' + hex_ts + '$' + chapter_id).toString()
    - s1 = md5.substr(8, 16)
    - s2 = hex_ts
    - s3 = reverse(base64(utf8(md5 + '@' + GPU + '@' + platform + '@' + W + '*' + H + '@' + hex_ts)))
    """
    ts = int(round(time.time()))
    hex_ts = format(ts, 'x')
    raw = f"{book_id}${hex_ts}${chapter_id}"
    md5 = hashlib.md5(raw.encode()).hexdigest()
    s1 = md5[8:24]
    s2 = hex_ts
    # 模拟 iPhone Safari
    gpu = "Apple GPU"
    platform = "iPhone"
    width = "375"
    height = "812"
    s3_raw = f"{md5}@{gpu}@{platform}@{width}*{height}@{hex_ts}"
    s3_b64 = base64.b64encode(s3_raw.encode('utf-8')).decode('utf-8')
    s3 = s3_b64[::-1]
    return {"s1": s1, "s2": s2, "s3": s3}


def _parse_api_response(resp_text: str) -> Optional[str]:
    """解析 API 响应, 返回音频 URL 或特殊状态标记"""
    text = resp_text.strip()

    # 检测限流 / HTML 错误页
    if text.startswith("<") or "频繁" in text or "受限" in text:
        return "RATE_LIMITED"

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    state = data.get("state", "")
    status = data.get("status", 0)

    if state != "success" or status not in (1, "1"):
        return None

    playlist = data.get("playlist", {})
    src_b64 = playlist.get("src", "")
    if not src_b64:
        return None

    # src 可能是: 直接 base64(url) 或 加密后的数据
    try:
        decoded = base64.b64decode(src_b64).decode("utf-8")
        if decoded.startswith("http"):
            return decoded
        if "$" in decoded:
            # 包含 '$' 说明是可直接使用的 URL
            return decoded
    except Exception:
        pass

    # 可能是加密数据, 需要额外解密 (暂不支持)
    return None


def _api_get_audio_mobile(book_id: str, chapter_pid: int,
                          proxy: Optional[str] = None) -> Optional[str]:
    """
    移动端 API (apiM1.php) — 使用 curl_cffi 绕过 TLS 指纹检测

    优势: 不需要验证码
    要求: curl_cffi 库, s1/s2/s3 认证头
    """
    if not _HAS_CFFI:
        return None

    auth = _compute_mobile_auth(book_id, str(chapter_pid))
    api_url = f"{MOBILE_BASE}/apiM1.php?id={book_id}&pid={chapter_pid}"

    try:
        s = cffi_requests.Session(impersonate=CFFI_BROWSER)

        # 先访问书籍页建立 session
        kwargs = {"verify": False, "timeout": 15}
        if proxy:
            kwargs["proxy"] = proxy

        s.get(f"{MOBILE_BASE}/book/{book_id}.html", **kwargs)

        # 调用 API
        resp = s.get(api_url, **kwargs, headers={
            "Referer": f"{MOBILE_BASE}/MTaudio.php",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
            "s1": auth["s1"],
            "s2": auth["s2"],
            "s3": auth["s3"],
        })
        resp.encoding = "utf-8"
        return _parse_api_response(resp.text)

    except Exception:
        return None


def _api_get_audio(book_id: str, chapter_pid: int,
                    cookies: Optional[Dict[str, str]] = None) -> Optional[str]:
    """
    桌面端 API (apiP2.php) — 传统方式

    章节 1~150 不需要验证码, 151+ 需要 PHPATSSD cookie
    """
    session = build_session(
        user_agent=random_ua(), referer=BASE + "/", cookies=cookies,
    )
    api_url = f"{BASE}/apiP2.php?id={book_id}&pid={chapter_pid}"

    try:
        resp = session.get(api_url, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return _parse_api_response(resp.text)

    except requests.RequestException:
        return None
    except (json.JSONDecodeError, KeyError):
        return None


def _api_get_audio_dual(book_id: str, chapter_pid: int,
                        cookies: Optional[Dict[str, str]] = None,
                        proxy: Optional[str] = None) -> Optional[str]:
    """
    双 API 策略: 优先尝试移动端 API, 失败则回退到桌面端 API

    返回值:
    - 音频 URL 字符串 (成功)
    - "RATE_LIMITED" (被限流)
    - None (失败)
    """
    # 策略 1: 移动端 API (curl_cffi)
    result = _api_get_audio_mobile(book_id, chapter_pid, proxy=proxy)
    if result and result != "RATE_LIMITED":
        return result

    # 策略 2: 桌面端 API (requests)
    result = _api_get_audio(book_id, chapter_pid, cookies=cookies)
    if result and result != "RATE_LIMITED":
        return result

    # 返回最后的状态
    return result  # None 或 "RATE_LIMITED"


# ══════════════════════════════════════════════════════════════
# 验证码解算 (保留完整实现, 供将来启用)
# ══════════════════════════════════════════════════════════════

def _decode_data_string(encoded: str) -> list:
    """解码验证码切片数据 (Caesar 密码: charCode - 3)"""
    encoded = encoded.replace("\\/", "/")
    return json.loads("".join(chr(ord(c) - 3) for c in encoded))


def _extract_captcha_data(html: str) -> dict:
    """从移动端播放页 HTML 提取验证码数据"""
    result: Dict = {}

    m = re.search(r'mpData\s*=\s*(\{)', html)
    if m:
        start = m.start(1)
        depth = 0
        for i in range(start, min(start + 3000, len(html))):
            if html[i] == '{':
                depth += 1
            elif html[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        result["mpData"] = json.loads(html[start:i + 1])
                    except json.JSONDecodeError:
                        pass
                    break

    m = re.search(r',\s*Data\s*=\s*"([^"]+)"', html)
    if m:
        result["encoded_data"] = m.group(1)

    m = re.search(r'sign\s*:\s*"([a-f0-9]+)"', html)
    if m:
        result["sign"] = m.group(1)
    m = re.search(r'time\s*:\s*"(\d+)"', html)
    if m:
        result["time"] = m.group(1)

    return result


def _download_captcha_image(session: requests.Session, url: str):
    """下载验证码图片"""
    if url.startswith("/"):
        url = MOBILE_BASE + url
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return cv2.imdecode(np.frombuffer(r.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)


def _reconstruct_image(bg_img, slice_data):
    """用切片数据重建打乱的验证码背景图"""
    bg_h, bg_w = bg_img.shape[:2]
    n_cols = len(slice_data[0])
    slice_w = bg_w // n_cols
    slice_h = bg_h // len(slice_data)
    output = np.zeros_like(bg_img)

    for row_idx, row_slices in enumerate(slice_data):
        for col_idx, (src_x, src_y) in enumerate(row_slices):
            sx, sy = int(src_x), int(src_y)
            dy, dx = row_idx * slice_h, col_idx * slice_w
            h = min(slice_h, bg_h - sy, bg_h - dy)
            w = min(slice_w, bg_w - sx, bg_w - dx)
            if h > 0 and w > 0:
                output[dy:dy + h, dx:dx + w] = bg_img[sy:sy + h, sx:sx + w]

    return output


def _find_puzzle_position(bg_img, piece_img) -> int:
    """使用边缘检测 + 模板匹配找到滑块 X 坐标"""
    bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
    pc_gray = (cv2.cvtColor(piece_img, cv2.COLOR_BGR2GRAY)
               if len(piece_img.shape) == 3 else piece_img)
    bg_edges = cv2.Canny(bg_gray, 100, 200)
    pc_edges = cv2.Canny(pc_gray, 100, 200)
    result = cv2.matchTemplate(bg_edges, pc_edges, cv2.TM_CCOEFF_NORMED)
    _, _, _, max_loc = cv2.minMaxLoc(result)
    return max_loc[0]


def _build_captcha_session(proxy: Optional[str] = None) -> requests.Session:
    """
    创建模拟 Chrome 浏览器的 Session (包含 Sec-Fetch-* 反爬头)

    huanting.cc 从 2025 年下半年起在播放页检查 Sec-Fetch-* 头,
    缺少这些头的请求会被 301 重定向到 m.baidu.com。
    """
    session = build_session(user_agent=random_ua(), referer=BASE + "/", proxy=proxy)
    session.verify = False  # 全局禁用 SSL 验证 (避免代理 SSL 问题)
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        # 不要 br (Brotli), requests 默认不支持解压
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    })
    return session


def _derive_book_url(play_url: str) -> str:
    """从播放页 URL 推导书籍页 URL
    /ting/2274-151.html → /book/2274.html
    """
    m = re.search(r'/ting/(\d+)-\d+\.html', play_url)
    if m:
        return f"{BASE}/book/{m.group(1)}.html"
    return BASE + "/"


def _solve_single_attempt(play_url: str, proxy: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    单次验证码解算尝试 (线程安全, 不操作 Clash)

    用于并行尝试: 多个线程各自用当前代理尝试一次。
    返回 cookies 或 None。

    访问流程 (模拟真实浏览器):
    1. 先访问书籍页建立 session cookies (PHPSESSID / PHPATSSD)
    2. 再访问播放页 (带 Sec-Fetch-* 头防重定向到百度)
    """
    tid = threading.current_thread().name[-2:]  # 线程简称
    session = _build_captcha_session(proxy=proxy)

    # Step 1: 先访问书籍页建立 session (带重试)
    book_url = _derive_book_url(play_url)
    book_ok = False
    for retry in range(3):
        try:
            r = session.get(book_url, timeout=15)
            if r.status_code == 200:
                book_ok = True
                break
        except Exception:
            time.sleep(1)

    if not book_ok:
        # 尝试首页建立 session
        try:
            session.get(BASE + "/", timeout=10)
        except Exception:
            pass

    # Step 2: 访问播放页 (带完整头 + 不跟随重定向)
    session.headers["Referer"] = book_url if book_ok else BASE + "/"
    try:
        resp = session.get(play_url, timeout=20, allow_redirects=False)
    except Exception as e:
        print(f"    [{tid}] 页面获取失败: {e}")
        return None

    # 检测 301 重定向到百度
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location", "")
        if "baidu" in location:
            print(f"    [{tid}] 被 {resp.status_code} 重定向到百度"
                  f" (session={book_ok}, cookies={list(session.cookies.keys())})")
        else:
            print(f"    [{tid}] 被重定向到: {location}")
        return None

    resp.encoding = "utf-8"
    html = resp.text

    # 二次检查: 页面内容是否为百度
    if "百度" in html[:2000]:
        print(f"    [{tid}] 页面内容是百度")
        return None

    # 限流
    if "频繁" in html or "受限" in html:
        print(f"    [{tid}] 被限流")
        return None

    # 已有播放器
    if "PTingJplayer" in html and "bg_pic" not in html:
        print(f"    [{tid}] 无需验证码, 直接获得 session")
        return dict(session.cookies)

    # 提取 Data
    data_m = re.search(r'Data\s*=\s*\{', html)
    if not data_m:
        # 详细日志: 输出页面特征帮助诊断
        page_len = len(html)
        has_title = bool(re.search(r'<title>(.*?)</title>', html))
        has_script = '<script' in html
        print(f"    [{tid}] 未找到 Data 变量 "
              f"(len={page_len}, title={has_title}, script={has_script})")
        # 保存页面用于调试 (仅第一个线程)
        if tid.endswith("0") or tid.endswith("d"):
            try:
                import tempfile, os
                debug_path = os.path.join(
                    tempfile.gettempdir(), "huanting_debug_page.html"
                )
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"    [{tid}] 页面已保存到: {debug_path}")
            except Exception:
                pass
        return None

    start = data_m.end() - 1
    depth, raw_data = 0, ""
    for i in range(start, min(start + 3000, len(html))):
        if html[i] == '{':
            depth += 1
        elif html[i] == '}':
            depth -= 1
            if depth == 0:
                raw_data = html[start:i + 1]
                break

    try:
        data_obj = json.loads(raw_data.replace("\\/", "/"))
    except (json.JSONDecodeError, ValueError):
        return None

    bg_url = data_obj.get("bg_pic", "")
    ico_info = data_obj.get("ico_pic", {})
    ico_url = ico_info.get("url", "") if isinstance(ico_info, dict) else ""
    if bg_url.startswith("/"):
        bg_url = BASE + bg_url
    if ico_url.startswith("/"):
        ico_url = BASE + ico_url

    data2_m = re.search(r',\s*Data2\s*=\s*"([^"]+)"', html)
    encoded_data = data2_m.group(1) if data2_m else ""

    sign_m = re.search(r'sign\s*:\s*"([a-f0-9]{32})"', html)
    time_m = re.search(r'time\s*:\s*"(\d{10})"', html)
    if not sign_m or not time_m or not bg_url or not ico_url:
        return None

    sign, time_val = sign_m.group(1), time_m.group(1)

    try:
        bg_img = cv2.imdecode(
            np.frombuffer(session.get(bg_url, timeout=8, verify=False).content,
                          dtype=np.uint8),
            cv2.IMREAD_UNCHANGED,
        )
        ico_img = cv2.imdecode(
            np.frombuffer(session.get(ico_url, timeout=8, verify=False).content,
                          dtype=np.uint8),
            cv2.IMREAD_UNCHANGED,
        )
        if bg_img is None or ico_img is None:
            return None

        if encoded_data:
            reconstructed = _reconstruct_image(bg_img,
                                               _decode_data_string(encoded_data))
        else:
            reconstructed = bg_img

        x_pos = _find_puzzle_position(reconstructed, ico_img)
    except Exception:
        return None

    try:
        vr = session.post(
            play_url,
            data={"point": x_pos, "sign": sign, "time": time_val},
            timeout=10, verify=False,
        )
        res = vr.json()
        state = res.get("state")
        if state == 0:
            print(f"    [{tid}] 解算成功! x={x_pos}")
            return dict(session.cookies)
        else:
            print(f"    [{tid}] 验证失败 state={state} x={x_pos}")
    except Exception as e:
        print(f"    [{tid}] 提交失败: {e}")

    return None


def solve_desktop_captcha(play_url: str, proxy: Optional[str] = None,
                          clash_rotator: Optional[ClashRotator] = None,
                          max_retries: int = 10) -> Optional[Dict[str, str]]:
    """
    并行验证码解算 — 同时用多个节点尝试, 第一个成功即返回。

    策略:
    - 每轮: 切换到不同节点, 并行发起 3 个尝试
    - 任一成功 → 立即返回 cookies
    - 全部失败 → 换下一批节点, 继续
    - 比串行快 3 倍以上
    """
    if not _HAS_CV2:
        print("  [!] OpenCV 未安装, 无法解算验证码")
        return None

    play_url = play_url.replace("m.huanting.cc", "www.huanting.cc")

    PARALLEL = 3  # 并行尝试数
    rounds = (max_retries + PARALLEL - 1) // PARALLEL

    for rnd in range(rounds):
        attempt_base = rnd * PARALLEL + 1
        print(f"  [验证码] 并行尝试 {attempt_base}-"
              f"{min(attempt_base + PARALLEL - 1, max_retries)}/{max_retries} "
              f"({PARALLEL} 并发)...")

        # 先切换节点 (串行, 只切一次)
        if clash_rotator and rnd > 0:
            new_node = clash_rotator.rotate()
            if new_node:
                print(f"  [验证码] 切换到: {new_node}")
                time.sleep(1)

        # 并行解算
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            futures = [
                pool.submit(_solve_single_attempt, play_url, proxy)
                for _ in range(PARALLEL)
            ]
            try:
                for future in as_completed(futures, timeout=90):
                    try:
                        result = future.result(timeout=0)
                        if result:
                            # 成功! 取消其他任务
                            for f in futures:
                                f.cancel()
                            print(f"  [验证码] ✓ 并行解算成功! "
                                  f"(cookie={list(result.keys())})")
                            return result
                    except Exception:
                        pass
            except TimeoutError:
                print(f"  [验证码] 本轮超时, 继续下一轮...")
                for f in futures:
                    f.cancel()

        # 本轮全部失败, 换节点继续
        if clash_rotator:
            clash_rotator.rotate()
            time.sleep(2)

    return None


def solve_mobile_captcha(play_url: str, proxy: Optional[str] = None, max_retries: int = 6) -> Optional[Dict[str, str]]:
    """自动解算移动端滑块验证码"""
    if not _HAS_CV2:
        print("  [!] OpenCV 未安装, 无法自动解算验证码")
        return None

    play_url = play_url.replace("www.huanting.cc", "m.huanting.cc")
    if "m.huanting.cc" not in play_url:
        play_url = play_url.replace("huanting.cc", "m.huanting.cc")

    session = requests.Session()
    session.headers.update({"User-Agent": MOBILE_UA, "Referer": MOBILE_BASE + "/"})
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    for attempt in range(1, max_retries + 1):
        print(f"  [验证码] 尝试 {attempt}/{max_retries}...")

        try:
            r = session.get(play_url, timeout=15)
            r.encoding = "utf-8"
            html = r.text
        except Exception as e:
            print(f"  [验证码] 页面获取失败: {e}")
            time.sleep(2)
            continue

        if "mpData" not in html:
            if "jplayer" in html.lower() or "audio" in html.lower():
                return dict(session.cookies)
            time.sleep(2)
            continue

        data = _extract_captcha_data(html)
        mp = data.get("mpData", {})
        bg_url = mp.get("bg_pic", "")
        ico_data = mp.get("ico_pic", {})
        ico_url = ico_data.get("url", "") if isinstance(ico_data, dict) else ""
        sign = data.get("sign", "")
        time_val = data.get("time", "")
        encoded = data.get("encoded_data", "")

        if not all([bg_url, ico_url, sign, encoded]):
            time.sleep(2)
            continue

        try:
            slice_data = _decode_data_string(encoded)
            bg_img = _download_captcha_image(session, bg_url)
            piece_img = _download_captcha_image(session, ico_url)
            reconstructed = _reconstruct_image(bg_img, slice_data)
            x_pos = _find_puzzle_position(reconstructed, piece_img)
        except Exception as e:
            print(f"  [验证码] 图像处理失败: {e}")
            time.sleep(2)
            continue

        solved = False
        for offset in [0, -2, 2, -4, 4]:
            x_try = max(0, x_pos + offset)
            try:
                vr = session.post(
                    play_url,
                    data={"point": x_try, "sign": sign, "time": time_val},
                    timeout=15,
                )
                res = vr.json()
                if res.get("state") == 0:
                    solved = True
                    break
                elif res.get("state") in (4602, 4603):
                    break
            except Exception:
                pass

        if solved:
            return dict(session.cookies)
        time.sleep(2)

    return None
