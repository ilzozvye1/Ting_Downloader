#!/usr/bin/env python3
"""
huanting.cc (ting22.com) 有声小说下载适配器

特点:
- 纯 HTTP API 方式获取音频 URL (章节 1~150)
- 滑块验证码自动解算 + Playwright 音频提取 (章节 151+)
- API: /apiP2.php?id={book_id}&pid={chapter_seq}
- 音频: base64 编码的 xmcdn.com URL (m4a 格式)
"""

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Dict
from urllib.parse import urlparse

import requests
from lxml import html as lxml_html

# OpenCV 用于验证码解算 (可选, 缺少时无法自动过验证码)
try:
    import numpy as np
    import cv2
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False

# ── 常量 ─────────────────────────────────────────────────────

MOBILE_BASE = "https://m.huanting.cc"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)


# ──────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────

@dataclass
class HuantingChapter:
    index: int          # 章节序号 (从1开始)
    title: str          # 章节标题
    play_url: str       # 播放页 URL
    audio_url: str = "" # 音频下载 URL (填充后)


@dataclass
class HuantingBookInfo:
    book_id: str
    title: str
    author: str = ""
    announcer: str = ""
    cover_url: str = ""
    chapters: List[HuantingChapter] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# URL 识别
# ──────────────────────────────────────────────────────────────

HUANTING_DOMAINS = ["ting22.com", "huanting.cc"]

def is_huanting_url(url: str) -> bool:
    """判断 URL 是否属于 huanting.cc / ting22.com"""
    url_lower = url.lower()
    return any(domain in url_lower for domain in HUANTING_DOMAINS)


def detect_huanting_url_type(url: str) -> str:
    """
    识别 huanting URL 类型
    返回: 'book', 'play', 'unknown'
    """
    if not is_huanting_url(url):
        return "unknown"
    path = urlparse(url).path
    if "/book/" in path:
        return "book"
    if "/ting/" in path:
        return "play"
    return "unknown"


def extract_book_id(url: str) -> Optional[str]:
    """从书籍 URL 提取 book_id"""
    # /book/2274.html -> 2274
    m = re.search(r"/book/(\d+)", url)
    return m.group(1) if m else None


# ──────────────────────────────────────────────────────────────
# HTTP 会话
# ──────────────────────────────────────────────────────────────

_proxy: Optional[str] = None


def set_proxy(proxy: Optional[str]):
    global _proxy
    _proxy = proxy


def get_proxy() -> Optional[str]:
    return _proxy


