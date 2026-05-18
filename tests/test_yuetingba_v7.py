"""
测试 v7：悦听巴 - 检查SPA加载错误 + 尝试非headless
"""
import re
import json
import time
from playwright.sync_api import sync_playwright

captured = []
console_msgs = []

def on_response(response):
    url = response.url
    ct = response.headers.get('content-type', '')
    if 'json' in ct:
        try:
            body = response.text()
            captured.append({"url": url, "body": body[:800]})
        except:
            pass

def on_console(msg):
    console_msgs.append(f"[{msg.type}] {msg.text[:150]}")

BASE = "http://www.yuetingba.cn"
book_url = f"{BASE}/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0"

print("🔍 悦听巴 v7 - 诊断SPA加载")
print("=" * 60)

with sync_playwright() as pw:
    # 尝试非headless来看看实际渲染效果
    browser = pw.chromium.launch(headless=True, args=[
        '--disable-blink-features=AutomationControlled',
        '--no-sandbox',
    ])
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    page = context.new_page()
    page.on("response", on_response)
    page.on("console", on_console)

    # 先访问首页看看
    print("\n--- 首页测试 ---")
    try:
        page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
        home_html = page.content()
        print(f"首页 HTML 长度: {len(home_html)}")
        title = page.title()
        print(f"首页 title: {title}")
    except Exception as e:
        print(f"首页加载异常: {e}")

    # 访问书籍详情页
    print("\n--- 书籍详情页 ---")
    try:
        page.goto(book_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)
        detail_html = page.content()
        print(f"详情 HTML 长度: {len(detail_html)}")
        title = page.title()
        print(f"详情 title: {title}")

        # 检查body内容
        body_text = page.evaluate("() => document.body.innerText.substring(0, 500)")
        print(f"body text: {body_text}")

        # 查找所有链接
        links_info = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a')).map(a => ({
                href: a.getAttribute('href') || '',
                text: (a.textContent || '').trim().substring(0, 40),
                classes: a.className || ''
            }))
        }""")
        print(f"页面链接总数: {len(links_info)}")
        for l in links_info[:20]:
            print(f"  [{l['classes'][:20]}] {l['text'][:30]} -> {l['href'][:80]}")

    except Exception as e:
        print(f"详情页加载异常: {e}")

    # 截图看效果
    page.screenshot(path="e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_screenshot.png")
    print(f"\n截图已保存")

    browser.close()

# 打印控制台消息
print(f"\n📝 控制台消息 ({len(console_msgs)} 条):")
for msg in console_msgs[:20]:
    print(f"  {msg}")

# 打印API
print(f"\n📡 JSON API ({len(captured)} 个):")
for i, api in enumerate(captured):
    print(f"  [{i}] {api['url'][:120]}")
    try:
        data = json.loads(api['body'])
        if isinstance(data, dict):
            print(f"      keys: {list(data.keys())[:5]}")
    except:
        print(f"      body: {api['body'][:150]}")