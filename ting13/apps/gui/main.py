#!/usr/bin/env python3
"""
有声小说下载器 — 图形界面 (v3.0 重构版)

基于插件化 Source 架构, 站点无关的 GUI。
添加新站点不需要修改本文件。

依赖:
    pip install customtkinter playwright requests lxml cssselect
    playwright install chromium
"""

import os
import sys
import queue
import threading
from typing import Optional, Dict

if __package__ in (None, ""):
    script_dir = os.path.dirname(__file__)
    sys.path.insert(0, os.path.abspath(os.path.join(script_dir, "..", "..")))
    sys.path.insert(0, os.path.abspath(os.path.join(script_dir, "..", "..", "..")))

# ── 初始化 ──
from ting13.core.utils import fix_windows_encoding, setup_playwright_env
fix_windows_encoding()
setup_playwright_env()

# ── GUI 框架 ──
import customtkinter as ctk
from tkinter import filedialog, messagebox

# ── 核心模块 ──
from ting13.core.models import BookInfo
from ting13.core.network import (
    set_proxy, get_proxy, detect_system_proxy,
    is_dns_poisoned, resolve_via_doh, ClashRotator,
)
from ting13.core.download import DownloadEngine, DownloadCallbacks
from ting13.core.utils import is_frozen, get_bundled_base

# ── Source 插件系统 ──
from ting13.sources import find_source, get_source_names
from ting13.sources.base import Source

# ── ting13 登录支持 ──
from ting13.sources.ting13 import (
    save_cookies, load_cookies, clear_cookies, has_cookies,
)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    pass


# ══════════════════════════════════════════════════════════════
# 日志重定向
# ══════════════════════════════════════════════════════════════

class QueueWriter:
    """将 print() 输出转发到 queue, 供 GUI 线程读取"""

    def __init__(self, log_queue: queue.Queue):
        self._queue = log_queue
        self.encoding = "utf-8"

    def write(self, text: str):
        if text and text.strip():
            self._queue.put(("log", text.rstrip("\n")))

    def flush(self):
        pass


# ══════════════════════════════════════════════════════════════
# 主窗口
# ══════════════════════════════════════════════════════════════

