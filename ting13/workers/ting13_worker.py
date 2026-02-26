"""
子进程工作函数模块 — 解析 / 下载
独立模块以解决 PyInstaller frozen 环境下 multiprocessing spawn 找不到 __main__ 属性的问题

换IP策略：子进程不直接调用 ClashRotator.rotate()，
而是通过队列发送 ("rotate_request", reason) 给主进程，
由主进程集中管理换IP决策（全局冷却 + 防冲突）。
"""

import multiprocessing
import os
import sys
import json
import time
import random
import traceback
from typing import Optional, Dict, Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from ting13.legacy.ting13_downloader import (
    _is_frozen,
    _get_bundled_base,
    Chapter,
    BookInfo,
    fetch_page,
    parse_book_page,
    extract_audio_url,
    extract_audio_url_fast,
    sanitize_filename,
    _build_session,
    load_cookies,
    set_proxy,
    get_proxy,
    detect_url_type,
    MIN_VALID_FILE_SIZE,
)
from playwright.sync_api import sync_playwright

from ting13.core.download import DownloadEngine, DownloadCallbacks
from ting13.core.network import set_proxy as core_set_proxy
from ting13.sources.huanting import HuantingSource


class _MsgSender:
    """在子进程中发送消息到 mp Queue"""
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
    """子进程中替换 sys.stdout，将 print 输出发送到队列"""
    def __init__(self, q: multiprocessing.Queue):
        self._q = q
        self.encoding = "utf-8"
    def write(self, text: str):
        if text and text.strip():
            self._q.put(("log", text.rstrip("\n")))
    def flush(self):
        pass


def worker_parse(msg_q: multiprocessing.Queue,
                 stop_evt: multiprocessing.Event,
                 url: str, site: str, proxy: str):
    """子进程：解析书籍"""
    sys.stdout = _PrintToQueue(msg_q)
    sys.stderr = _PrintToQueue(msg_q)
    s = _MsgSender(msg_q)
    try:
        set_proxy(proxy if proxy else None)
        if site == "huanting":
            core_set_proxy(proxy if proxy else None)
            ht_source = HuantingSource()
            ht_book = ht_source.parse_book(url)
            total = len(ht_book.chapters)
            s.info(f"书名: {ht_book.title}  作者: {ht_book.author}  章节: {total}")
            s.status(f"解析完成 - 共 {total} 章")
            s.progress(1, "完成")
            s.log(f"[OK] 解析完成: {ht_book.title} ({total} 章)")
            s.rename(ht_book.title[:10])
            book_data = {
                "title": ht_book.title,
                "author": getattr(ht_book, "author", ""),
                "chapters": [
                    {"index": ch.index, "title": ch.title,
                     "play_url": getattr(ch, "play_url", getattr(ch, "url", "")),
                     "audio_url": getattr(ch, "audio_url", ""),
                     }
                    for ch in ht_book.chapters
                ],
                "extra": ht_book.extra if hasattr(ht_book, "extra") else {},
            }
            s.result("huanting_book", book_data)
        else:
            info = parse_book_page(url)
            total = len(info.chapters)
            s.info(f"书名: {info.title}  作者: {info.author}  章节: {total}")
            s.status(f"解析完成 - 共 {total} 章")
            s.progress(1, "完成")
            s.log(f"[OK] 解析完成: {info.title} ({total} 章)")
            s.rename(info.title[:10])
            book_data = {
                "title": info.title,
                "author": info.author,
                "chapters": [
                    {"index": ch.index, "title": ch.title,
                     "play_url": ch.play_url,
                     "audio_url": getattr(ch, "audio_url", ""),
                     }
                    for ch in info.chapters
                ],
            }
            s.result("ting13_book", book_data)
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
                    book_data: Optional[dict]):
    """子进程：下载书籍"""
    sys.stdout = _PrintToQueue(msg_q)
    sys.stderr = _PrintToQueue(msg_q)
    s = _MsgSender(msg_q)
    set_proxy(proxy if proxy else None)

    try:
        if site == "huanting":
            _download_huanting(s, stop_evt, url, output_dir, start, end,
                               proxy, rotate_interval, book_data)
        else:
            _download_ting13(s, stop_evt, url, url_type, output_dir,
                             start, end, headless, proxy,
                             rotate_enabled, rotate_interval, book_data)
    except Exception as e:
        s.log(f"[FAIL] 下载出错: {e}")
        s.log(traceback.format_exc())
        s.status(f"出错: {e}")
    finally:
        s.buttons(False)


