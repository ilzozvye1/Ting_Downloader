"""
补下载章节 2, 3, 26
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re, time, requests, urllib3
urllib3.disable_warnings()
from playwright.sync_api import sync_playwright
from ting13.sources.yuetingba import BASE

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})
session.verify = False

book_id = "3a20a667-3381-cb4f-f597-92364e9a0039"

# 从HTML获取章节ID
r = session.get(f"{BASE}/book/detail/{book_id}/0", timeout=30)
r.encoding = "utf-8"

# 需要下载的标题
need = {"0002_寻仙", "0003_拒仙（1）", "0026_人心（1）"}
chapters_to_dl = []
for cid, ctitle in re.findall(r'onclick="testFn\(\'([a-f0-9-]+)\'\)"[^>]*>\s*([^<]+)\s*</a>', r.text):
    ctitle = ctitle.strip()
    if ctitle in need:
        chapters_to_dl.append({"chapter_id": cid, "title": ctitle})
        print(f"   找到: {ctitle}")

if not chapters_to_dl:
    print("   未找到需要下载的章节")
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
    else route.continue_()
))

dl = requests.Session()
dl.headers.update({"User-Agent": "Mozilla/5.0", "Referer": BASE + "/"})
dl.verify = False

page.goto(f"{BASE}/book/detail/{book_id}/0", wait_until="domcontentloaded", timeout=30000)
page.wait_for_timeout(5000)

for ch in chapters_to_dl:
    cid = ch["chapter_id"]
    title = ch["title"]
    print(f"\n  [{title}]")

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

# 最终验证
expected = [f for f in [
    "0001_序章 缘起.mp3","0002_寻仙.mp3","0003_拒仙（1）.mp3","0004_拒仙（2）.mp3",
    "0005_不信仙（1）.mp3","0006_不信仙（2）.mp3","0007_闯院.mp3","0008_方士（1）.mp3",
    "0009_方士（2）.mp3","0010_除虎（1）.mp3","0011_除虎（2）.mp3","0012_除虎（3）.mp3",
    "0013_出山.mp3","0014_道观（1）.mp3","0015_道观（2）.mp3","0016_蛛妖.mp3",
    "0017_相助（1）.mp3","0018_相助（2）.mp3","0019_责任（1）.mp3","0020_责任（2）.mp3",
    "0021_悬榜（1）.mp3","0022_悬榜（2）.mp3","0023_查案.mp3","0024_道姑（1）.mp3",
    "0025_道姑（2）.mp3","0026_人心（1）.mp3","0027_人心（2）.mp3","0028_惊变.mp3",
    "0029_明河（1）.mp3","0030_明河（2）.mp3",
]]

exist = [f for f in expected if os.path.exists(os.path.join(OUTPUT_DIR, f))]
missing3 = [f for f in expected if f not in exist]
print(f"\n   ✅ {len(exist)}/30 集")
if missing3:
    print(f"   ❌ 仍缺失: {missing3}")
else:
    print(f"   🎉 30集全部下载完成!")

print(f"\n✅ 完成")