"""
悦听巴 30集爬取测试 v4 — 每5集重启浏览器 + 增加延迟 + 双重策略
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re, time, requests, urllib3
urllib3.disable_warnings()
from playwright.sync_api import sync_playwright
from ting13.sources.yuetingba import BASE

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

print("=" * 70)
print("  悦听巴 yuetingba.cn — 30集爬取测试 v4 (每5章重启浏览器)")
print("=" * 70)

# ── Step 1: parse all chapters ──
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
session.verify = False

book_id = "3a20a667-3381-cb4f-f597-92364e9a0039"
r = session.get(f"{BASE}/book/detail/{book_id}/0", timeout=30)
r.encoding = "utf-8"
html = r.text

offsets = set([0])
for om in re.findall(rf'/book/detail/{re.escape(book_id)}/(\d+)', html):
    offsets.add(int(om))
offsets = sorted(offsets)

all_chapters = []
for offset in offsets:
    if offset != 0:
        time.sleep(0.2)
        r = session.get(f"{BASE}/book/detail/{book_id}/{offset}", timeout=30)
        r.encoding = "utf-8"
        ph = r.text
    else:
        ph = html

    matches = re.findall(r'onclick="testFn\(\'([a-f0-9-]+)\'\)"[^>]*>\s*([^<]+)\s*</a>', ph)
    for cid, ctitle in matches:
        ctitle = ctitle.strip()
        if ctitle:
            all_chapters.append({"chapter_id": cid, "title": ctitle})

def extract_num(title):
    m = re.match(r'^(\d+)', title)
    return int(m.group(1)) if m else 99999

all_chapters.sort(key=lambda c: extract_num(c["title"]))
chapters = all_chapters[:30]

print(f"   书名: 问道红尘, 总集数: {len(all_chapters)}, 取前30集")
for ch in chapters[:5]:
    print(f"   [{extract_num(ch['title'])}] {ch['title']}")
print(f"   ...")
print(f"   [{extract_num(chapters[-1]['title'])}] {chapters[-1]['title']}")

# ── 爬取函数 ──
BATCH_SIZE = 5

# 创建session用于下载
dl = requests.Session()
dl.headers.update({"User-Agent": "Mozilla/5.0", "Referer": BASE + "/"})
dl.verify = False

def start_browser():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage']
    )
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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
    return pw, browser, context, page

def process_chapter(page, ch_info, idx, total):
    """处理单个章节: 点击 → 捕获MP3 → 下载"""
    cid = ch_info["chapter_id"]
    title = ch_info["title"]
    print(f"    [{idx}/{total}] {title}")

    captured = []

    def handler(request):
        url = request.url
        if url.startswith('http') and '.mp3' in url.lower() and 'myfiles' in url:
            captured.append(url)

    page.on("request", handler)
    try:
        # 点击章节
        selector = f'a[onclick*="testFn(\'{cid}\')"]'
        el = page.query_selector(selector)
        if el:
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            el.click()
            page.wait_for_timeout(5000)
        else:
            # JS调用
            page.evaluate(f"""
                (function() {{
                    const iframe = document.getElementById('iframe_tingPlay');
                    if (iframe && iframe.contentWindow && typeof iframe.contentWindow.testFun === 'function') {{
                        try {{ iframe.contentWindow.testFun('{cid}'); }} catch(e) {{}}
                    }}
                }})()
            """)
            page.wait_for_timeout(6000)

        if captured:
            mp3_url = captured[0]
            h = dl.head(mp3_url, timeout=15)
            if h.status_code == 200:
                sz = h.headers.get("Content-Length", "?")
                dr = dl.get(mp3_url, timeout=120, stream=True)
                fname = title.replace('/', '_').replace('\\', '_').replace(':', '_').replace('?', '_')
                fpath = os.path.join(OUTPUT_DIR, f"{fname}.mp3")
                total = 0
                with open(fpath, "wb") as f:
                    for ck in dr.iter_content(8192):
                        f.write(ck)
                        total += len(ck)
                if total > 50000:
                    print(f"           ✅ {total}B")
                    return True
                else:
                    os.remove(fpath)
                    print(f"           ❌ 太小:{total}B")
            else:
                print(f"           ❌ HEAD {h.status_code}")
        else:
            print(f"           ❌ 未捕获MP3")
    except Exception as e:
        print(f"           ❌ 异常:{e}")
    finally:
        page.remove_listener("request", handler)
    return False

# ── 主循环 ──
print(f"\n📥 开始爬取 (每{BATCH_SIZE}集重启浏览器)")
print("-" * 50)

success = 0
fail = 0
failed = []

for batch_start in range(0, len(chapters), BATCH_SIZE):
    batch = chapters[batch_start:batch_start + BATCH_SIZE]
    batch_num = batch_start // BATCH_SIZE + 1
    print(f"\n  🔄 批次 {batch_num}: 集 {extract_num(batch[0]['title'])} - {extract_num(batch[-1]['title'])}")

    # 启动新浏览器
    pw, browser, context, page = start_browser()
    try:
        detail_url = f"{BASE}/book/detail/{book_id}/0"
        page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)

        for i, ch in enumerate(batch):
            result = process_chapter(page, ch, batch_start + i + 1, len(chapters))
            if result:
                success += 1
            else:
                fail += 1
                failed.append(ch)
            time.sleep(2)  # 章节间大延迟

    finally:
        browser.close()
        pw.stop()

    if batch_start + BATCH_SIZE < len(chapters):
        time.sleep(3)  # 批次间延迟

# ── 总结 ──
print(f"\n\n{'='*70}")
print(f"  📊 最终结果")
print(f"{'='*70}")
print(f"  ✅ {success}/30  |  ❌ {fail}/30")
print(f"  📁 {OUTPUT_DIR}")

files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.endswith('.mp3')])
if files:
    total_sz = sum(os.path.getsize(os.path.join(OUTPUT_DIR, f)) for f in files)
    print(f"  📦 {len(files)} 文件, {total_sz/1024/1024:.1f} MB")
    for f in files:
        sz = os.path.getsize(os.path.join(OUTPUT_DIR, f))
        print(f"     {f} ({sz}B)")

if failed:
    print(f"\n  ⚠️  失败: {len(failed)}集")
    for ch in failed[:10]:
        print(f"     {ch['title']}")

print(f"\n✅ 完成")