"""
测试 v12: 获取悦听巴 site.js + 用Playwright stealth模式
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
})
s.verify = False
s.mount("https://", WeakTLSAdapter())

print("=" * 60)
print("🔍 获取 site.js (包含 testFn 函数)")
print("=" * 60)

resp = s.get("http://106.13.91.31:43134/js/site.js", timeout=30)
print(f"   HTTP {resp.status_code}, 长度: {len(resp.text)}")

js_text = resp.text

# 搜索 testFn
test_fn_start = js_text.find("testFn")
if test_fn_start > 0:
    print(f"\n   testFn 在位置 {test_fn_start}")
    # 打印周围代码
    snippet = js_text[max(0, test_fn_start-50):test_fn_start+1000]
    print(f"   代码: {snippet[:500]}")

# 找所有函数定义
funcs = re.findall(r'function\s+(\w+)\s*\(', js_text)
print(f"\n   函数列表: {funcs}")

# 找 API 端点 / URL
urls = re.findall(r'["\']([^"\']*(?:api|ting|play|audio|book|get|src)[^"\']*)["\']', js_text, re.IGNORECASE)
print(f"\n   含关键字的URL: {urls[:20]}")

# 找 axios / $.ajax / fetch 调用
ajax_calls = re.findall(r'(?:axios|\.ajax|fetch|\.get|\.post)\s*\([^)]+\)', js_text)
if ajax_calls:
    print(f"\n   AJAX调用: {ajax_calls[:10]}")

# 保存
with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_site.js", "w", encoding="utf-8") as f:
    f.write(js_text)

print("\n" + "=" * 60)
print("🔍 同时获取 bookplay JS 组件")
print("=" * 60)

# 获取 pages-book-bookplay 的JS
resp2 = s.get("http://106.13.91.31:43134/tingpage/assets/pages-book-bookplay.DVPVQF4m.js", timeout=30)
if resp2.status_code == 200:
    print(f"   HTTP 200, 长度: {len(resp2.text)}")
    with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_bookplay.js", "w", encoding="utf-8") as f:
        f.write(resp2.text)

    # 搜索关键模式
    patterns = [
        r'["\']([^"\']*mp3[^"\']*)["\']',
        r'["\']([^"\']*m4a[^"\']*)["\']',
        r'["\']([^"\']*audio[^"\']*)["\']',
        r'["\']([^"\']*://[^"\']*/myfiles[^"\']*)["\']',
        r'["\']([^"\']*/api/[^"\']*)["\']',
    ]
    for pat in patterns:
        matches = re.findall(pat, resp2.text, re.IGNORECASE)
        if matches:
            print(f"   {pat}: {matches[:5]}")
else:
    print(f"   HTTP {resp2.status_code}")

print("\n✅ 完成")