def _download_huanting(s, stop_evt, url, output_dir, start, end,
                       proxy, rotate_interval, book_data):
    core_set_proxy(proxy if proxy else None)

    source = HuantingSource()

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
        clash_rotator=None,
        rotate_interval=rotate_interval,
    )

    if book_data:
        s.log("[*] 解析书籍信息...")
        s.status("解析中...")
    book = source.parse_book(url)
    book.extra["url"] = url
    s.info(f"{book.title}  共 {len(book.chapters)} 章")
    s.rename(book.title[:10])
    engine.run(book, output_dir, start, end)


def _download_ting13(s, stop_evt, url, url_type, output_dir, start, end,
                     headless, proxy, rotate_enabled, rotate_interval, book_data):

    if url_type == "play":
        chapters = [Chapter(index=1, title="单集", play_url=url)]
        book_title = "ting13_audio"
    else:
        if book_data:
            book_title = book_data["title"]
            chapters = [
                Chapter(index=ch["index"], title=ch["title"], play_url=ch["play_url"])
                for ch in book_data["chapters"]
            ]
            s.log(f"[*] 使用已解析数据: {book_title} ({len(chapters)} 章)")
        else:
            s.log("[*] 尚未解析，先自动解析书籍信息...")
            s.status("自动解析中...")
            info = parse_book_page(url)
            chapters = info.chapters
            book_title = info.title

        s.rename(book_title[:10])

        if not chapters:
            s.log("[FAIL] 未找到任何章节")
            s.status("未找到章节")
            return

        start_idx = max(0, start - 1)
        end_idx = end if end else len(chapters)
        chapters = chapters[start_idx:end_idx]

        s.info(
            f"书名: {book_title}  下载: 第{start}集 ~ 第{start + len(chapters) - 1}集 (共{len(chapters)}集)"
        )

    book_dir = os.path.join(output_dir, sanitize_filename(book_title))
    os.makedirs(book_dir, exist_ok=True)
    s.log(f"[*] 输出目录: {os.path.abspath(book_dir)}")

    # ── 扫描缺失集数 ──
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
        continuations = [ch for ch in missing_chapters if ch.index > max_dl or ch.index < min_dl]
        if gaps:
            gap_nums = [str(ch.index) for ch in gaps]
            s.log(f"[*] 检测到 {len(gaps)} 个缺失集: {', '.join(gap_nums[:20])}"
                  + ("... 等" if len(gaps) > 20 else ""))
            chapters = gaps + continuations
        else:
            chapters = missing_chapters
    elif missing_chapters:
        chapters = missing_chapters
    else:
        s.log("[*] 所有章节均已下载")
        s.status("全部已下载")
        return

    total = len(chapters)
    success = 0
    fail = 0
    skipped = 0
    consecutive_ok = 0
    consecutive_fail = 0
    api_mode_ok = 0
    pw_mode_ok = 0
    s.log(f"[*] 待下载: {total} 集")
    s.log("[*] 优先使用 API 快速模式（~0.3s/集），失败自动回退浏览器模式")

    # 复用同一个 Session 以利用 HTTP keep-alive
    shared_session = _build_session()

    pw_manager = None
    browser = None
    context = None
    page = None

    _AD_BLOCK_PATTERNS = [
        "*googlesyndication.com*", "*doubleclick.net*",
        "*adtrafficquality.google*", "*google-analytics.com*",
        "*hm.baidu.com*", "*zz.bdstatic.com*", "*sp0.baidu.com*",
        "*sdk.51.la*", "*collect*.51.la*", "*cnzz.com*",
        "*bytegoofy.com*goofy/ttzz*", "*zhanzhang.toutiao.com*",
        "*api.pv.iiisss.top*", "*nfksjkfs.com*", "*ymmiyun.com*",
        "*spl.ztvx8.com*", "*adbyunion*",
    ]

    def _ensure_pw():
        nonlocal pw_manager, browser, context, page
        if page is not None:
            return page
        s.log("[*] API 模式不可用，启动浏览器回退模式...")
        s.status("启动浏览器...")
        pw_manager = sync_playwright().start()
        launch_kwargs: Dict = {"headless": headless}
        if _is_frozen():
            base = _get_bundled_base()
            chrome_exe = os.path.join(
                base, "ms-playwright", "chromium-1208", "chrome-win64", "chrome.exe"
            )
            if os.path.isfile(chrome_exe):
                launch_kwargs["executable_path"] = chrome_exe
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = pw_manager.chromium.launch(**launch_kwargs)
        stored_cookies = load_cookies()
        if stored_cookies:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36",
            )
            context.add_cookies(stored_cookies)
        else:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                           "Version/16.0 Mobile/15E148 Safari/604.1",
                viewport={"width": 375, "height": 812},
            )
        for pat in _AD_BLOCK_PATTERNS:
            context.route(pat, lambda route, _: route.abort())
        page = context.new_page()
        s.log("[*] 浏览器就绪")
        return page

    def _dl_file(audio_url, filepath, chapter_idx, total_ch):
        temp_path = filepath + ".part"
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.ting13.cc/",
            }
            dl_url = audio_url
            if dl_url.startswith("//"):
                dl_url = "https:" + dl_url
            resp = shared_session.get(dl_url, headers=headers, stream=True, timeout=60, verify=False)
            resp.raise_for_status()
            content_length = int(resp.headers.get("content-length", 0))

            if 0 < content_length < MIN_VALID_FILE_SIZE:
                s.log(f"  [!] 服务器返回文件过小 ({content_length} bytes)，跳过")
                return False

            downloaded = 0
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            with open(temp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if stop_evt.is_set():
                        try:
                            f.close()
                            os.remove(temp_path)
                        except OSError:
                            pass
                        return False
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if content_length > 0:
                            file_pct = downloaded / content_length
                            overall = (chapter_idx + file_pct) / total_ch
                            s.progress(overall,
                                       f"{chapter_idx+1}/{total_ch} ({file_pct*100:.0f}%)")

            if downloaded < MIN_VALID_FILE_SIZE:
                s.log(f"  [!] 下载文件过小 ({downloaded} bytes)，不是有效音频")
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                return False

            os.replace(temp_path, filepath)
            return True
        except Exception as e:
            s.log(f"  [FAIL] 下载失败: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return False

    try:
        for i, chapter in enumerate(chapters):
            if stop_evt.is_set():
                s.log("[!] 用户请求停止")
                break

            progress = i / total
            s.progress(progress, f"{i}/{total}")
            s.status(f"下载中 [{i+1}/{total}]  {chapter.title}")
            s.log(f"\n[{i+1}/{total}] {chapter.title}")

            safe_title = sanitize_filename(chapter.title)
            try:
                existing = [f for f in os.listdir(book_dir) if f.startswith(f"{chapter.index:04d}_")]
            except OSError:
                existing = []

            if existing:
                valid_existing = None
                for ex in existing:
                    ex_path = os.path.join(book_dir, ex)
                    try:
                        if os.path.isfile(ex_path) and os.path.getsize(ex_path) >= MIN_VALID_FILE_SIZE:
                            valid_existing = ex
                            break
                    except OSError:
                        pass
                if valid_existing:
                    s.log(f"  [SKIP] 已存在: {valid_existing}")
                    success += 1
                    skipped += 1
                    continue
                # 存在同序号但无效的小文件，删除后重下
                for ex in existing:
                    ex_path = os.path.join(book_dir, ex)
                    try:
                        if os.path.isfile(ex_path):
                            os.remove(ex_path)
                            s.log(f"  [*] 删除无效旧文件: {ex}")
                    except OSError:
                        pass

            s.log("  [>] 提取音频 URL...")
            audio_url = None

            t0 = time.time()
            try:
                audio_url = extract_audio_url_fast(chapter.play_url, session=shared_session)
            except Exception:
                pass

            if audio_url:
                api_mode_ok += 1
                consecutive_fail = 0
                elapsed = time.time() - t0
                s.log(f"  [FAST] API 模式成功 ({elapsed:.1f}s)")
            else:
                pw_page = _ensure_pw()
                attempt = 0
                while not audio_url and not stop_evt.is_set():
                    attempt += 1
                    if attempt > 1:
                        s.log(f"  [>] 第{attempt}次尝试 (浏览器模式)...")
                    try:
                        audio_url = extract_audio_url(pw_page, chapter.play_url, timeout=30)
                    except Exception as e:
                        s.log(f"  [!] 提取出错: {e}")

                    if audio_url:
                        pw_mode_ok += 1
                        consecutive_fail = 0
                        break

                    consecutive_fail += 1

                    # 连续失败达到阈值 → 向主进程请求换IP
                    if (rotate_enabled and rotate_interval > 0
                            and consecutive_fail >= rotate_interval):
                        s.request_rotate(f"连续失败 {consecutive_fail} 次")
                        s.log(f"  [*] 已请求换IP (连续失败 {consecutive_fail} 次)")
                        consecutive_fail = 0
                        time.sleep(3)

                    if attempt <= 3:
                        wait = 10 * (2 ** (attempt - 1))
                        wait = min(wait, 60)
                    else:
                        wait = 60

                    s.log(f"  [!] 第{attempt}次失败，等待 {wait}s 后重试...")
                    for _ in range(wait):
                        if stop_evt.is_set():
                            break
                        time.sleep(1)
                    if stop_evt.is_set():
                        break

                    try:
                        pw_page.close()
                    except Exception:
                        pass
                    try:
                        pw_page = context.new_page()
                        page = pw_page
                        s.log("  [*] 已重建浏览器页面")
                    except Exception as e:
                        s.log(f"  [!] 重建页面失败: {e}")
                        try:
                            context.close()
                        except Exception:
                            pass
                        try:
                            stored_cookies = load_cookies()
                            if stored_cookies:
                                context = browser.new_context(
                                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                                               "Chrome/120.0.0.0 Safari/537.36",
                                )
                                context.add_cookies(stored_cookies)
                            else:
                                context = browser.new_context(
                                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                                               "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                                               "Version/16.0 Mobile/15E148 Safari/604.1",
                                    viewport={"width": 375, "height": 812},
                                )
                            for pat in _AD_BLOCK_PATTERNS:
                                context.route(pat, lambda route, _: route.abort())
                            pw_page = context.new_page()
                            page = pw_page
                            s.log("  [*] 已重建浏览器上下文和页面")
                        except Exception as e2:
                            s.log(f"  [FAIL] 重建上下文失败: {e2}")
                            break

            if stop_evt.is_set():
                break

            if not audio_url:
                fail += 1
                consecutive_ok = 0
                consecutive_fail += 1

                if (rotate_enabled and rotate_interval > 0
                        and consecutive_fail >= rotate_interval):
                    s.request_rotate(f"连续失败 {consecutive_fail} 次")
                    s.log(f"  [*] 已请求换IP (连续失败 {consecutive_fail} 次)")
                    consecutive_fail = 0
                    time.sleep(3)

                continue

            consecutive_ok += 1
            consecutive_fail = 0
            chapter.audio_url = audio_url
            short_url = audio_url[:70] + "..." if len(audio_url) > 70 else audio_url
            s.log(f"  [OK] {short_url}")

            ext = ".mp3"
            if ".m4a" in audio_url:
                ext = ".m4a"
            elif ".aac" in audio_url:
                ext = ".aac"

            filename = f"{chapter.index:04d}_{safe_title}{ext}"
            filepath = os.path.join(book_dir, filename)

            s.log(f"  [>] 下载: {filename}")
            dl_attempt = 0
            dl_ok = False
            while not dl_ok and not stop_evt.is_set():
                dl_attempt += 1
                dl_ok = _dl_file(audio_url, filepath, i, total)
                if dl_ok:
                    break
                wait = min(10 * dl_attempt, 60)
                s.log(f"  [!] 下载第{dl_attempt}次失败，等待 {wait}s 后重试...")
                for _ in range(wait):
                    if stop_evt.is_set():
                        break
                    time.sleep(1)

            if dl_ok:
                chapter.downloaded = True
                success += 1
                s.log("  [OK] 完成")
            else:
                fail += 1
                consecutive_ok = 0

            # ── 自适应延迟 ──
            if api_mode_ok > pw_mode_ok:
                if consecutive_ok > 30:
                    delay = random.uniform(0.3, 0.6)
                elif consecutive_ok > 10:
                    delay = random.uniform(0.5, 0.8)
                else:
                    delay = random.uniform(0.8, 1.5)
                if (i + 1) % 100 == 0:
                    pause = random.uniform(5, 10)
                    s.log(f"  [*] 已完成 {i+1} 集，休息 {pause:.0f}s...")
                    for _ in range(int(pause)):
                        if stop_evt.is_set():
                            break
                        time.sleep(1)
                else:
                    time.sleep(delay)
            else:
                if consecutive_ok > 30:
                    delay = random.uniform(0.5, 1.0)
                elif consecutive_ok > 10:
                    delay = random.uniform(1.0, 2.0)
                else:
                    delay = random.uniform(2.0, 3.5)
                if (i + 1) % 50 == 0:
                    pause = random.uniform(8, 15)
                    s.log(f"  [*] 已完成 {i+1} 集，休息 {pause:.0f}s...")
                    for _ in range(int(pause)):
                        if stop_evt.is_set():
                            break
                        time.sleep(1)
                else:
                    time.sleep(delay)

    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw_manager:
            try:
                pw_manager.stop()
            except Exception:
                pass

    record = {
        "title": book_title,
        "url": url,
        "chapters": [
            {
                "index": ch.index,
                "title": ch.title,
                "play_url": ch.play_url,
                "audio_url": getattr(ch, "audio_url", ""),
                "downloaded": getattr(ch, "downloaded", False),
            }
            for ch in chapters
        ],
    }
    record_path = os.path.join(book_dir, "download_record.json")
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    s.progress(1, "完成")
    stopped = " (已停止)" if stop_evt.is_set() else ""
    summary = f"完成{stopped} - 成功: {success}, 跳过: {skipped}, 失败: {fail}"
    s.status(summary)
    s.log(f"\n{'='*50}")
    s.log(f"[DONE] {summary}")
    s.log(f"  API快速模式: {api_mode_ok} 集, 浏览器模式: {pw_mode_ok} 集")
    s.log(f"  输出目录: {os.path.abspath(book_dir)}")
