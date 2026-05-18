"""
补爬最后5集 (章节26-30) + 验证所有文件
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re, time, requests, urllib3
urllib3.disable_warnings()
from playwright.sync_api import sync_playwright
from ting13.sources.yuetingba import BASE

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

print("🔧 补爬最后5集")
print("=" * 50)

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})
session.verify = False
book_id = "3a20a667-3381-cb4f-f597-92364e9a0039"

# 获取章节26-30的ID
r = session.get(f"{BASE}/book/detail/{book_id}/0", timeout=30)
r.encoding = "utf-8"

all_chapters = {}
for cid, ctitle in re.findall(r'onclick="testFn\(\'([a-f0-9-]+)\'\)"[^>]*>\s*([^<]+)\s*</a>', r.text):
    ctitle = ctitle.strip()
    m = re.match(r'^(\d+)', ctitle)
    if m:
        num = int(m.group(1))
        if 26 <= num <= 30:
            all_chapters[num] = {"chapter_id": cid, "title": ctitle}

for num in sorted(all_chapters):
    print(f"   需要爬取: [{num}] {all_chapters[num]['title']}")

if not all_chapters:
    print("   没有需要爬取的章节")
    exit()

# 启动浏览器
pw = sync_playwright().start()
browser = pw.chromium.launch(
    headless=True,
    args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage']
)
context = browser.new_context(
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    viewport={"width": 1920, "height": 1080},
    locale="zh-CN",
)
page = context.new_page()
page.add_init_script("""
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
    const oq = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : oq(p);
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
""")
page.route("**/*", lambda route: (
    route.abort() if 'disable-devtool' in route.request.url.lower()
    else route.abort() if any(k in route.request.url.lower() for k in ['googleads', 'fundingchoices', 'googlesyndication'])
    else route.continue_()
))

dl = requests.Session()
dl.headers.update({"User-Agent": "Mozilla/5.0", "Referer": BASE + "/"})
dl.verify = False

page.goto(f"{BASE}/book/detail/{book_id}/0", wait_until="domcontentloaded", timeout=30000)
page.wait_for_timeout(6000)

for idx, num in enumerate(sorted(all_chapters)):
    ch = all_chapters[num]
    cid = ch["chapter_id"]
    title = ch["title"]
    print(f"\n  [{num}] {title}")

    captured = []

    def handler(req):
        url = req.url
        if url.startswith('http') and '.mp3' in url.lower() and 'myfiles' in url:
            captured.append(url)

    page.on("request", handler)
    try:
        selector = f'a[onclick*="testFn(\'{cid}\')"]'
        el = page.query_selector(selector)
        if el:
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            el.click()
            page.wait_for_timeout(6000)

        if captured:
            mp3_url = captured[0]
            h = dl.head(mp3_url, timeout=15)
            if h.status_code == 200:
                dr = dl.get(mp3_url, timeout=120, stream=True)
                fpath = os.path.join(OUTPUT_DIR, f"{title}.mp3")
                total = 0
                with open(fpath, "wb") as f:
                    for ck in dr.iter_content(8192):
                        f.write(ck)
                        total += len(ck)
                if total > 50000:
                    print(f"     ✅ {total}B")
                else:
                    os.remove(fpath)
                    print(f"     ❌ 太小:{total}B")
            else:
                print(f"     ❌ HEAD {h.status_code}")
        else:
            print(f"     ❌ 未捕获")
    except Exception as e:
        print(f"     ❌ {e}")
    finally:
        page.remove_listener("request", handler)
    time.sleep(2)

browser.close()
pw.stop()

# ── 最终验证 ──
print(f"\n\n🔍 最终文件列表")
print("=" * 50)
files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.endswith('.mp3')])
total_sz = sum(os.path.getsize(os.path.join(OUTPUT_DIR, f)) for f in files)

# 统计去重后的数量（按文件名去重）
unique = {}
for f in files:
    fpath = os.path.join(OUTPUT_DIR, f)
    sz = os.path.getsize(fpath)
    # 按文件大小+名称匹配去重（去10KB容差）
    base = re.sub(r'^\d+_|^\d{4}_', '', f)
    if base in unique:
        if sz > unique[base]["size"]:
            # 保留大的
            os.remove(unique[base]["path"])
            unique[base] = {"size": sz, "path": fpath, "name": f}
        else:
            os.remove(fpath)
            unique[base]["name"] = min(unique[base]["name"], f)
    else:
        unique[base] = {"size": sz, "path": fpath, "name": f}

print(f"   总文件: {len(files)} → 去重后: {len(unique)}")
print(f"   总大小: {sum(u['size'] for u in unique.values()) / 1024 / 1024:.1f} MB")
for name, info in sorted(unique.items()):
    print(f"   {info['name']} ({info['size']}B)")

print(f"\n✅ 完成")