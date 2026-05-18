"""
测试 v6：捕获悦听巴所有 API 请求，不做过滤
"""
import re
import json
import time
from playwright.sync_api import sync_playwright

captured = []

def on_response(response):
    url = response.url
    ct = response.headers.get('content-type', '')
    if 'json' in ct:
        try:
            body = response.text()
            captured.append({"url": url, "body": body[:600]})
        except:
            pass

BASE = "http://www.yuetingba.cn"
book_url = f"{BASE}/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0"

print("=" * 60)
print("🔍 悦听巴 - 全量 API 捕获")
print("=" * 60)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("response", on_response)

    page.goto(book_url, wait_until="domcontentloaded", timeout=60000)
    # 滚动到底部触发懒加载
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(5)
    page.wait_for_timeout(3000)

    html = page.content()
    print(f"\n   渲染 HTML 长度: {len(html)}")
    print(f"   HTML 前 500 字符: {html[:500]}")

    # 找所有可点击的链接
    links = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a[href]')).map(a => ({
            href: a.href,
            text: a.textContent.trim().substring(0, 30)
        })).filter(l => l.href.includes('play') || l.href.includes('chapter') || l.href.includes('book'))
    }""")
    print(f"\n   含 play/chapter/book 的页面链接: {len(links)} 个")
    for l in links[:10]:
        print(f"      {l['text'][:30]} -> {l['href'][:100]}")

    browser.close()

# 分析捕获的 API
print(f"\n   📡 JSON API ({len(captured)} 个):")
for i, api in enumerate(captured):
    url_short = api['url'].replace(BASE, '')
    print(f"   [{i}] {url_short[:100]}")
    try:
        data = json.loads(api['body'])
        if isinstance(data, dict):
            top_keys = list(data.keys())[:6]
            print(f"       keys: {top_keys}")
            for k in top_keys:
                v = data[k]
                if isinstance(v, list):
                    print(f"       {k}: list[{len(v)}]")
                    if v and isinstance(v[0], dict):
                        print(f"       {k}[0]: {json.dumps(v[0], ensure_ascii=False)[:200]}")
                elif isinstance(v, dict):
                    subkeys = list(v.keys())[:5]
                    print(f"       {k}: dict keys={subkeys}")
                else:
                    print(f"       {k}: {str(v)[:100]}")
    except:
        print(f"       raw: {api['body'][:200]}")