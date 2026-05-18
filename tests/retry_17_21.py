"""补17和21"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import re, time, requests, urllib3
urllib3.disable_warnings()
from playwright.sync_api import sync_playwright
from ting13.sources.yuetingba import BASE

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

r = requests.get(f"{BASE}/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0",
    headers={"User-Agent": "Mozilla/5.0"}, verify=False, timeout=30)
r.encoding = "utf-8"

need = {"0017_相助（1）", "0021_悬榜（1）"}
to_dl = []
for cid, ct in re.findall(r'onclick="testFn\(\'([a-f0-9-]+)\'\)"[^>]*>\s*([^<]+)\s*</a>', r.text):
    if ct.strip() in need:
        to_dl.append({"chapter_id": cid, "title": ct.strip()})

pw = sync_playwright().start()
browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
ctx = browser.new_context(
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    viewport={"width": 1920, "height": 1080}, locale="zh-CN")
page = ctx.new_page()
page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>false});window.chrome={runtime:{}};")
page.route("**/*", lambda route: route.abort() if 'disable-devtool' in route.request.url.lower() else route.continue_())
page.goto(f"{BASE}/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0", wait_until="domcontentloaded", timeout=30000)
page.wait_for_timeout(5000)

dl = requests.Session()
dl.headers.update({"User-Agent": "Mozilla/5.0", "Referer": BASE + "/"})
dl.verify = False

for ch in to_dl:
    cid = ch["chapter_id"]; title = ch["title"]
    captured = []
    def h(req):
        if req.url.startswith('http') and '.mp3' in req.url and 'myfiles' in req.url:
            captured.append(req.url)
    page.on("request", h)
    try:
        el = page.query_selector(f'a[onclick*="testFn(\'{cid}\')"]')
        if el: el.scroll_into_view_if_needed(); page.wait_for_timeout(200); el.click(); page.wait_for_timeout(6000)
        if captured:
            hr = dl.head(captured[0], timeout=15)
            if hr.status_code == 200:
                dr = dl.get(captured[0], timeout=120, stream=True)
                t = 0
                with open(os.path.join(OUTPUT_DIR, f"{title}.mp3"), "wb") as f:
                    for ck in dr.iter_content(8192): f.write(ck); t += len(ck)
                print(f"   {title}: ✅ {t}B")
            else: print(f"   {title}: ❌ HEAD {hr.status_code}")
        else: print(f"   {title}: ❌")
    except Exception as e: print(f"   {title}: ❌ {e}")
    finally: page.remove_listener("request", h)
    time.sleep(2)

browser.close(); pw.stop()

# 最终验证
files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith('.mp3')]
print(f"\n   📊 总计: {len(files)} 文件")
for f in sorted(files):
    sz = os.path.getsize(os.path.join(OUTPUT_DIR, f))
    print(f"   {f} ({sz/1024:.0f}KB)")

print(f"\n✅ 完成")