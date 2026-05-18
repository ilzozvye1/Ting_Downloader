"""
测试 v13: 用Playwright+项目已有方法拦截悦听巴音频URL
使用项目 ting13.py 中的 Playwright 配置模式
"""
import re
import json
import time
import os

# 检查是否安装了 playwright stealth
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌ 需要安装 playwright: pip install playwright && playwright install chromium")
    exit(1)

# 尝试导入 playwright_stealth
try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("⚠️  playwright_stealth 未安装，尝试基本模式")

captured_audio_urls = []
captured_all_jsons = []

def on_response(response):
    url = response.url
    # 捕获所有 JSON 响应
    ct = response.headers.get('content-type', '')
    if 'json' in ct or 'javascript' in ct:
        try:
            body = response.text()
            if len(body) > 10:  # 长度过滤
                captured_all_jsons.append({"url": url, "body": body[:800]})
        except:
            pass
    # 捕获音频文件请求
    if any(ext in url.lower() for ext in ['.mp3', '.m4a', '.m3u8', '.ogg', 'audio/', 'media/', 'stream/']):
        captured_audio_urls.append(url)

def on_request(request):
    url = request.url
    if any(ext in url.lower() for ext in ['.mp3', '.m4a', '.m3u8']):
        print(f"   🔊 音频请求: {url[:150]}")

BASE = "http://www.yuetingba.cn"
detail_url = f"{BASE}/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0"

print("=" * 60)
print("🔍 悦听巴 - Playwright 完整测试 (音频捕获)")
print("=" * 60)

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=True,
        args=[
            '--disable-blink-features=AutomationControlled',
            '--disable-features=IsolateOrigins,site-per-process',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-infobars',
            '--disable-dev-shm-usage',
            '--disable-extensions',
        ]
    )
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="zh-CN",
    )

    page = context.new_page()

    # 如果可用，使用 stealth
    if HAS_STEALTH:
        stealth_sync(page)

    page.on("response", on_response)
    page.on("request", on_request)

    # 注入JS：隐藏webdriver属性
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
            Promise.resolve({state: Notification.permission}) :
            originalQuery(parameters)
        );
        window.chrome = {runtime: {}};
    """)

    print("\n   加载书籍详情页...")
    try:
        page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)

        html = page.content()
        print(f"   渲染后HTML: {len(html)} 字符")
        title = page.title()
        print(f"   页面标题: {title}")
        print(f"   body文本: {page.evaluate('() => document.body.innerText.substring(0, 200)')}")

        # 查找章节链接
        chapter_links = page.evaluate("""() => {
            const links = document.querySelectorAll('a[onclick*="testFn"]');
            return Array.from(links).map(l => ({
                onclick: l.getAttribute('onclick'),
                text: l.textContent.trim().substring(0, 30)
            }));
        }""")
        print(f"\n   章节链接: {len(chapter_links)} 个")
        for l in chapter_links[:5]:
            print(f"      {l['text'][:30]} -> {l['onclick'][:100]}")

        if chapter_links:
            # 点击第一个章节
            first = chapter_links[0]['onclick']
            ting_id = re.search(r"testFn\('([^']+)'\)", first)
            if ting_id:
                tid = ting_id.group(1)
                print(f"\n   点击章节: {tid}")

                # 通过 window 变量获取 assl
                assl = page.evaluate("() => window.assl || document.querySelector('script').textContent.match(/var assl = '([^']+)'/)")
                print(f"   assl变量: {str(assl)[:100] if assl else 'NOT FOUND'}")

                # 获取页面中的变量
                page_vars = page.evaluate("""() => {
                    const scripts = document.querySelectorAll('script');
                    let result = {};
                    for (const s of scripts) {
                        const match_tingId = s.textContent.match(/var tingId = '([^']+)'/);
                        const match_assl = s.textContent.match(/var assl = '([^']+)'/);
                        const match_ts = s.textContent.match(/var ts ='([^']+)'/);
                        const match_es = s.textContent.match(/var es ='([^']+)'/);
                        if (match_tingId) result.tingId = match_tingId[1];
                        if (match_assl) result.assl = match_assl[1].substring(0, 50) + '...';
                        if (match_ts) result.ts = match_ts[1];
                        if (match_es) result.es = match_es[1];
                    }
                    return result;
                }""")
                print(f"   页面变量: {json.dumps(page_vars, ensure_ascii=False)}")

                # 点击章节触发 testFn
                # 找到第一个章节的 a 标签并点击
                element = page.query_selector('a[onclick*="testFn"]')
                if element:
                    element.click()
                    page.wait_for_timeout(8000)

                    # 再次检查是否捕获到音频
                    print(f"\n   捕获到音频URL: {len(captured_audio_urls)} 个")
                    for au in captured_audio_urls:
                        print(f"      {au}")

        print(f"\n   捕获到JSON响应: {len(captured_all_jsons)} 个")
        for i, j in enumerate(captured_all_jsons):
            url_short = j['url'].replace(BASE, '').replace('http://106.13.91.31:43134', '')
            print(f"   [{i}] {url_short[:100]}")
            if any(k in j['body'].lower() for k in ['mp3', 'm4a', 'audio', 'src', 'url']):
                print(f"       body: {j['body'][:300]}")

        page.screenshot(path="e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_playwright.png")

    except Exception as e:
        print(f"   ❌ 异常: {e}")
        import traceback
        traceback.print_exc()

    browser.close()

print(f"\n📊 最终结果:")
print(f"   音频URL捕获: {len(captured_audio_urls)} 个")
print(f"   JSON响应捕获: {len(captured_all_jsons)} 个")
print(f"\n✅ 完成")