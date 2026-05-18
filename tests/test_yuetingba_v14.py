"""
测试 v14: 拦截 anti-devtool 脚本 + 注入反检测
"""
import re
import json
import time
from playwright.sync_api import sync_playwright

captured_audio_urls = []
captured_apis = []

def on_response(response):
    url = response.url
    # 排除已知的非API响应
    skip_patterns = ['baidu.com', 'google', 'gtag', 'adsbygoogle', 'fundingchoices',
                      'disable-devtool', 'bootstrap', 'jquery', 'site.js', 'qs.js']
    if any(s in url.lower() for s in skip_patterns):
        return
    try:
        ct = response.headers.get('content-type', '')
        body = response.text()
        if len(body) > 10 and len(body) < 50000:
            captured_apis.append({"url": url, "body": body[:500]})
    except:
        pass

def on_request(request):
    url = request.url
    if any(ext in url.lower() for ext in ['.mp3', '.m4a', '.m3u8', '/api/']):
        print(f"   🔊 REQ: {url[:150]}")
        captured_audio_urls.append(url)

def on_route(route):
    url = route.request.url.lower()
    # 拦截 anti-devtool 脚本
    if 'disable-devtool' in url:
        print(f"   🚫 已拦截 anti-devtool: {route.request.url[:80]}")
        route.abort()
        return
    # 拦截 Google 广告脚本
    if 'google' in url and ('ads' in url or 'fundingchoices' in url):
        route.abort()
        return
    route.continue_()

BASE = "http://www.yuetingba.cn"
detail_url = f"{BASE}/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0"

print("=" * 60)
print("🔍 悦听巴 v14 - 拦截 anti-devtool + 注入反检测")
print("=" * 60)

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=True,
        args=[
            '--disable-blink-features=AutomationControlled',
            '--disable-features=IsolateOrigins,site-per-process',
            '--no-sandbox',
            '--disable-dev-shm-usage',
        ]
    )
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="zh-CN",
    )

    page = context.new_page()
    page.on("response", on_response)
    page.on("request", on_request)

    # 拦截路由 - 阻止 anti-devtool 等脚本
    page.route("**/*", on_route)

    # 反检测注入
    page.add_init_script("""
        // 隐藏 webdriver
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        // 添加 chrome 对象
        window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {} };
        // 覆盖 permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
            Promise.resolve({state: Notification.permission}) :
            originalQuery(parameters)
        );
        // 覆盖 plugins
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5], });
        // 覆盖 languages
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'], });
        // 禁用 webdriver 检测
        delete navigator.__proto__.webdriver;
    """)

    print("\n   加载书籍详情页...")
    try:
        page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)

        html = page.content()
        print(f"   渲染后HTML长度: {len(html)}")
        title = page.title()
        print(f"   页面标题: {title}")

        if len(html) > 100:
            # 提取章节信息
            chapters = page.evaluate("""() => {
                const links = document.querySelectorAll('a[onclick*="testFn"]');
                return Array.from(links).map(l => ({
                    id: l.getAttribute('onclick'),
                    text: (l.textContent || '').trim().substring(0, 40),
                    title: l.getAttribute('title') || ''
                }));
            }""")
            print(f"\n   ✅ 章节链接: {len(chapters)} 个")
            for c in chapters[:5]:
                print(f"      {c['text'][:40]} -> {c['id'][:80]}")

            # 提取页面变量
            page_vars = page.evaluate("""() => {
                const scripts = document.querySelectorAll('script');
                let result = {};
                for (const s of scripts) {
                    const mId = s.textContent.match(/var tingId = '([^']+)'/);
                    const mAssl = s.textContent.match(/var assl = '([^]+?)';s*var/);
                    const mTs = s.textContent.match(/var ts ='([^']+)'/);
                    const mEs = s.textContent.match(/var es ='([^']+)'/);
                    if (mId) result.tingId = mId[1];
                    if (mTs) result.ts = mTs[1];
                    if (mEs) result.es = mEs[1];
                }
                return result;
            }""")
            print(f"   页面变量: {json.dumps(page_vars, ensure_ascii=False)}")

            # 检查 iframe 是否加载
            iframe_src = page.evaluate("""() => {
                const iframe = document.getElementById('iframe_tingPlay');
                return iframe ? iframe.getAttribute('src') : null;
            }""")
            print(f"   iframe src: {iframe_src}")

            if chapters:
                # 点击第一个章节
                first_chapter = page.query_selector('a[onclick*="testFn"]')
                if first_chapter:
                    print(f"\n   点击第一个章节...")
                    first_chapter.click()
                    page.wait_for_timeout(10000)

                    # 再次检查
                    post_click_iframe = page.evaluate("""() => {
                        const iframe = document.getElementById('iframe_tingPlay');
                        return iframe ? iframe.getAttribute('src') : null;
                    }""")
                    print(f"   点击后 iframe src: {post_click_iframe}")
        else:
            print(f"   ❌ 页面仍然被阻止 (只有 {len(html)} 字符)")
            # 截图看效果
            page.screenshot(path="e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_blocked.png")

    except Exception as e:
        print(f"   ❌ 异常: {e}")
        import traceback
        traceback.print_exc()

    browser.close()

print(f"\n📊 最终结果:")
print(f"   音频URL: {len(captured_audio_urls)} 个,  " + str(captured_audio_urls[:3]))
print(f"   捕获API: {len(captured_apis)} 个")
for api in captured_apis:
    print(f"      [{api['url'][:80]}] {api['body'][:150]}")
print("\n✅ 完成")