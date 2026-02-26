#!/usr/bin/env python3
"""
有声小说下载器 - 图形界面版 v4.3
支持 ting13.cc / ting22.com (huanting.cc)
基于 CustomTkinter 的现代 UI - 多标签页多任务 (多进程)

特性:
- 多标签页：每本书一个页签，独立进程并行下载
- 集中式换IP管理：子进程发请求，主进程统一执行（全局冷却防冲突）
- 导航栏：ting13.cc 主页快捷跳转 + 最近10条历史URL快速回填
- 并行 CDN 下载 + URL 预取流水线
- 自适应延迟 + 主动 IP 轮换
"""

import multiprocessing
multiprocessing.freeze_support()

import os
import re
import sys
import json
import queue
import time
import threading
import webbrowser
from typing import Optional, Dict, Any, List

if __package__ in (None, ""):
    script_dir = os.path.dirname(__file__)
    sys.path.insert(0, os.path.abspath(os.path.join(script_dir, "..", "..")))
    sys.path.insert(0, os.path.abspath(os.path.join(script_dir, "..", "..", "..")))

# ── GUI 框架 ─────────────────────────────────────────────────
import customtkinter as ctk
from tkinter import filedialog, messagebox

# ── ting13.cc 旧架构 (Playwright) ────────────────────────────
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
    download_cover,
    detect_url_type,
    _build_session,
    save_cookies,
    load_cookies,
    clear_cookies,
    has_cookies,
    set_proxy,
    get_proxy,
    detect_system_proxy,
    ClashRotator,
    resolve_via_doh,
    _is_dns_poisoned,
)
from playwright.sync_api import sync_playwright

# ── huanting.cc 新架构 (DownloadEngine) ──────────────────────
from ting13.core.download import DownloadEngine, DownloadCallbacks
from ting13.core.network import set_proxy as core_set_proxy
from ting13.sources.huanting import HuantingSource


# ══════════════════════════════════════════════════════════════
# URL 历史管理
# ══════════════════════════════════════════════════════════════

class UrlHistory:
    MAX_ITEMS = 10
    FILENAME = "url_history.json"

    def __init__(self):
        self._path = self._resolve_path()
        self._urls: List[str] = []
        self._load()

    def _resolve_path(self) -> str:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, self.FILENAME)

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._urls = [u for u in data if isinstance(u, str)][:self.MAX_ITEMS]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._urls = []

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._urls, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    @property
    def urls(self) -> List[str]:
        return list(self._urls)

    def add(self, url: str):
        url = url.strip()
        if not url:
            return
        if url in self._urls:
            self._urls.remove(url)
        self._urls.insert(0, url)
        self._urls = self._urls[:self.MAX_ITEMS]
        self._save()

    def clear(self):
        self._urls.clear()
        self._save()

    def display_items(self) -> List[str]:
        if not self._urls:
            return ["(无历史记录)"]
        items = []
        for u in self._urls:
            label = u if len(u) <= 60 else u[:57] + "..."
            items.append(label)
        return items

    def url_for_display(self, display: str) -> Optional[str]:
        if display == "(无历史记录)":
            return None
        for u in self._urls:
            short = u if len(u) <= 60 else u[:57] + "..."
            if short == display:
                return u
        return display


# ══════════════════════════════════════════════════════════════
# 站点识别
# ══════════════════════════════════════════════════════════════

def detect_site(url: str) -> str:
    url_lower = url.lower()
    if any(d in url_lower for d in ["ting13.cc", "ting13.com"]):
        return "ting13"
    if any(d in url_lower for d in ["ting22.com", "huanting.cc"]):
        return "huanting"
    return "unknown"


# ══════════════════════════════════════════════════════════════
# 子进程工作函数 — 从独立模块导入 (解决 PyInstaller frozen spawn 问题)
# ══════════════════════════════════════════════════════════════
from ting13.workers.ting13_worker import worker_parse, worker_download


# ══════════════════════════════════════════════════════════════
# TaskTab — 单个下载任务标签页 (UI 层，主进程)
# ══════════════════════════════════════════════════════════════

