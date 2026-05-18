"""
测试 v10：获取悦听巴 SPA iframe (/tingpage/index.html) 并找API
"""
import requests
import re
import ssl
import json
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})
s.verify = False
s.mount("https://", WeakTLSAdapter())

BASE = "http://www.yuetingba.cn"

# Step 1: 获取 SPA index.html
print("=" * 60)
print("🔍 获取 SPA iframe")
print(f"   {BASE}/tingpage/index.html")
print("=" * 60)

resp = s.get(f"{BASE}/tingpage/index.html", timeout=30)
print(f"   HTTP {resp.status_code}, 长度: {len(resp.text)}")
with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_spa_index.html", "w", encoding="utf-8") as f:
    f.write(resp.text)

# 从SPA index中找JS文件引用
scripts = re.findall(r'<script[^>]*src=["\']([^"\']+)["\']', resp.text)
print(f"   JS文件: {scripts[:10]}")

# Step 2: 获取主要的 app JS 文件来分析API
SPA_BASE = "http://106.13.91.31:43134"
main_js = None
for js in scripts:
    if 'pages-book-bookplay' in js or 'app' in js.lower() or 'main' in js.lower():
        main_js = js
        break

if not main_js and scripts:
    # 取最后一个
    for js in reversed(scripts):
        if js.endswith('.js'):
            main_js = js
            break

if main_js:
    url = f"{BASE}{main_js}" if main_js.startswith('/') else (main_js if main_js.startswith('http') else f"{SPA_BASE}{main_js}")
    print(f"\n   分析主JS: {url}")
    try:
        resp2 = s.get(url, timeout=30)
        if resp2.status_code == 200:
            js_text = resp2.text
            print(f"   JS长度: {len(js_text)}")

            # 找API端点
            api_patterns = [
                r'["\']([^"\']*api[^"\']*)["\']',
                r'["\']([^"\']*/book/ting[^"\']*)["\']',
                r'["\']([^"\']*/book/Ting[^"\']*)["\']',
                r'["\']([^"\']*/book/play[^"\']*)["\']',
                r'["\']([^"\']*://[^"\']*audio[^"\']*)["\']',
                r'["\']([^"\']*://[^"\']*mp3[^"\']*)["\']',
                r'["\']([^"\']*://[^"\']*/myfiles[^"\']*)["\']',
            ]
            for pat in api_patterns:
                matches = re.findall(pat, js_text, re.IGNORECASE)
                if matches:
                    print(f"   {pat[:50]}: {matches[:5]}")

            # 找 src 赋值
            src_assigns = re.findall(r'(?:src|url|audioUrl)\s*[:=]\s*[`"\'][^`"\']*[`"\']', js_text, re.IGNORECASE)
            if src_assigns:
                print(f"   src/url赋值: {src_assigns[:5]}")

            # 找 axios/get 调用
            axios_calls = re.findall(r'(?:axios|get|post|request)\s*\(\s*[`"\'][^`"\']*[`"\']', js_text, re.IGNORECASE)
            if axios_calls:
                print(f"   axios/get调用: {axios_calls[:10]}")

            # 搜索 ting 相关
            ting_refs = re.findall(r'["\']([^"\']*ting[^"\']*)["\']', js_text)
            if ting_refs:
                print(f"   ting引用: {ting_refs[:10]}")

            # 保存JS
            with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_main_js.txt", "w", encoding="utf-8") as f:
                f.write(js_text)
        else:
            print(f"   JS请求失败: HTTP {resp2.status_code}")
    except Exception as e:
        print(f"   JS请求异常: {e}")

# Step 3: 尝试直接用API猜测
print(f"\n🔍 猜测API端点")
test_apis = [
    f"{BASE}/book/Ting/{chapter_id}",
    f"{BASE}/book/tingApi/{chapter_id}",
    f"{BASE}/api/book/ting/{chapter_id}",
    f"{BASE}/api/book/Ting/{chapter_id}",
    f"{BASE}/book/api/ting/{chapter_id}",
]
chapter_id = "3a20a68a-d9e0-ce93-6c13-4dc2f86c96b6"
for api in test_apis:
    try:
        r = s.get(api, timeout=15)
        print(f"   {api}: HTTP {r.status_code}, {r.text[:100]}")
    except:
        print(f"   {api}: 请求失败")

print("\n✅ 完成")