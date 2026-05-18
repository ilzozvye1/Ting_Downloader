"""
测试 v5：用 Playwright 访问悦听巴 SPA，捕获 API 调用
"""
import re
import json
import time

try:
    from playwright.sync_api import sync_playwright
    _HAS_PW = True
except ImportError:
    _HAS_PW = False
    print("❌ Playwright 未安装")

if not _HAS_PW:
    exit(1)

# 收集所有 XHR/Fetch 请求的响应
captured_apis = []

def on_response(response):
    url = response.url
    if any(k in url for k in ['api', 'play', 'chapter', 'track', 'episode', 'get']):
        try:
            ct = response.headers.get('content-type', '')
            if 'json' in ct or 'text' in ct:
                body = response.text()
                captured_apis.append({
                    "url": url,
                    "status": response.status,
                    "body_preview": body[:500],
                })
        except:
            pass

def on_request(request):
    url = request.url
    if any(k in url for k in ['api', 'play', 'chapter', 'track', 'episode']):
        print(f"   REQ: {url[:120]}")

BASE = "http://www.yuetingba.cn"
book_url = f"{BASE}/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0"

print("=" * 60)
print("🔍 悦听巴 - Playwright SPA 分析")
print(f"   URL: {book_url}")
print("=" * 60)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("response", on_response)
    page.on("request", on_request)

    print("\n   加载页面...")
    page.goto(book_url, wait_until="networkidle", timeout=30000)
    time.sleep(3)  # 等SPA渲染

    # 获取渲染后的 HTML
    html = page.content()
    with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_spa.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n   📄 渲染后 HTML 长度: {len(html)}")

    # 找章节链接
    chapter_links = re.findall(r'<a[^>]*href=["\']([^"\']*play[^"\']*)["\'][^>]*>([^<]+)</a>', html)
    if chapter_links:
        print(f"   📋 找到 {len(chapter_links)} 个 play 链接:")
        for href, text in chapter_links[:5]:
            print(f"      - {text[:40]} -> {href}")
    else:
        # 更宽松的搜索
        all_links = re.findall(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>\s*第?\s*(\d+)\s*[集章节]?\s*([^<]*)</a>', html)
        if all_links:
            print(f"   📋 找到 {len(all_links)} 个序号链接:")
            for href, num, rest in all_links[:5]:
                print(f"      - 第{num}{rest[:10]} -> {href}")

        # 试试找 audio 标签
        audios = re.findall(r'<audio[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if audios:
            print(f"   🎵 Audio 标签: {audios[:3]}")
        else:
            sources = re.findall(r'<source[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if sources:
                print(f"   🎵 Source 标签: {sources[:3]}")

    browser.close()

# 打印捕获的 API
print(f"\n   📡 捕获的 API ({len(captured_apis)} 个):")
for api in captured_apis:
    print(f"      [{api['status']}] {api['url'][:120]}")
    if api['body_preview']:
        try:
            body = json.loads(api['body_preview'])
            if isinstance(body, dict):
                keys = list(body.keys())[:5]
                print(f"         JSON keys: {keys}")
                # 看看有没有 tracks/chapters
                for k in ['data', 'tracks', 'chapters', 'list', 'result']:
                    if k in body:
                        val = body[k]
                        if isinstance(val, list):
                            print(f"         {k}: {len(val)} items")
                            if val:
                                print(f"         first: {json.dumps(val[0], ensure_ascii=False)[:200]}")
                        elif isinstance(val, dict):
                            print(f"         {k}: {list(val.keys())[:5]}")
        except:
            print(f"         body: {api['body_preview'][:200]}")