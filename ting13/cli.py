#!/usr/bin/env python3
"""
有声小说下载器 — 命令行接口 (v3.0 重构版)

统一的 CLI 入口, 自动识别站点并调用对应 Source 插件。

用法:
    # ting13.cc
    python cli.py "https://www.ting13.cc/youshengxiaoshuo/10408/"

    # ting22.com / huanting.cc
    python cli.py "https://www.huanting.cc/book/2274.html"

    # 通用选项
    python cli.py -o ./downloads --start 5 --end 10 "URL"
    python cli.py --proxy auto "URL"
    python cli.py --rotate 30 "URL"
"""

import argparse
import sys

from core.utils import fix_windows_encoding, setup_playwright_env

fix_windows_encoding()
setup_playwright_env()

from core.models import BookInfo, Chapter
from core.network import set_proxy, get_proxy, detect_system_proxy, ClashRotator
from core.download import DownloadEngine, DownloadCallbacks
from sources import find_source, get_source_names


def main():
    parser = argparse.ArgumentParser(
        description="有声小说下载器 (插件化架构)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
支持的站点: {', '.join(get_source_names())}

示例:
  # 下载整本书
  python cli.py "https://www.ting13.cc/youshengxiaoshuo/10408/"

  # 下载 ting22.com 的书
  python cli.py "https://www.huanting.cc/book/2274.html"

  # 指定范围和输出目录
  python cli.py -o ./audiobooks --start 5 --end 10 "URL"

  # 使用代理 (auto = 自动检测)
  python cli.py --proxy auto "URL"

  # Clash 自动换 IP (每30集)
  python cli.py --proxy auto --rotate 30 "URL"
        """,
    )
    parser.add_argument("url", help="书籍页面或播放页面 URL")
    parser.add_argument("-o", "--output", default=".", help="输出目录 (默认: 当前目录)")
    parser.add_argument("--start", type=int, default=1, help="起始章节 (默认: 1)")
    parser.add_argument("--end", type=int, default=None, help="结束章节 (默认: 全部)")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--proxy", default=None,
                        help="代理地址 (auto = 自动检测)")
    parser.add_argument("--rotate", type=int, default=0,
                        help="通过 Clash API 每 N 集自动换 IP")

    args = parser.parse_args()

    # ── 识别站点 ──
    source = find_source(args.url)
    if not source:
        print(f"[FAIL] 无法识别的 URL: {args.url}")
        print(f"  支持的站点: {', '.join(get_source_names())}")
        sys.exit(1)

    print("=" * 60)
    print(f"  有声小说下载器 v3.0  [{source.name}]")
    print("=" * 60)

    # ── 代理 ──
    if args.proxy:
        if args.proxy.lower() == "auto":
            detected = detect_system_proxy()
            if detected:
                set_proxy(detected)
                print(f"[*] 自动检测到代理: {detected}")
            else:
                print("[!] 未检测到系统代理, 将使用直连")
        else:
            set_proxy(args.proxy)
            print(f"[*] 代理: {args.proxy}")

    # ── Clash ──
    rotator = None
    if args.rotate > 0:
        rotator = ClashRotator()
        if rotator.auto_detect():
            nodes = rotator.load_nodes()
            print(f"[*] Clash API: {rotator.api_url}  "
                  f"代理组: {rotator.group_name}  节点: {len(nodes)}")
        else:
            print("[!] 未检测到 Clash API, 自动换IP不可用")
            rotator = None

    # ── 配置 Source ──
    if hasattr(source, 'set_headless'):
        source.set_headless(not args.no_headless)

    # ── 解析或单集 ──
    url_type = source.detect_url_type(args.url)
    if url_type == "play":
        book = BookInfo(
            title="single_audio",
            chapters=[Chapter(index=1, title="单集", play_url=args.url)],
            source_name=source.name,
        )
    elif url_type == "book":
        book = source.parse_book(args.url)
    else:
        print(f"[FAIL] 无法识别的 URL 类型: {args.url}")
        sys.exit(1)

    if not book.chapters:
        print("[FAIL] 未找到任何章节")
        sys.exit(1)

    # ── 下载 ──
    callbacks = DownloadCallbacks(
        on_log=lambda msg: print(msg),
        on_status=lambda text: None,
        on_info=lambda text: print(f"[*] {text}"),
        on_progress=lambda val, label: None,
        is_stopped=lambda: False,
    )

    engine = DownloadEngine(
        source=source,
        callbacks=callbacks,
        clash_rotator=rotator,
        rotate_interval=args.rotate,
    )
    engine.run(book, args.output, args.start, args.end)


if __name__ == "__main__":
    main()