def _build_session() -> requests.Session:
    """构建 requests 会话"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.huanting.cc/",
    })
    if _proxy:
        session.proxies = {"http": _proxy, "https": _proxy}
    return session


# ──────────────────────────────────────────────────────────────
# 书籍解析
# ──────────────────────────────────────────────────────────────

def parse_book_page(url: str) -> HuantingBookInfo:
    """
    解析书籍页面, 获取书籍信息和章节列表

    支持的 URL 格式:
    - https://www.huanting.cc/book/2274.html
    - https://www.ting22.com/book/2274.html
    """
    print(f"[*] 正在解析书籍页面: {url}")

    # 规范化 URL 到 huanting.cc
    url = url.replace("ting22.com", "huanting.cc")
    if not url.startswith("http"):
        url = "https://" + url
    if "www." not in url:
        url = url.replace("://huanting.cc", "://www.huanting.cc")

    book_id = extract_book_id(url)
    if not book_id:
        raise ValueError(f"无法从 URL 提取书籍 ID: {url}")

    session = _build_session()

    # 获取第一页
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    tree = lxml_html.fromstring(resp.text)

    # 提取书籍信息
    title = ""
    h1 = tree.xpath("//h1/text()")
    if h1:
        title = h1[0].strip().replace("有声小说", "").strip()
    if not title:
        title = f"book_{book_id}"
    print(f"  书名: {title}")

    author = ""
    author_el = tree.xpath('//span[@class="author"]/text()')
    if author_el:
        author = author_el[0].replace("作者：", "").strip()
        print(f"  作者: {author}")

    announcer = ""
    ann_el = tree.xpath('//span[@class="announcer"]/text()')
    if ann_el:
        announcer = ann_el[0].replace("主播：", "").strip()
        print(f"  播音: {announcer}")

    cover_url = ""
    img = tree.xpath('//div[@class="img"]/img/@src')
    if img:
        cover_url = img[0]

    # 提取章节列表 - 从所有分页
    all_chapters = []

    # 解析当前页的章节
    def parse_chapter_list(page_tree):
        chapters = []
        links = page_tree.xpath('//ul[@id="vlink"]/li/a')
        for a in links:
            ch_title = a.text_content().strip()
            ch_href = a.get("href", "")
            if ch_href and ch_title:
                if not ch_href.startswith("http"):
                    ch_href = f"https://www.huanting.cc{ch_href}"
                chapters.append((ch_title, ch_href))
        return chapters

    # 获取分页信息
    page_links = tree.xpath('//div[@class="play_navs"]/a')
    total_pages = len(page_links) if page_links else 1
    print(f"  共 {total_pages} 页章节")

    # 第一页
    page1_chapters = parse_chapter_list(tree)
    all_chapters.extend(page1_chapters)
    print(f"    第 1/{total_pages} 页: {len(page1_chapters)} 个章节")

    # 后续分页
    for p in range(2, total_pages + 1):
        page_url = f"https://www.huanting.cc/book/{book_id}.html?p={p}"
        try:
            time.sleep(0.3)  # 请求间隔
            resp = session.get(page_url, timeout=15)
            resp.encoding = "utf-8"
            page_tree = lxml_html.fromstring(resp.text)
            page_chapters = parse_chapter_list(page_tree)
            all_chapters.extend(page_chapters)
            print(f"    第 {p}/{total_pages} 页: {len(page_chapters)} 个章节")
        except Exception as e:
            print(f"    第 {p}/{total_pages} 页获取失败: {e}")

    # 构建 Chapter 对象
    chapters = []
    for i, (ch_title, ch_href) in enumerate(all_chapters, 1):
        chapters.append(HuantingChapter(
            index=i,
            title=ch_title,
            play_url=ch_href,
        ))

    print(f"  共 {len(chapters)} 个章节")

    return HuantingBookInfo(
        book_id=book_id,
        title=title,
        author=author,
        announcer=announcer,
        cover_url=cover_url,
        chapters=chapters,
    )


# ──────────────────────────────────────────────────────────────
# 音频 URL 提取
# ──────────────────────────────────────────────────────────────

def get_audio_url(book_id: str, chapter_pid: int) -> Optional[str]:
    """
    通过 API 获取音频 URL

    Args:
        book_id: 书籍 ID (如 "2274")
        chapter_pid: 章节序号 (从 1 开始)

    Returns:
        音频下载 URL, 失败返回 None
        返回 "RATE_LIMITED" 表示被限流
    """
    session = _build_session()
    api_url = f"https://www.huanting.cc/apiP2.php?id={book_id}&pid={chapter_pid}"

    try:
        resp = session.get(api_url, timeout=15)
        resp.raise_for_status()

        # 检查是否返回了限流页面 (HTML 而非 JSON)
        content_type = resp.headers.get("content-type", "")
        body_text = resp.text.strip()

        if "html" in content_type or body_text.startswith("<") or "频繁" in body_text or "受限" in body_text:
            print(f"  [!] 被限流! 服务器返回了限流页面")
            return "RATE_LIMITED"

        data = resp.json()

        if data.get("status") != 1 or data.get("state") != "success":
            print(f"  [!] API 返回非成功状态: {data.get('state', 'unknown')}")
            return None

        playlist = data.get("playlist", {})

        # 优先: src 字段 (base64 编码的直链)
        src_b64 = playlist.get("src", "")
        if src_b64:
            try:
                audio_url = base64.b64decode(src_b64).decode("utf-8")
                if audio_url.startswith("http"):
                    return audio_url
            except Exception:
                pass

        # 备选: 从 file 字段提取 (如果 src 不可用)
        # file 字段使用自定义编码, 需要 JS 解码, 暂不支持

        return None

    except requests.RequestException as e:
        print(f"  [!] API 请求失败: {e}")
        return None
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [!] API 响应解析失败: {e}")
        return None


# 缓存: {book_id: free_limit}  已检测过的书免费章节上限
_FREE_LIMIT_CACHE: Dict[str, int] = {}

FREE_CHAPTER_LIMIT = 150          # huanting.cc 全站默认免费章节上限


def detect_free_chapter_limit(book_id: str) -> int:
    """
    检测某本书的免费章节上限.

    用二分思路快速确认边界:
      - 测试 pid=150 (应成功) 和 pid=151 (应失败)
      - 如果 pid=150 也失败, 说明可能被限流, 返回 0
      - 如果 pid=151 也成功, 尝试更高 pid (200, 300, …) 直到失败

    Returns:
        可免费获取的最大章节序号. 0 = 无法判断 (网络/限流问题).
    """
    if book_id in _FREE_LIMIT_CACHE:
        return _FREE_LIMIT_CACHE[book_id]

    # 测试 pid=150
    r150 = get_audio_url(book_id, 150)
    if r150 is None or r150 == "RATE_LIMITED":
        # 可能被限流或网络问题, 不做结论
        return 0

    # pid=150 可用, 测试 pid=151
    r151 = get_audio_url(book_id, 151)
    if r151 is None:
        # 经典 150 章限制
        _FREE_LIMIT_CACHE[book_id] = 150
        return 150
    elif r151 == "RATE_LIMITED":
        return 0  # 被限流, 不做结论

    # pid=151 也可用? 罕见, 向上探测
    limit = 151
    for test_pid in [200, 300, 500, 1000]:
        r = get_audio_url(book_id, test_pid)
        if r is None:
            # 边界在 (limit, test_pid] 之间, 粗略取 test_pid - 1
            limit = test_pid - 1
            break
        elif r == "RATE_LIMITED":
            break
        else:
            limit = test_pid

    _FREE_LIMIT_CACHE[book_id] = limit
    return limit


# ──────────────────────────────────────────────────────────────
# 文件名处理
# ──────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """清理文件名"""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    return name or "untitled"


# ──────────────────────────────────────────────────────────────
# 下载
# ──────────────────────────────────────────────────────────────

def download_file(
    url: str,
    filepath: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> bool:
    """
    下载音频文件

    Args:
        url: 音频 URL
        filepath: 保存路径
        progress_callback: 进度回调 (downloaded_bytes, total_bytes)

    Returns:
        是否成功
    """
    session = _build_session()
    try:
        resp = session.get(url, stream=True, timeout=30)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))

        tmp_path = filepath + ".tmp"
        downloaded = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total:
                        progress_callback(downloaded, total)

        # 验证文件大小
        if total and downloaded < total * 0.95:
            print(f"  [!] 文件不完整: {downloaded}/{total} bytes")
            os.remove(tmp_path)
            return False

        os.replace(tmp_path, filepath)
        return True

    except Exception as e:
        print(f"  [!] 下载失败: {e}")
        tmp_path = filepath + ".tmp"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def download_cover(cover_url: str, save_dir: str) -> Optional[str]:
    """下载封面图片"""
    if not cover_url:
        return None
    session = _build_session()
    ext = os.path.splitext(urlparse(cover_url).path)[1] or ".jpg"
    filepath = os.path.join(save_dir, f"cover{ext}")
    if os.path.exists(filepath):
        return filepath
    try:
        resp = session.get(cover_url, timeout=15)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
        return filepath
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────
# 主下载流程 (CLI)
# ──────────────────────────────────────────────────────────────

def download_book(
    url: str,
    output_dir: str = ".",
    start: int = 1,
    end: Optional[int] = None,
):
    """
    CLI 模式下载整本书

    Args:
        url: 书籍 URL
        output_dir: 输出目录
        start: 起始章节
        end: 结束章节 (None=全部)
    """
    info = parse_book_page(url)
    chapters = info.chapters

    if not chapters:
        print("[FAIL] 未找到章节")
        return

    # 范围裁剪
    chapters = chapters[max(0, start - 1): end]

    # 创建目录
    book_dir = os.path.join(output_dir, sanitize_filename(info.title))
    os.makedirs(book_dir, exist_ok=True)
    print(f"\n[*] 输出目录: {os.path.abspath(book_dir)}")

    # 下载封面
    if info.cover_url:
        cover = download_cover(info.cover_url, book_dir)
        if cover:
            print(f"[*] 封面已保存")

    # 扫描已下载的章节
    try:
        existing_files = os.listdir(book_dir)
    except OSError:
        existing_files = []

    downloaded_set = set()
    for f in existing_files:
        parts = f.split("_", 1)
        if parts[0].isdigit():
            downloaded_set.add(int(parts[0]))

    # 过滤已下载的
    todo = [ch for ch in chapters if ch.index not in downloaded_set]
    skipped = len(chapters) - len(todo)
    if skipped:
        print(f"[*] 跳过已下载: {skipped} 集")

    if not todo:
        print("[*] 所有章节均已下载")
        return

    print(f"[*] 待下载: {len(todo)} 集\n")

    success = 0
    fail = 0

    for i, chapter in enumerate(todo):
        print(f"[{i + 1}/{len(todo)}] {chapter.title}")

        # 获取音频 URL (无限重试)
        audio_url = None
        attempt = 0
        while not audio_url:
            attempt += 1
            audio_url = get_audio_url(info.book_id, chapter.index)
            if audio_url:
                break

            wait = min(10 * (3 ** (attempt - 1)), 60)
            print(f"  [!] 第{attempt}次失败, 等待 {wait}s...")
            time.sleep(wait)

        # 确定文件扩展名
        ext = ".m4a"
        if ".mp3" in audio_url:
            ext = ".mp3"

        filename = f"{chapter.index:04d}_{sanitize_filename(chapter.title)}{ext}"
        filepath = os.path.join(book_dir, filename)

        # 下载 (无限重试)
        dl_ok = False
        attempt = 0
        while not dl_ok:
            attempt += 1
            dl_ok = download_file(audio_url, filepath)
            if dl_ok:
                break

            wait = min(10 * attempt, 60)
            print(f"  [!] 下载第{attempt}次失败, 等待 {wait}s...")
            if os.path.exists(filepath + ".tmp"):
                os.remove(filepath + ".tmp")
            time.sleep(wait)

        fsize = os.path.getsize(filepath) / 1024
        print(f"  [OK] {filename} ({fsize:.0f} KB)")
        success += 1

        # 下载间隔
        time.sleep(1 + (i % 5 == 0) * 2)

    print(f"\n[DONE] 成功: {success}, 失败: {fail}")


# ──────────────────────────────────────────────────────────────
# 滑块验证码自动解算 (151+ 章节)
# ──────────────────────────────────────────────────────────────

def _decode_data_string(encoded: str) -> list:
    """解码验证码切片数据 (Caesar 密码: charCode - 3)"""
    encoded = encoded.replace("\\/", "/")
    return json.loads("".join(chr(ord(c) - 3) for c in encoded))


def _extract_captcha_data(html: str) -> dict:
    """从移动端播放页 HTML 提取 mpData, Data, sign, time"""
    result: Dict = {}

    # mpData (嵌套 JSON — 需要平衡大括号匹配)
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

    # 编码的切片数据
    m = re.search(r',\s*Data\s*=\s*"([^"]+)"', html)
    if m:
        result["encoded_data"] = m.group(1)

    # sign, time
    m = re.search(r'sign\s*:\s*"([a-f0-9]+)"', html)
    if m:
        result["sign"] = m.group(1)
    m = re.search(r'time\s*:\s*"(\d+)"', html)
    if m:
        result["time"] = m.group(1)

    return result


def _download_captcha_image(session: requests.Session, url: str):
    """下载验证码图片并解码为 cv2 格式"""
    if url.startswith("/"):
        url = MOBILE_BASE + url
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return cv2.imdecode(np.frombuffer(r.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)


def _reconstruct_image(bg_img, slice_data):
    """用切片数据重建被打乱的验证码背景图"""
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
    """使用边缘检测 + 模板匹配找到滑块拼图的 X 坐标"""
    bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
    pc_gray = (
        cv2.cvtColor(piece_img, cv2.COLOR_BGR2GRAY)
        if len(piece_img.shape) == 3
        else piece_img
    )
    bg_edges = cv2.Canny(bg_gray, 100, 200)
    pc_edges = cv2.Canny(pc_gray, 100, 200)
    result = cv2.matchTemplate(bg_edges, pc_edges, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    return max_loc[0]


def solve_mobile_captcha(
    play_url: str,
    proxy: Optional[str] = None,
    max_retries: int = 6,
) -> Optional[Dict[str, str]]:
    """
    自动解算移动端滑块验证码

    Args:
        play_url: 播放页 URL (会自动转为移动端)
        proxy: 代理地址
        max_retries: 最大重试次数

    Returns:
        成功时返回 session cookies dict, 失败返回 None
    """
    if not _HAS_CV2:
        print("  [!] OpenCV 未安装, 无法自动解算验证码")
        print("  [!] 请运行: pip install opencv-python-headless numpy")
        return None

    # 确保使用移动端 URL
    play_url = play_url.replace("www.huanting.cc", "m.huanting.cc")
    if "m.huanting.cc" not in play_url:
        play_url = play_url.replace("huanting.cc", "m.huanting.cc")

    session = requests.Session()
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Referer": MOBILE_BASE + "/",
    })
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

        # 无验证码 = 直接通过
        if "mpData" not in html:
            if "jplayer" in html.lower() or "audio" in html.lower():
                print("  [验证码] 无需验证码, 页面正常")
                return dict(session.cookies)
            print("  [验证码] 页面异常, 无验证码数据")
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
            print("  [验证码] 缺少必要数据")
            time.sleep(2)
            continue

        # 解码切片 → 重建图像 → 计算位置
        try:
            slice_data = _decode_data_string(encoded)
            bg_img = _download_captcha_image(session, bg_url)
            piece_img = _download_captcha_image(session, ico_url)
            reconstructed = _reconstruct_image(bg_img, slice_data)
            x_pos = _find_puzzle_position(reconstructed, piece_img)
            print(f"  [验证码] 计算滑块位置: x={x_pos}")
        except Exception as e:
            print(f"  [验证码] 图像处理失败: {e}")
            time.sleep(2)
            continue

        # 提交验证 (带小偏移容错)
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
                state = res.get("state")
                if state == 0:
                    print(f"  [验证码] 验证成功! point={x_try}")
                    solved = True
                    break
                elif state in (4602, 4603):
                    # 会话过期或尝试过多, 需要重新获取
                    break
            except Exception:
                pass

        if solved:
            return dict(session.cookies)

        time.sleep(2)

    print("  [验证码] 多次尝试后仍未通过")
    return None


def needs_captcha(book_id: str, chapter_pid: int) -> bool:
    """
    快速检测某章节是否需要验证码 (API 是否返回 fail)
    """
    session = _build_session()
    api_url = f"https://www.huanting.cc/apiP2.php?id={book_id}&pid={chapter_pid}"
    try:
        resp = session.get(api_url, timeout=10)
        data = resp.json()
        return data.get("state") != "success"
    except Exception:
        return True


# ──────────────────────────────────────────────────────────────
# CLI 入口
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python site_huanting.py <书籍URL> [输出目录] [起始集] [结束集]")
        print("例:   python site_huanting.py https://www.huanting.cc/book/2274.html ./output 1 10")
        sys.exit(1)

    book_url = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "."
    s = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    e = int(sys.argv[4]) if len(sys.argv) > 4 else None

    download_book(book_url, out_dir, s, e)