class App(ctk.CTk):
    """有声小说下载器 — 插件化架构 GUI"""

    APP_TITLE = "有声小说下载器"
    VERSION = "v3.0"
    WINDOW_WIDTH = 820
    WINDOW_HEIGHT = 720

    def __init__(self):
        super().__init__()

        self.title(self.APP_TITLE)
        self.geometry(f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}")
        self.minsize(750, 620)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # ── 状态 ──
        self._source: Optional[Source] = None
        self._book_info: Optional[BookInfo] = None
        self._is_downloading = False
        self._stop_requested = False
        self._worker_thread: Optional[threading.Thread] = None
        self._msg_queue: queue.Queue = queue.Queue()
        self._clash_rotator: Optional[ClashRotator] = None

        # ── 构建 UI ──
        self._build_ui()
        self._poll_queue()

    # ══════════════════════════════════════════════════════════
    # UI 构建
    # ══════════════════════════════════════════════════════════

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        pad = {"padx": 16, "pady": (8, 0)}

        # ── 1) 标题 ──
        title_frame = ctk.CTkFrame(self, fg_color="transparent")
        title_frame.grid(row=0, column=0, sticky="ew", **pad)
        title_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            title_frame, text=self.APP_TITLE,
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        sites = " / ".join(get_source_names())
        ctk.CTkLabel(
            title_frame,
            text=f"{self.VERSION}  ({sites})",
            font=ctk.CTkFont(size=12), text_color="gray",
        ).grid(row=0, column=1, sticky="e")

        # ── 2) 输入区 ──
        input_frame = ctk.CTkFrame(self)
        input_frame.grid(row=1, column=0, sticky="ew", **pad)
        input_frame.grid_columnconfigure(1, weight=1)

        # URL
        ctk.CTkLabel(input_frame, text="书籍 URL:", width=80, anchor="e").grid(
            row=0, column=0, padx=(12, 4), pady=10)
        self.url_entry = ctk.CTkEntry(
            input_frame, placeholder_text=f"支持: {sites}")
        self.url_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=10)

        self.paste_btn = ctk.CTkButton(
            input_frame, text="粘贴", width=60, command=self._paste_url)
        self.paste_btn.grid(row=0, column=2, padx=(4, 12), pady=10)

        # 输出目录
        ctk.CTkLabel(input_frame, text="输出目录:", width=80, anchor="e").grid(
            row=1, column=0, padx=(12, 4), pady=(0, 10))
        self.output_entry = ctk.CTkEntry(input_frame)
        self.output_entry.grid(row=1, column=1, sticky="ew", padx=4, pady=(0, 10))
        self.output_entry.insert(0, os.path.join(os.path.expanduser("~"), "Downloads"))

        self.browse_btn = ctk.CTkButton(
            input_frame, text="浏览...", width=60, command=self._browse_output)
        self.browse_btn.grid(row=1, column=2, padx=(4, 12), pady=(0, 10))

        # 代理
        ctk.CTkLabel(input_frame, text="代理:", width=80, anchor="e").grid(
            row=2, column=0, padx=(12, 4), pady=(0, 10))
        self.proxy_entry = ctk.CTkEntry(
            input_frame,
            placeholder_text="留空=直连  例: http://127.0.0.1:7890  socks5://127.0.0.1:1080")
        self.proxy_entry.grid(row=2, column=1, sticky="ew", padx=4, pady=(0, 10))

        self.proxy_detect_btn = ctk.CTkButton(
            input_frame, text="自动检测", width=70, command=self._on_detect_proxy)
        self.proxy_detect_btn.grid(row=2, column=2, padx=(4, 12), pady=(0, 10))

        # Clash
        clash_frame = ctk.CTkFrame(input_frame, fg_color="transparent")
        clash_frame.grid(row=3, column=1, columnspan=2, sticky="ew", padx=4, pady=(0, 10))

        self._rotate_var = ctk.BooleanVar(value=False)
        self.rotate_check = ctk.CTkCheckBox(
            clash_frame, text="自动换IP (Clash)",
            variable=self._rotate_var, width=160)
        self.rotate_check.pack(side="left")

        ctk.CTkLabel(clash_frame, text="  每").pack(side="left")
        self.rotate_interval_entry = ctk.CTkEntry(clash_frame, width=45)
        self.rotate_interval_entry.insert(0, "30")
        self.rotate_interval_entry.pack(side="left", padx=2)
        ctk.CTkLabel(clash_frame, text="集切换").pack(side="left")

        self.clash_status_label = ctk.CTkLabel(
            clash_frame, text="", text_color="gray", font=ctk.CTkFont(size=11))
        self.clash_status_label.pack(side="left", padx=(12, 0))

        # ── 3) 选项 + 按钮 ──
        opts_frame = ctk.CTkFrame(self)
        opts_frame.grid(row=2, column=0, sticky="ew", **pad)

        settings_row = ctk.CTkFrame(opts_frame, fg_color="transparent")
        settings_row.pack(fill="x", padx=12, pady=(10, 0))

        ctk.CTkLabel(settings_row, text="起始集:").pack(side="left", padx=(0, 4))
        self.start_entry = ctk.CTkEntry(settings_row, width=70, justify="center")
        self.start_entry.insert(0, "1")
        self.start_entry.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(settings_row, text="结束集:").pack(side="left", padx=(0, 4))
        self.end_entry = ctk.CTkEntry(
            settings_row, width=70, justify="center", placeholder_text="全部")
        self.end_entry.pack(side="left", padx=(0, 20))

        self.headless_var = ctk.BooleanVar(value=True)
        self.headless_cb = ctk.CTkCheckBox(
            settings_row, text="隐藏浏览器窗口", variable=self.headless_var)
        self.headless_cb.pack(side="left")

        btn_row = ctk.CTkFrame(opts_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(6, 10))

        self.login_btn = ctk.CTkButton(
            btn_row, text="未登录", width=80,
            fg_color="#555555", hover_color="#666666",
            command=self._on_login)
        self.login_btn.pack(side="left", padx=(0, 16))
        self._update_login_status()

        self.parse_btn = ctk.CTkButton(
            btn_row, text="解析书籍", width=100,
            fg_color="#2B7A0B", hover_color="#1E5A08",
            command=self._on_parse)
        self.parse_btn.pack(side="left", padx=(0, 8))

        self.download_btn = ctk.CTkButton(
            btn_row, text="开始下载", width=100, command=self._on_download)
        self.download_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ctk.CTkButton(
            btn_row, text="停止", width=70,
            fg_color="#8B0000", hover_color="#5C0000",
            state="disabled", command=self._on_stop)
        self.stop_btn.pack(side="left")

        # ── 4) 信息 + 进度 ──
        info_frame = ctk.CTkFrame(self)
        info_frame.grid(row=3, column=0, sticky="ew", **pad)
        info_frame.grid_columnconfigure(1, weight=1)

        self.info_label = ctk.CTkLabel(
            info_frame, text="等待输入 URL ...",
            font=ctk.CTkFont(size=13), anchor="w")
        self.info_label.grid(row=0, column=0, columnspan=3, sticky="ew", padx=12, pady=(10, 4))

        self.progress_bar = ctk.CTkProgressBar(info_frame, height=14)
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 4))
        self.progress_bar.set(0)

        self.progress_label = ctk.CTkLabel(
            info_frame, text="0%", width=50, font=ctk.CTkFont(size=12))
        self.progress_label.grid(row=1, column=2, padx=(4, 12), pady=(0, 4))

        self.status_label = ctk.CTkLabel(
            info_frame, text="就绪",
            font=ctk.CTkFont(size=12), text_color="gray", anchor="w")
        self.status_label.grid(row=2, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 10))

        # ── 5) 日志 ──
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=4, column=0, sticky="nsew", padx=16, pady=(8, 16))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)

        self.log_text = ctk.CTkTextbox(
            log_frame,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    # ══════════════════════════════════════════════════════════
    # 辅助方法
    # ══════════════════════════════════════════════════════════

    def _paste_url(self):
        try:
            text = self.clipboard_get().strip()
            if text:
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, text)
        except Exception:
            pass

    def _browse_output(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, path)

    def _log(self, msg: str):
        self._msg_queue.put(("log", msg))

    def _set_status(self, text: str):
        self._msg_queue.put(("status", text))

    def _set_info(self, text: str):
        self._msg_queue.put(("info", text))

    def _set_progress(self, value: float, label: str = ""):
        self._msg_queue.put(("progress", value, label))

    def _set_buttons(self, working: bool):
        self._msg_queue.put(("buttons", working))

    def _poll_queue(self):
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                kind = msg[0]

                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", msg[1] + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status_label.configure(text=msg[1])
                elif kind == "info":
                    self.info_label.configure(text=msg[1])
                elif kind == "progress":
                    self.progress_bar.set(msg[1])
                    label = msg[2] if len(msg) > 2 else ""
                    self.progress_label.configure(
                        text=label if label else f"{msg[1] * 100:.0f}%")
                elif kind == "buttons":
                    working = msg[1]
                    state_main = "disabled" if working else "normal"
                    state_stop = "normal" if working else "disabled"
                    for w in [self.parse_btn, self.download_btn, self.url_entry,
                              self.output_entry, self.browse_btn, self.proxy_entry,
                              self.proxy_detect_btn, self.rotate_check,
                              self.rotate_interval_entry, self.paste_btn,
                              self.login_btn]:
                        w.configure(state=state_main)
                    self.stop_btn.configure(state=state_stop)
                elif kind == "update_login":
                    self._update_login_status()
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _get_url(self) -> str:
        return self.url_entry.get().strip()

    def _get_output_dir(self) -> str:
        return self.output_entry.get().strip() or "."

    def _get_range(self):
        start, end = 1, None
        try:
            start = int(self.start_entry.get().strip())
        except (ValueError, AttributeError):
            pass
        try:
            end_text = self.end_entry.get().strip()
            if end_text:
                end = int(end_text)
        except (ValueError, AttributeError):
            pass
        return start, end

    # ══════════════════════════════════════════════════════════
    # 代理
    # ══════════════════════════════════════════════════════════

    def _apply_proxy(self):
        proxy = self.proxy_entry.get().strip()
        if not proxy:
            detected = detect_system_proxy()
            if detected:
                proxy = detected
                self.proxy_entry.delete(0, "end")
                self.proxy_entry.insert(0, detected)
                self._log(f"[*] 自动检测到代理: {detected}")
                self._try_init_clash()

        set_proxy(proxy if proxy else None)
        if proxy:
            self._log(f"[*] 使用代理: {proxy}")
        else:
            self._log("[*] 未使用代理 (直连模式)")

    def _try_init_clash(self):
        if self._clash_rotator is not None:
            return
        try:
            rotator = ClashRotator()
            if rotator.auto_detect():
                nodes = rotator.load_nodes()
                if nodes:
                    self._clash_rotator = rotator
                    self._log(f"[*] Clash API 就绪: {rotator.group_name} ({len(nodes)} 个节点)")
        except Exception:
            pass

    def _on_detect_proxy(self):
        self._log("[*] 正在检测系统代理...")
        detected = detect_system_proxy()
        if detected:
            self.proxy_entry.delete(0, "end")
            self.proxy_entry.insert(0, detected)
            self._log(f"[OK] 检测到代理: {detected}")
        else:
            self._log("[!] 未检测到系统代理")
            messagebox.showinfo("提示", "未检测到系统代理。\n请确认 Clash Verge 已开启「系统代理」。")
            return

        self._log("[*] 正在检测 DNS 状态...")
        if is_dns_poisoned("www.ting13.cc"):
            real_ip = resolve_via_doh("www.ting13.cc")
            self._log(f"[!] DNS 被污染! 真实 IP: {real_ip}")
            self._log("[!] 请在 Clash Verge 中切换到「全局模式」")
        else:
            self._log("[OK] DNS 正常")

        self._log("[*] 正在检测 Clash API...")
        rotator = ClashRotator()
        if rotator.auto_detect():
            nodes = rotator.load_nodes()
            if nodes:
                self._clash_rotator = rotator
                self._rotate_var.set(True)
                self.clash_status_label.configure(
                    text=f"已连接  {rotator.group_name}  {len(nodes)}个节点",
                    text_color="#2B7A0B")
                self._log(f"[OK] Clash API: {rotator.api_url}")
            else:
                self.clash_status_label.configure(
                    text="已连接但无节点", text_color="orange")
        else:
            self.clash_status_label.configure(
                text="未检测到 Clash API", text_color="gray")
            self._log("[*] 未检测到 Clash API (自动换IP不可用)")

    # ══════════════════════════════════════════════════════════
    # 登录 (ting13.cc)
    # ══════════════════════════════════════════════════════════

    def _update_login_status(self):
        if has_cookies():
            self.login_btn.configure(
                text="已登录", fg_color="#2B7A0B", hover_color="#1E5A08")
        else:
            self.login_btn.configure(
                text="未登录", fg_color="#555555", hover_color="#666666")

    def _on_login(self):
        if has_cookies():
            choice = messagebox.askyesnocancel(
                "登录状态", "当前已登录。\n\n是 = 重新登录\n否 = 退出登录\n取消 = 返回")
            if choice is True:
                clear_cookies()
                self._update_login_status()
                self._do_login()
            elif choice is False:
                clear_cookies()
                self._update_login_status()
                self._log("[*] 已退出登录")
            return
        self._do_login()

    def _do_login(self):
        self._set_buttons(True)
        self._log("[*] 正在打开浏览器, 请在浏览器中登录 ting13.cc ...")
        self._set_status("等待登录...")

        def worker():
            try:
                with sync_playwright() as pw:
                    launch_kwargs: Dict = {"headless": False}
                    from ting13.core.utils import get_chrome_exe_path
                    chrome = get_chrome_exe_path()
                    if chrome:
                        launch_kwargs["executable_path"] = chrome
                    if get_proxy():
                        launch_kwargs["proxy"] = {"server": get_proxy()}

                    browser = pw.chromium.launch(**launch_kwargs)
                    context = browser.new_context()
                    page = context.new_page()
                    page.goto("https://m.ting13.cc/user/public/login.html",
                              wait_until="domcontentloaded", timeout=30000)
                    self._log("[*] 浏览器已打开, 请登录后关闭浏览器窗口")

                    logged_in = False
                    for _ in range(600):
                        try:
                            _ = page.url
                        except Exception:
                            break
                        cookies = context.cookies()
                        has_session = any(
                            c.get("name") in ("PHPSESSID", "user_token", "token", "uid")
                            or "user" in c.get("name", "").lower()
                            for c in cookies)
                        if has_session and "login" not in page.url:
                            logged_in = True
                            break
                        page.wait_for_timeout(1000)

                    try:
                        cookies = context.cookies()
                    except Exception:
                        cookies = []

                    if cookies:
                        save_cookies(cookies)
                        self._log(f"[OK] 已保存 {len(cookies)} 个 cookies")
                        if logged_in:
                            self._log("[OK] 登录成功!")
                    try:
                        browser.close()
                    except Exception:
                        pass
            except Exception as e:
                self._log(f"[FAIL] 登录流程出错: {e}")
            finally:
                self._msg_queue.put(("update_login",))
                self._set_buttons(False)
                self._set_status("就绪")

        threading.Thread(target=worker, daemon=True).start()

    # ══════════════════════════════════════════════════════════
    # 解析书籍 (通用 — 使用 Source 插件)
    # ══════════════════════════════════════════════════════════

    def _on_parse(self):
        self._apply_proxy()
        url = self._get_url()
        if not url:
            messagebox.showwarning("提示", "请先输入书籍 URL")
            return

        source = find_source(url)
        if not source:
            messagebox.showerror("错误",
                f"无法识别的 URL\n\n支持: {', '.join(get_source_names())}")
            return

        url_type = source.detect_url_type(url)
        if url_type == "play":
            self._source = source
            self._book_info = None
            self._set_info(f"[{source.name}] 单集播放链接, 可直接下载")
            self._log(f"[*] [{source.name}] 检测到单集链接")
            return

        if url_type == "unknown":
            messagebox.showerror("错误", f"无法识别的 URL 格式")
            return

        self._set_buttons(True)
        self._set_info("正在解析书籍信息...")
        self._set_status("解析中...")
        self._set_progress(0, "解析中")
        self._source = source
        self._book_info = None

        def worker():
            old_stdout = sys.stdout
            sys.stdout = QueueWriter(self._msg_queue)
            try:
                info = source.parse_book(url)
                self._book_info = info
                total = len(info.chapters)
                self._set_info(
                    f"[{source.name}] {info.title}   "
                    f"作者: {info.author}   章节: {total}")
                self._set_status(f"解析完成 - {total} 个章节")
                self._set_progress(1, "完成")
                self._log(f"[OK] {info.title} ({total} 章) [{source.name}]")
            except Exception as e:
                self._set_info(f"解析失败: {e}")
                self._set_status("解析失败")
                self._log(f"[FAIL] 解析失败: {e}")
            finally:
                sys.stdout = old_stdout
                self._set_buttons(False)

        threading.Thread(target=worker, daemon=True).start()

    # ══════════════════════════════════════════════════════════
    # 开始下载 (通用 — 使用 DownloadEngine)
    # ══════════════════════════════════════════════════════════

    def _on_download(self):
        self._apply_proxy()
        url = self._get_url()
        if not url:
            messagebox.showwarning("提示", "请先输入书籍 URL")
            return

        source = find_source(url)
        if not source:
            messagebox.showerror("错误", "无法识别的 URL")
            return

        self._source = source

        # 配置 Source (如 headless)
        if hasattr(source, 'set_headless'):
            source.set_headless(self.headless_var.get())

        output_dir = self._get_output_dir()
        start, end = self._get_range()

        self._is_downloading = True
        self._stop_requested = False
        self._set_buttons(True)
        self._set_status("准备下载...")
        self._set_progress(0, "0%")

        # Clash 参数
        rotate_interval = 0
        if self._rotate_var.get() and self._clash_rotator:
            try:
                rotate_interval = int(self.rotate_interval_entry.get())
            except ValueError:
                rotate_interval = 30

        def worker():
            old_stdout = sys.stdout
            sys.stdout = QueueWriter(self._msg_queue)
            try:
                # 如果未解析, 先解析
                if self._book_info is None:
                    url_type = source.detect_url_type(url)
                    if url_type == "play":
                        from ting13.core.models import Chapter
                        self._book_info = BookInfo(
                            title="single_audio",
                            chapters=[Chapter(index=1, title="单集", play_url=url)],
                            source_name=source.name,
                        )
                    else:
                        self._log("[*] 尚未解析, 先解析书籍...")
                        self._book_info = source.parse_book(url)

                # 创建回调
                callbacks = DownloadCallbacks(
                    on_log=self._log,
                    on_status=self._set_status,
                    on_info=self._set_info,
                    on_progress=self._set_progress,
                    is_stopped=lambda: self._stop_requested,
                )

                # 创建并运行下载引擎
                engine = DownloadEngine(
                    source=source,
                    callbacks=callbacks,
                    clash_rotator=self._clash_rotator,
                    rotate_interval=rotate_interval,
                )
                engine.run(self._book_info, output_dir, start, end)

            except Exception as e:
                self._log(f"[FAIL] 下载出错: {e}")
                self._set_status(f"出错: {e}")
            finally:
                sys.stdout = old_stdout
                self._is_downloading = False
                self._stop_requested = False
                self._set_buttons(False)

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _on_stop(self):
        if self._is_downloading:
            self._stop_requested = True
            self._set_status("正在停止...")
            self._log("[!] 正在停止下载...")


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════

def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