class TaskTab:
    def __init__(self, app: "App", parent_frame: ctk.CTkFrame, tab_name: str):
        self._app = app
        self._parent = parent_frame
        self._tab_name = tab_name

        self._book_data: Optional[dict] = None
        self._current_site: str = "ting13"
        self._is_downloading = False
        self._worker_proc: Optional[multiprocessing.Process] = None
        self._stop_evt: Optional[multiprocessing.Event] = None
        self._mp_queue: Optional[multiprocessing.Queue] = None

        self._build_ui()

    def _build_ui(self):
        self._parent.grid_columnconfigure(0, weight=1)
        self._parent.grid_rowconfigure(4, weight=1)

        pad = {"padx": 8, "pady": (4, 0)}

        # ── URL + 输出目录 ──
        input_frame = ctk.CTkFrame(self._parent)
        input_frame.grid(row=0, column=0, sticky="ew", **pad)
        input_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(input_frame, text="书籍 URL:", width=70, anchor="e").grid(
            row=0, column=0, padx=(8, 4), pady=(8, 4)
        )
        self.url_entry = ctk.CTkEntry(
            input_frame, placeholder_text="ting13.cc 或 ting22.com 的书籍链接"
        )
        self.url_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=(8, 4))

        self.paste_btn = ctk.CTkButton(
            input_frame, text="粘贴", width=50, command=self._paste_url
        )
        self.paste_btn.grid(row=0, column=2, padx=4, pady=(8, 4))

        self.close_btn = ctk.CTkButton(
            input_frame, text="✕", width=32, height=28,
            fg_color="#8B0000", hover_color="#5C0000",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._on_close,
        )
        self.close_btn.grid(row=0, column=3, padx=(0, 8), pady=(8, 4))

        ctk.CTkLabel(input_frame, text="输出目录:", width=70, anchor="e").grid(
            row=1, column=0, padx=(8, 4), pady=(0, 8)
        )
        self.output_entry = ctk.CTkEntry(input_frame)
        self.output_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=4, pady=(0, 8))
        self.output_entry.insert(0, os.path.join(os.path.expanduser("~"), "Downloads"))

        self.browse_btn = ctk.CTkButton(
            input_frame, text="浏览", width=50, command=self._browse_output
        )
        self.browse_btn.grid(row=1, column=3, padx=(0, 8), pady=(0, 8))

        # ── 选项 + 按钮 ──
        opts_frame = ctk.CTkFrame(self._parent)
        opts_frame.grid(row=1, column=0, sticky="ew", **pad)

        settings_row = ctk.CTkFrame(opts_frame, fg_color="transparent")
        settings_row.pack(fill="x", padx=8, pady=(6, 0))

        ctk.CTkLabel(settings_row, text="起始集:").pack(side="left", padx=(0, 4))
        self.start_entry = ctk.CTkEntry(settings_row, width=60, justify="center")
        self.start_entry.insert(0, "1")
        self.start_entry.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(settings_row, text="结束集:").pack(side="left", padx=(0, 4))
        self.end_entry = ctk.CTkEntry(
            settings_row, width=60, justify="center", placeholder_text="全部"
        )
        self.end_entry.pack(side="left", padx=(0, 12))

        self.headless_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            settings_row, text="隐藏浏览器", variable=self.headless_var
        ).pack(side="left")

        btn_row = ctk.CTkFrame(opts_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=8, pady=(4, 6))

        self.parse_btn = ctk.CTkButton(
            btn_row, text="解析书籍", width=90,
            fg_color="#2B7A0B", hover_color="#1E5A08",
            command=self._on_parse
        )
        self.parse_btn.pack(side="left", padx=(0, 6))

        self.download_btn = ctk.CTkButton(
            btn_row, text="开始下载", width=90, command=self._on_download
        )
        self.download_btn.pack(side="left", padx=(0, 6))

        self.stop_btn = ctk.CTkButton(
            btn_row, text="停止", width=60,
            fg_color="#8B0000", hover_color="#5C0000",
            state="disabled", command=self._on_stop
        )
        self.stop_btn.pack(side="left")

        # ── 信息 + 进度 ──
        info_frame = ctk.CTkFrame(self._parent)
        info_frame.grid(row=2, column=0, sticky="ew", **pad)
        info_frame.grid_columnconfigure(1, weight=1)

        self.info_label = ctk.CTkLabel(
            info_frame, text="等待输入 URL ...",
            font=ctk.CTkFont(size=12), anchor="w",
        )
        self.info_label.grid(row=0, column=0, columnspan=3, sticky="ew", padx=8, pady=(6, 2))

        self.progress_bar = ctk.CTkProgressBar(info_frame, height=12)
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 2))
        self.progress_bar.set(0)

        self.progress_label = ctk.CTkLabel(
            info_frame, text="0%", width=50, font=ctk.CTkFont(size=11),
        )
        self.progress_label.grid(row=1, column=2, padx=(4, 8), pady=(0, 2))

        self.status_label = ctk.CTkLabel(
            info_frame, text="就绪",
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w",
        )
        self.status_label.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 6))

        # ── 日志 ──
        log_frame = ctk.CTkFrame(self._parent)
        log_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=(4, 8))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)

        log_header = ctk.CTkFrame(log_frame, fg_color="transparent")
        log_header.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 0))
        log_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_header, text="运行日志",
            font=ctk.CTkFont(size=11, weight="bold"), anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            log_header, text="清除", width=40, height=20,
            font=ctk.CTkFont(size=10),
            fg_color="#444444", hover_color="#555555",
            command=self._clear_log,
        ).grid(row=0, column=1, sticky="e")

        self.log_text = ctk.CTkTextbox(
            log_frame,
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="word", state="disabled",
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 6))

    # ── 辅助 ──

    def _paste_url(self):
        try:
            text = self._app.clipboard_get().strip()
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

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _get_url(self) -> str:
        raw = self.url_entry.get().strip()
        if raw.count("http") > 1:
            m = re.search(r'(https?://[^\s]+)', raw)
            if m:
                raw = m.group(1)
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, raw)
        return raw

    def _get_output_dir(self) -> str:
        return self.output_entry.get().strip() or "."

    def _get_range(self):
        s, e = 1, None
        try:
            s = int(self.start_entry.get().strip())
        except (ValueError, AttributeError):
            pass
        try:
            t = self.end_entry.get().strip()
            if t:
                e = int(t)
        except (ValueError, AttributeError):
            pass
        return s, e

    def _on_close(self):
        self._app.remove_task_tab(self._tab_name)

    # ── UI 更新（主线程调用）──

    def _ui_set_buttons(self, working: bool):
        st_main = "disabled" if working else "normal"
        st_stop = "normal" if working else "disabled"
        self.parse_btn.configure(state=st_main)
        self.download_btn.configure(state=st_main)
        self.stop_btn.configure(state=st_stop)
        self.url_entry.configure(state=st_main)
        self.output_entry.configure(state=st_main)
        self.browse_btn.configure(state=st_main)
        self.paste_btn.configure(state=st_main)

    def poll_queue(self):
        """主线程轮询：从 multiprocessing Queue 读消息更新 UI"""
        if self._mp_queue is None:
            return
        try:
            for _ in range(15):
                msg = self._mp_queue.get_nowait()
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
                    val = msg[1]
                    label = msg[2] if len(msg) > 2 else ""
                    self.progress_bar.set(val)
                    self.progress_label.configure(
                        text=label if label else f"{val * 100:.0f}%"
                    )
                elif kind == "buttons":
                    working = msg[1]
                    self._ui_set_buttons(working)
                    if not working:
                        self._is_downloading = False
                elif kind == "rename_tab":
                    new_name = msg[1]
                    old_name = self._tab_name
                    try:
                        tv = self._app.tabview
                        tv.rename(old_name, new_name)
                        if tv._current_name == old_name:
                            tv._current_name = new_name
                        self._tab_name = new_name
                        tabs = self._app._tabs
                        if old_name in tabs:
                            tabs[new_name] = tabs.pop(old_name)
                    except Exception:
                        pass
                elif kind == "result":
                    key = msg[1]
                    value = msg[2]
                    self._book_data = value
                    if key == "huanting_book":
                        self._current_site = "huanting"
                    else:
                        self._current_site = "ting13"
                elif kind == "rotate_request":
                    reason = msg[1] if len(msg) > 1 else ""
                    self._app.handle_rotate_request(self._tab_name, reason)
        except (queue.Empty, EOFError, OSError):
            pass

        if self._worker_proc is not None and not self._worker_proc.is_alive():
            self._worker_proc = None
            if self._is_downloading:
                self._is_downloading = False
                self._ui_set_buttons(False)

    # ── 启动子进程 ──

    def _launch_process(self, target, args):
        self._mp_queue = multiprocessing.Queue()
        self._stop_evt = multiprocessing.Event()
        full_args = (self._mp_queue, self._stop_evt) + args
        self._worker_proc = multiprocessing.Process(
            target=target, args=full_args, daemon=True
        )
        self._worker_proc.start()

    # ── 解析 ──

    def _on_parse(self):
        self._app.apply_proxy()
        url = self._get_url()
        if not url:
            messagebox.showwarning("提示", "请先输入书籍 URL")
            return

        site = detect_site(url)
        if site == "huanting":
            _ht = HuantingSource()
            url_type = _ht.detect_url_type(url)
            if url_type == "unknown":
                messagebox.showerror("错误", "无法识别的 URL 格式")
                return
            self._current_site = "huanting"
        elif site == "ting13":
            url_type = detect_url_type(url)
            if url_type == "unknown":
                messagebox.showerror("错误", "无法识别的 URL 格式")
                return
            if url_type == "play":
                self._book_data = None
                self._current_site = "ting13"
                self.info_label.configure(text="单集播放链接，可直接点击「开始下载」")
                return
            self._current_site = "ting13"
        else:
            messagebox.showerror("错误", "无法识别的 URL，支持 ting13.cc / ting22.com")
            return

        self._app.save_url_to_history(url)
        self._ui_set_buttons(True)
        self.info_label.configure(text="正在解析书籍信息...")
        self.status_label.configure(text="解析中...")
        self.progress_bar.set(0)
        self.progress_label.configure(text="解析中")
        self._book_data = None

        proxy = self._app.proxy_entry.get().strip()
        self._launch_process(worker_parse, (url, site, proxy))

    # ── 下载 ──

    def _on_download(self):
        self._app.apply_proxy()
        url = self._get_url()
        if not url:
            messagebox.showwarning("提示", "请先输入书籍 URL")
            return

        site = detect_site(url)
        if site == "huanting":
            self._current_site = "huanting"
            _ht = HuantingSource()
            url_type = _ht.detect_url_type(url)
        elif site == "ting13":
            self._current_site = "ting13"
            url_type = detect_url_type(url)
        else:
            messagebox.showerror("错误", "无法识别的 URL")
            return

        if url_type == "unknown":
            messagebox.showerror("错误", "无法识别的 URL 格式")
            return

        self._app.save_url_to_history(url)

        output_dir = self._get_output_dir()
        start, end = self._get_range()
        headless = self.headless_var.get()
        proxy = self._app.proxy_entry.get().strip()

        self._is_downloading = True
        self._ui_set_buttons(True)
        self.status_label.configure(text="准备下载...")
        self.progress_bar.set(0)
        self.progress_label.configure(text="0%")

        self._launch_process(worker_download, (
            url, site, url_type,
            output_dir, start, end,
            headless, proxy,
            self._app.rotate_enabled,
            self._app.get_rotate_interval(),
            self._book_data,
        ))

    # ── 停止 ──

    def _on_stop(self):
        if self._stop_evt is not None:
            self._stop_evt.set()
            self.status_label.configure(text="正在停止...")
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "[!] 正在停止...\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def kill_process(self):
        if self._stop_evt is not None:
            self._stop_evt.set()
        if self._worker_proc is not None and self._worker_proc.is_alive():
            self._worker_proc.terminate()
            self._worker_proc.join(timeout=2)


