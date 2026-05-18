#!/usr/bin/env python3
"""
有声小说下载器 — 命令行接口 (v4.3)

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
import time

if __package__ in (None, ""):
    import os
    script_dir = os.path.dirname(__file__)
    sys.path.insert(0, os.path.abspath(os.path.join(script_dir, "..", "..")))
    sys.path.insert(0, os.path.abspath(os.path.join(script_dir, "..", "..", "..")))

from ting13.core.utils import fix_windows_encoding, setup_playwright_env

fix_windows_encoding()
setup_playwright_env()

from ting13.core.models import BookInfo, Chapter
from ting13.core.network import set_proxy, get_proxy, detect_system_proxy, ClashRotator, set_proxy_pool, get_proxy_pool
from ting13.core.download import DownloadEngine, DownloadCallbacks
from ting13.core.config import load_config, save_config
from ting13.sources import find_source, get_source_names


def main():
    cfg = load_config()

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

  # 使用代理池自动换 IP
  python cli.py --proxy-pool-url "http://localhost:5149" --proxy-pool-key "your-key" --rotate 15 "URL"

  # 代理池 + 极速模式（最快下载，推荐）
  python cli.py --proxy-pool-url "http://localhost:5149" --proxy-pool-key "your-key" --rotate 10 --fast "URL"

  # 代理池 + 自定义线程数
  python cli.py --proxy-pool-url "http://localhost:5149" --proxy-pool-key "your-key" --download-workers 5 --url-fetch-workers 4 "URL"

  # 终极方案：批量预获取所有 URL 后全速下载（最快！）
  python cli.py --proxy-pool-url "http://localhost:5149" --proxy-pool-key "your-key" --rotate 10 --batch "URL"

配置保存于: ~/.ting13/config.json
        """,
    )
    parser.add_argument("url", help="书籍页面或播放页面 URL")
    parser.add_argument("-o", "--output", default=cfg.get("output", "."), help="输出目录 (默认: 当前目录)")
    parser.add_argument("--start", type=int, default=1, help="起始章节 (默认: 1)")
    parser.add_argument("--end", type=int, default=None, help="结束章节 (默认: 全部)")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--proxy", default=cfg.get("proxy"),
                        help="代理地址 (auto = 自动检测)")
    parser.add_argument("--rotate", type=int, default=cfg.get("rotate", 0),
                        help="通过 Clash API 每 N 集自动换 IP")
    parser.add_argument("--proxy-pool-url", default=cfg.get("proxy_pool_url"),
                        help="代理池 API URL (例如: http://localhost:5149)")
    parser.add_argument("--proxy-pool-key", default=cfg.get("proxy_pool_key"),
                        help="代理池 API Key")
    parser.add_argument("--download-workers", type=int, default=cfg.get("download_workers", 0),
                        help="并行下载线程数 (默认: 4)")
    parser.add_argument("--url-fetch-workers", type=int, default=cfg.get("url_fetch_workers", 0),
                        help="并发获取 URL 线程数 (仅代理池有效, 默认: 5)")
    parser.add_argument("--fast", action="store_true",
                        help="极速模式 (仅代理池有效, 最小延迟, 依赖多 IP)")
    parser.add_argument("--batch", action="store_true",
                        help="批量预获取模式 (仅代理池有效, 先获取所有 URL 再全速下载)")
    parser.add_argument("--delay", type=float, default=cfg.get("delay", 0),
                        help="额外请求延迟(秒), 用于多进程并发时防限流 (默认: 0)")
    parser.add_argument("--save-config", action="store_true",
                        help="将本次参数保存为用户默认配置")
    parser.add_argument("-V", "--version", action="version", version="ting13 v4.3.0")

    args = parser.parse_args()

    # ── 识别站点 ──
    source = find_source(args.url)
    if not source:
        print(f"[FAIL] 无法识别的 URL: {args.url}")
        print(f"  支持的站点: {', '.join(get_source_names())}")
        sys.exit(1)

    print("=" * 60)
    print(f"  有声小说下载器 v4.3  [{source.name}]")
    print("=" * 60)

    # ── 代理池（优先） ──
    proxy_pool = None
    if args.proxy_pool_url and args.proxy_pool_key:
        set_proxy_pool(args.proxy_pool_url, args.proxy_pool_key)
        proxy_pool = get_proxy_pool()
        if proxy_pool.get_proxy():
            print(f"[*] 代理池已连接: {args.proxy_pool_url} (优先使用)")
        else:
            print(f"[!] 代理池连接失败，将尝试使用 Clash 作为备用")
            proxy_pool = None

    # ── Clash（备用） ──
    rotator = None
    parse_rotator = None
    if args.rotate > 0:
        rotator = ClashRotator()
        if rotator.auto_detect():
            nodes = rotator.load_nodes()
            if proxy_pool:
                print(f"[*] Clash API 可用（备用方案）: {rotator.api_url}  "
                      f"代理组: {rotator.group_name}  节点: {len(nodes)}")
            else:
                print(f"[*] 使用 Clash 作为代理: {rotator.api_url}  "
                      f"代理组: {rotator.group_name}  节点: {len(nodes)}")
            parse_rotator = rotator
        else:
            if not proxy_pool:
                print("[!] 未检测到 Clash API, 自动换IP不可用")
            rotator = None
    elif args.proxy and args.proxy.lower() == "auto" and not proxy_pool:
        # 只有在没有代理池的情况下，才在解析书籍阶段启用节点轮换兜底
        probe_rotator = ClashRotator()
        if probe_rotator.auto_detect():
            nodes = probe_rotator.load_nodes()
            if nodes:
                parse_rotator = probe_rotator
                rotator = probe_rotator
                print(f"[*] 解析/下载自动换节点已启用: {len(nodes)} 个")

    # ── 代理（直连或手动代理，只有在无代理池且无Clash时使用） ──
    if args.proxy and not proxy_pool and not rotator:
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
        book = None
        last_parse_exc = None
        max_parse_attempts = 8 if parse_rotator else 3
        for attempt in range(1, max_parse_attempts + 1):
            try:
                book = source.parse_book(args.url)
                break
            except Exception as exc:
                last_parse_exc = exc
                print(f"[!] 解析失败({attempt}/{max_parse_attempts}): {exc}")
                if parse_rotator:
                    # 对齐旧版策略: 连续失败后轮换节点，再继续尝试
                    if attempt % 2 == 0:
                        new_node = parse_rotator.rotate()
                        if new_node:
                            print(f"[!] 解析连续失败，切换节点: {new_node}")
                if attempt < max_parse_attempts:
                    time.sleep(min(3 * attempt, 10))
        if book is None:
            raise last_parse_exc
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
        proxy_pool=proxy_pool,
        rotate_interval=args.rotate,
        download_workers=args.download_workers,
        url_fetch_workers=args.url_fetch_workers,
        fast_mode=args.fast,
        batch_fetch=args.batch,
        extra_delay=args.delay,
    )
    engine.run(book, args.output, args.start, args.end)

    if args.save_config:
        save_config({
            "output": args.output,
            "proxy": args.proxy,
            "rotate": args.rotate,
            "headless": not args.no_headless,
            "download_workers": args.download_workers,
            "url_fetch_workers": args.url_fetch_workers,
            "delay": args.delay,
            "proxy_pool_url": args.proxy_pool_url,
            "proxy_pool_key": args.proxy_pool_key,
        })
        print(f"[*] 配置已保存到: ~/.ting13/config.json")


if __name__ == "__main__":
    main()
