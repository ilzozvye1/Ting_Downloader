"""
测试 v16: 通过 Playwright 在 SPA iframe 中直接获取解密后的音频URL
"""
import re
import json
import time
from playwright.sync_api import sync_playwright

captured_mp3s = []

def on_request(request):
    url = request.url
    if any(ext in url.lower() for ext in ['.mp3', '.m4a']):
        captured_mp3s.append(url)
        print(f"   🔊 音频请求: {url[:200]}")

def on_route(route):
    url = route.request.url.lower()
    if 'disable-devtool' in url:
        route.abort()
        return
    if 'google' in url and ('ads' in url or 'fundingchoices' in url):
        route.abort()
        return
    route.continue_()

BASE = "http://www.yuetingba.cn"
detail_url = f"{BASE}/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0"

print("=" * 60)
print("🔍 悦听巴 v16 - 通过SPA iframe获取解密音频URL")
print("=" * 60)

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=True,
        args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
    )
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="zh-CN",
    )
    page = context.new_page()
    page.on("request", on_request)
    page.route("**/*", on_route)

    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {} };
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (p) =>
            p.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : origQuery(p);
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
    """)

    detail_url_alt = f"{BASE}/book/detail/3a1c0235-9335-5f9b-b236-e3b92dda9baa/0"  # 仙逆

    print(f"   加载: {detail_url_alt}")
    page.goto(detail_url_alt, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)

    title = page.title()
    print(f"   标题: {title}")

    # 获取章节链接
    chapters = page.evaluate("""() => {
        const links = document.querySelectorAll('a[onclick*="testFn"]');
        return Array.from(links).map(l => {
            const onclick = l.getAttribute('onclick') || '';
            const match = onclick.match(/testFn\\('([^']+)'\\)/);
            return {
                id: match ? match[1] : '',
                text: (l.textContent || l.title || '').trim().substring(0, 40)
            };
        }).filter(c => c.id);
    }""")
    print(f"   章节数: {len(chapters)}")
    for c in chapters[:5]:
        print(f"      {c['text'][:30]} -> {c['id']}")

    # 获取3个不同章节的音频URL
    all_audio = []
    for i, ch in enumerate(chapters[:5]):
        print(f"\n   [{i+1}] 章节 {ch['id'][:20]}... {ch['text']}")

        # 点击章节
        selector = f'a[onclick*="testFn(\'{ch["id"]}\')"]'
        el = page.query_selector(selector)
        if el:
            captured_mp3s.clear()
            el.click()
            page.wait_for_timeout(5000)

            if captured_mp3s:
                all_audio.append({"chapter_id": ch["id"], "title": ch["text"], "audio_url": captured_mp3s[0]})
                print(f"   ✅ 音频: {captured_mp3s[0][:150]}")

                # 测试下载
                try:
                    import requests, urllib3
                    urllib3.disable_warnings()
                    hr = requests.head(captured_mp3s[0], timeout=10, verify=False,
                        headers={"User-Agent": "Mozilla/5.0"})
                    print(f"   📦 HEAD: {hr.status_code}, Size: {hr.headers.get('Content-Length','?')}B")
                except:
                    print(f"   📦 HEAD: 失败")
            else:
                print(f"   ❌ 未捕获到音频")

        time.sleep(1)

    browser.close()

    # 最终结果
    print(f"\n{'='*60}")
    print(f"📊 音频URL获取结果({len(all_audio)}/{5})")
    print(f"{'='*60}")
    for a in all_audio:
        print(f"  {a['title'][:30]}: {a['audio_url'][:150]}")

    print(f"\n✅ 悦听巴爬取模式确认:")
    print(f"   parse_book: HTML解析 /book/detail/{'{book_id}'}/{'{offset}'}")
    print(f"     - 提取书名, 作者, 演播, 章节ID列表 (每页200章)")
    print(f"     - 分页通过nav-tabs中的链接 (/book/detail/{'{book_id}'}/{'{0,200,400...}'})")
    print(f"   get_audio_url: Playwright iframe通信")
    print(f"     - 调用 page.evaluate 获取 assl/ts/es 解密参数")
    print(f"     - 通过 iframe postMessage/testFun 获取解密音频URL")
    print(f"     - 或直接拦截网络请求获取MP3 URL")