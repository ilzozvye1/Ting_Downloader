"""
测试 v11: 用多种URL模式猜测悦听巴的音频API
同时直接测试实际音频文件是否能下载
"""
import requests
import re
import ssl
import json
import time
import urllib3
urllib3.disable_warnings()
from requests.adapters import HTTPAdapter

class WeakTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "http://www.yuetingba.cn",
    "Referer": "http://www.yuetingba.cn/",
})
s.verify = False
s.mount("https://", WeakTLSAdapter())

BASE = "http://www.yuetingba.cn"

print("=" * 60)
print("🔍 悦听巴 - 系统化API探测 + 音频直接下载测试")
print("=" * 60)

book_id = "3a20a667-3381-cb4f-f597-92364e9a0039"
chapter_id = "3a20a68a-d9e0-ce93-6c13-4dc2f86c96b6"

# ── API 探测 ──
apis_to_try = [
    # 可能的书籍API
    f"{BASE}/api/book/detail/{book_id}",
    f"{BASE}/api/Book/Detail/{book_id}",
    f"{BASE}/api/book/Detail/{book_id}",
    f"{BASE}/api/bookdetail/{book_id}",

    # 可能的章节API
    f"{BASE}/api/book/chapter/{chapter_id}",
    f"{BASE}/api/book/ting/{chapter_id}",
    f"{BASE}/api/Book/Ting/{chapter_id}",
    f"{BASE}/api/BookTing/{chapter_id}",
    f"{BASE}/api/ting/{chapter_id}",
    f"{BASE}/api/sound/{chapter_id}",
    f"{BASE}/api/play/{chapter_id}",
    f"{BASE}/api/getPlayUrl/{chapter_id}",
    f"{BASE}/api/getAudio/{chapter_id}",
    f"{BASE}/api/getaudio/{chapter_id}",
    f"{BASE}/api/track/{chapter_id}",
    f"{BASE}/api/audio/{chapter_id}",
    f"{BASE}/api/media/{chapter_id}",

    # 可能的列表API
    f"{BASE}/api/book/chapters/{book_id}",
    f"{BASE}/api/book/list/{book_id}",
    f"{BASE}/api/chapters/{book_id}",
    f"{BASE}/api/book/tracklist/{book_id}",
    f"{BASE}/api/book/tracks/{book_id}",

    # 直接尝试不带API的路径
    f"{BASE}/book/getPlay?bookId={book_id}&tingId={chapter_id}",
    f"{BASE}/book/ting/json/{chapter_id}",
    f"{BASE}/book/ting/play/{chapter_id}",
    f"{BASE}/book/Ting?tingId={chapter_id}",
]

print("\n📡 API探测 (JSON响应)")
found_apis = []
for i, api in enumerate(apis_to_try):
    try:
        r = s.get(api, timeout=10)
        ct = r.headers.get("Content-Type", "")
        if "json" in ct and r.status_code == 200:
            try:
                data = r.json()
                found_apis.append({"url": api, "data": data})
                print(f"   ✅ [{i}] {api} -> JSON {len(r.text)}B")
            except:
                pass
        elif r.status_code == 200 and len(r.text) > 0 and r.text.strip()[0] in '{"[':
            try:
                data = json.loads(r.text)
                found_apis.append({"url": api, "data": data})
                print(f"   ✅ [{i}] {api} -> JSON {len(r.text)}B")
            except:
                pass
    except:
        pass

if found_apis:
    print(f"\n   找到 {len(found_apis)} 个JSON API:")
    for api in found_apis:
        print(f"      {api['url']}")
        d = api['data']
        if isinstance(d, dict):
            for k, v in list(d.items())[:5]:
                v_str = str(v)[:100]
                print(f"         {k}: {v_str}")
else:
    print(f"\n   ❌ 未找到JSON API")

# ── 直接测试音频文件下载 ──
print(f"\n🎵 直接测试音频下载")
# 根据CDN路径模式猜测
# 从图片路径: http://106.13.91.31:43134/myfiles/host/listen/2026/4/16/e5e5aac86ac64053a057f8257d83a16b_thumb.webp
# 猜测音频路径模式: http://106.13.91.31:43134/myfiles/host/listen/{year}/{month}/{day}/{hash}.mp3
# 或: http://106.13.91.31:43134/myfiles/host/listen/{book_hash}/chapter_{number}.mp3

# 从章节页的引用URL猜测
# 章节UUID: 3a20a68a-d9e0-ce93-6c13-4dc2f86c96b6
# 尝试直接访问带有这个UUID的文件
test_urls = [
    f"http://106.13.91.31:43134/myfiles/host/listen/{chapter_id}.mp3",
    f"http://106.13.91.31:43134/myfiles/host/listen/{chapter_id}.m4a",
    f"http://106.13.91.31:43134/myfiles/host/listen/{chapter_id.replace('-', '')}.mp3",
    f"http://106.13.91.31:43134/myfiles/host/listen/{chapter_id}/audio.mp3",
    f"http://106.13.91.31:43134/myfiles/host/listen/{chapter_id}/index.m3u8",
]

for tu in test_urls:
    try:
        r = s.head(tu, timeout=10)
        if r.status_code == 200:
            cl = r.headers.get("Content-Length", "N/A")
            ct = r.headers.get("Content-Type", "N/A")
            print(f"   ✅ {tu}: 200, Size={cl}, Type={ct}")
        elif r.status_code < 400:
            print(f"   ⚠️ {tu}: {r.status_code}")
    except:
        pass

print(f"\n✅ 探测完成")
print(f"\n📝 总结: 悦听巴使用SPA + iframe, 音频URL通过uniapp打包的JS动态获取。")
print(f"   需要进一步逆向 pages-book-bookplay.DVPVQF4m.js 或使用Playwright拦截网络请求。")