# ══════════════════════════════════════════════════════════════
# 主窗口 — 标签页管理器 + 集中式换IP管理
# ══════════════════════════════════════════════════════════════

class App(ctk.CTk):
    APP_TITLE = "有声小说下载器"
    APP_VERSION = "v4.3"
    WINDOW_WIDTH = 880
    WINDOW_HEIGHT = 800

    ROTATE_COOLDOWN = 30
    HOMEPAGE_URL = "https://www.ting13.cc/"

    def __init__(self):
        super().__init__()
        self.title(self.APP_TITLE)
        self.geometry(f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}")
        self.minsize(750, 650)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.clash_rotator: Optional[ClashRotator] = None
        self._tab_counter = 0
        self._tabs: Dict[str, TaskTab] = {}
        self._last_rotate_time: float = 0.0
        self.url_history = UrlHistory()

        self._build_ui()
        self._add_task_tab()
        self._poll_all()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        pad = {"padx": 12, "pady": (6, 0)}

        # ── Row 0: 标题 + 导航栏 ──
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", **pad)
        header_frame.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            header_frame, text=self.APP_TITLE,
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))

        self._home_btn = ctk.CTkButton(
            header_frame, text="ting13.cc", width=100, height=28,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#1a6b0a", hover_color="#145508",
            command=self._open_homepage,
        )
        self._home_btn.grid(row=0, column=1, padx=(0, 8))

        self._history_var = ctk.StringVar(value="历史记录")
        self._history_menu = ctk.CTkOptionMenu(
            header_frame,
            variable=self._history_var,
            values=self.url_history.display_items(),
            width=320, height=28,
            font=ctk.CTkFont(size=11),
            fg_color="#333333", button_color="#444444",
            button_hover_color="#555555",
            command=self._on_history_selected,
        )
        self._history_menu.grid(row=0, column=2, sticky="ew", padx=(0, 4))

        ctk.CTkButton(
            header_frame, text="清除", width=45, height=28,
            font=ctk.CTkFont(size=11),
            fg_color="#555555", hover_color="#666666",
            command=self._clear_history,
        ).grid(row=0, column=3, padx=(0, 4))

        ctk.CTkLabel(
            header_frame, text=self.APP_VERSION,
            font=ctk.CTkFont(size=11), text_color="gray",
        ).grid(row=0, column=4, sticky="e")

        # ── Row 1: 共享设置区 (代理 + 换IP) ──
        shared_frame = ctk.CTkFrame(self)
        shared_frame.grid(row=1, column=0, sticky="ew", **pad)
        shared_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(shared_frame, text="代理:", width=50, anchor="e").grid(
            row=0, column=0, padx=(8, 4), pady=(8, 4)
        )
        self.proxy_entry = ctk.CTkEntry(
            shared_frame,
            placeholder_text="留空=直连  例: http://127.0.0.1:7890"
        )
        self.proxy_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=(8, 4))

        self.proxy_detect_btn = ctk.CTkButton(
            shared_frame, text="自动检测", width=70,
            command=self._on_detect_proxy,
        )
        self.proxy_detect_btn.grid(row=0, column=2, padx=(4, 8), pady=(8, 4))

        bottom_row = ctk.CTkFrame(shared_frame, fg_color="transparent")
        bottom_row.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 8))

        self._rotate_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            bottom_row, text="自动换IP", variable=self._rotate_var, width=100,
        ).pack(side="left")

        ctk.CTkLabel(bottom_row, text=" 连续失败").pack(side="left")
        self.rotate_interval_entry = ctk.CTkEntry(bottom_row, width=35)
        self.rotate_interval_entry.insert(0, "3")
        self.rotate_interval_entry.pack(side="left", padx=2)
        ctk.CTkLabel(bottom_row, text="次换IP").pack(side="left")

        ctk.CTkLabel(bottom_row, text="  冷却").pack(side="left")
        self.cooldown_entry = ctk.CTkEntry(bottom_row, width=35)
        self.cooldown_entry.insert(0, "30")
        self.cooldown_entry.pack(side="left", padx=2)
        ctk.CTkLabel(bottom_row, text="秒").pack(side="left")

        self.clash_status_label = ctk.CTkLabel(
            bottom_row, text="", text_color="gray", font=ctk.CTkFont(size=10)
        )
        self.clash_status_label.pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            bottom_row, text="+ 新任务", width=70,
            fg_color="#2B7A0B", hover_color="#1E5A08",
            command=self._add_task_tab,
        ).pack(side="right", padx=(4, 0))

        self.login_btn = ctk.CTkButton(
            bottom_row, text="未登录", width=60,
            fg_color="#555555", hover_color="#666666",
            command=self._on_login,
        )
        self.login_btn.pack(side="right", padx=(4, 0))
        self._update_login_status()

        # ── Row 3: 标签页容器 ──
        self.tabview = ctk.CTkTabview(self, height=500)
        self.tabview.grid(row=3, column=0, sticky="nsew", padx=12, pady=(6, 12))

    # ── 标签页管理 ──

    def _add_task_tab(self):
        self._tab_counter += 1
        tab_name = f"任务 {self._tab_counter}"
        frame = self.tabview.add(tab_name)
        task = TaskTab(self, frame, tab_name)
        self._tabs[tab_name] = task
        self.tabview.set(tab_name)

    def remove_task_tab(self, tab_name: str):
        task = self._tabs.get(tab_name)
        if not task:
            return

        if task._is_downloading:
            ok = messagebox.askyesno(
                "确认关闭",
                f"「{tab_name}」正在下载中。\n确定要停止并关闭吗？"
            )
            if not ok:
                return
            task.kill_process()

        if len(self._tabs) <= 1:
            messagebox.showinfo("提示", "至少保留一个任务标签页")
            return

        del self._tabs[tab_name]
        try:
            self.tabview.delete(tab_name)
        except Exception:
            pass

    # ── 导航栏 ──

    def _open_homepage(self):
        webbrowser.open(self.HOMEPAGE_URL)

    def _on_history_selected(self, display: str):
        url = self.url_history.url_for_display(display)
        if not url:
            return
        current = self.tabview.get()
        task = self._tabs.get(current)
        if task:
            task.url_entry.delete(0, "end")
            task.url_entry.insert(0, url)
        self._history_var.set("历史记录")

    def _clear_history(self):
        self.url_history.clear()
        self._refresh_history_menu()
        self._log_to_current("[*] 已清除 URL 历史记录")

    def _refresh_history_menu(self):
        items = self.url_history.display_items()
        self._history_menu.configure(values=items)
        self._history_var.set("历史记录")

    def save_url_to_history(self, url: str):
        self.url_history.add(url)
        self._refresh_history_menu()

    # ── 共享属性 ──

    @property
    def rotate_enabled(self) -> bool:
        return self._rotate_var.get()

    def get_rotate_interval(self) -> int:
        try:
            val = int(self.rotate_interval_entry.get())
            return max(val, 1)
        except ValueError:
            return 3

    def _get_cooldown(self) -> float:
        try:
            return max(float(self.cooldown_entry.get()), 5.0)
        except ValueError:
            return 30.0

    # ── 集中式换IP管理 ──

    def handle_rotate_request(self, tab_name: str, reason: str):
        if not self.clash_rotator:
            return

        now = time.time()
        cooldown = self._get_cooldown()
        elapsed_since_last = now - self._last_rotate_time

        if elapsed_since_last < cooldown:
            remaining = cooldown - elapsed_since_last
            self._log_to_current(
                f"[*] [{tab_name}] 请求换IP被冷却 (剩余 {remaining:.0f}s): {reason}"
            )
            return

        new_node = self.clash_rotator.rotate()
        if new_node:
            self._last_rotate_time = time.time()
            msg = f"[*] 已切换代理节点: {new_node} (来自 {tab_name}: {reason})"
            for name, task in self._tabs.items():
                if task._mp_queue is not None:
                    task.log_text.configure(state="normal")
                    task.log_text.insert("end", msg + "\n")
                    task.log_text.see("end")
                    task.log_text.configure(state="disabled")
        else:
            self._log_to_current(f"[!] [{tab_name}] 换IP失败: {reason}")

    # ── 代理设置 ──

    def apply_proxy(self):
        proxy = self.proxy_entry.get().strip()
        if not proxy:
            detected = detect_system_proxy()
            if detected:
                proxy = detected
                self.proxy_entry.delete(0, "end")
                self.proxy_entry.insert(0, detected)
                self._log_to_current(f"[*] 自动检测到代理: {detected}")

                if self.clash_rotator is None:
                    try:
                        rotator = ClashRotator()
                        if rotator.auto_detect():
                            nodes = rotator.load_nodes()
                            if nodes:
                                self.clash_rotator = rotator
                                self._log_to_current(
                                    f"[*] Clash API 就绪: {rotator.group_name} ({len(nodes)} 节点)"
                                )
                    except Exception:
                        pass

        set_proxy(proxy if proxy else None)
        if proxy:
            self._log_to_current(f"[*] 使用代理: {proxy}")

    def _log_to_current(self, msg: str):
        current = self.tabview.get()
        task = self._tabs.get(current)
        if task:
            task.log_text.configure(state="normal")
            task.log_text.insert("end", msg + "\n")
            task.log_text.see("end")
            task.log_text.configure(state="disabled")

    def _on_detect_proxy(self):
        self._log_to_current("[*] 正在检测系统代理...")
        detected = detect_system_proxy()
        if detected:
            self.proxy_entry.delete(0, "end")
            self.proxy_entry.insert(0, detected)
            self._log_to_current(f"[OK] 检测到代理: {detected}")
        else:
            self._log_to_current("[!] 未检测到系统代理")
            messagebox.showinfo("提示", "未检测到系统代理。\n请确认 Clash Verge 已开启「系统代理」。")
            return

        self._log_to_current("[*] 正在检测 DNS 状态...")
        if _is_dns_poisoned("www.ting13.cc"):
            real_ip = resolve_via_doh("www.ting13.cc")
            self._log_to_current(f"[!] DNS 被污染! 真实 IP: {real_ip}")
        else:
            self._log_to_current("[OK] DNS 正常")

        self._log_to_current("[*] 正在检测 Clash API...")
        rotator = ClashRotator()
        if rotator.auto_detect():
            nodes = rotator.load_nodes()
            if nodes:
                self.clash_rotator = rotator
                self._rotate_var.set(True)
                self.clash_status_label.configure(
                    text=f"已连接  节点: {len(nodes)}个",
                    text_color="#2B7A0B",
                )
                self._log_to_current(f"[OK] Clash API 就绪  节点: {len(nodes)}")
            else:
                self.clash_status_label.configure(text="已连接但无节点", text_color="orange")
        else:
            self.clash_status_label.configure(text="未检测到", text_color="gray")
            self._log_to_current("[*] 未检测到 Clash API")

    # ── 登录 ──

    def _update_login_status(self):
        if has_cookies():
            self.login_btn.configure(
                text="已登录", fg_color="#2B7A0B", hover_color="#1E5A08",
            )
        else:
            self.login_btn.configure(
                text="未登录", fg_color="#555555", hover_color="#666666",
            )

    def _on_login(self):
        if has_cookies():
            choice = messagebox.askyesnocancel(
                "登录状态",
                "当前已登录。\n\n"
                "点击「是」重新登录\n"
                "点击「否」退出登录\n"
                "点击「取消」返回"
            )
            if choice is True:
                clear_cookies()
                self._update_login_status()
                self._do_login()
            elif choice is False:
                clear_cookies()
                self._update_login_status()
                self._log_to_current("[*] 已退出登录")
            return
        self._do_login()

    def _do_login(self):
        self._log_to_current("[*] 正在打开浏览器，请在浏览器中登录 ting13.cc ...")

        def worker():
            try:
                with sync_playwright() as pw:
                    launch_kwargs: Dict = {"headless": False}
                    if _is_frozen():
                        base = _get_bundled_base()
                        chrome_exe = os.path.join(
                            base, "ms-playwright", "chromium-1208",
                            "chrome-win64", "chrome.exe"
                        )
                        if os.path.isfile(chrome_exe):
                            launch_kwargs["executable_path"] = chrome_exe
                    if get_proxy():
                        launch_kwargs["proxy"] = {"server": get_proxy()}
                    browser = pw.chromium.launch(**launch_kwargs)
                    context = browser.new_context()
                    page = context.new_page()
                    page.goto(
                        "https://m.ting13.cc/user/public/login.html",
                        wait_until="domcontentloaded", timeout=30000,
                    )
                    self._log_to_current("[*] 浏览器已打开，请登录后关闭浏览器窗口")

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
                            for c in cookies
                        )
                        if has_session:
                            current_url = page.url
                            if "login" not in current_url:
                                logged_in = True
                                break
                        page.wait_for_timeout(1000)

                    try:
                        cookies = context.cookies()
                    except Exception:
                        cookies = []

                    if cookies:
                        save_cookies(cookies)
                        self._log_to_current(f"[OK] 已保存 {len(cookies)} 个 cookies")
                        if logged_in:
                            self._log_to_current("[OK] 登录成功！")
                    else:
                        self._log_to_current("[!] 未获取到 cookies")

                    try:
                        browser.close()
                    except Exception:
                        pass
            except Exception as e:
                self._log_to_current(f"[FAIL] 登录流程出错: {e}")
            finally:
                self.after(100, self._update_login_status)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    # ── 全局轮询 ──

    def _poll_all(self):
        for task in list(self._tabs.values()):
            try:
                task.poll_queue()
            except Exception:
                pass
        self.after(80, self._poll_all)


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════

def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
