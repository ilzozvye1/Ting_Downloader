"""
测试 v15: 直接调用悦听巴API + 验证音频下载
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
    "Referer": "http://www.yuetingba.cn/",
})
s.verify = False
s.mount("https://", WeakTLSAdapter())

BASE = "http://www.yuetingba.cn"

print("=" * 60)
print("🔍 悦听巴 - API 验证 + 音频下载测试")
print("=" * 60)

# ── Step 1: 测试 API (ting-with-efi) ──
book_id = "3a20a667-3381-cb4f-f597-92364e9a0039"
chapter_id = "3a20a68a-d9e0-ce93-6c13-4dc2f86c96b6"

api_url = f"{BASE}/api/app/docs-listen/{chapter_id}/ting-with-efi"
print(f"\n📡 API: {api_url}")
r = s.get(api_url, timeout=15)
print(f"   HTTP {r.status_code}")
try:
    data = r.json()
    print(f"   响应: {json.dumps(data, ensure_ascii=False, indent=2)}")
    efi = data.get("efi", "")

    # efi 可能也是加密的
    if efi:
        print(f"\n   efi字段: {efi[:80]}...")

except:
    print(f"   原始响应: {r.text[:300]}")

# ── Step 2: 测试获取所有章节的 API ──
# 从页面结构看，章节是通过分页返回的
# /book/detail/{book_id}/0 返回第0-199章(HTML)
# 尝试 JSON API

# 尝试 chapters list API
possible_chapter_apis = [
    f"{BASE}/api/app/docs-listen/{book_id}/tings",
    f"{BASE}/api/app/docs-listen/{book_id}/chapters",
    f"{BASE}/api/book/{book_id}/tings",
    f"{BASE}/api/book/chapters/{book_id}",
]

for api in possible_chapter_apis:
    try:
        r = s.get(api, timeout=10)
        if r.status_code == 200 and len(r.text) > 10:
            ct = r.headers.get("Content-Type", "")
            try:
                data = r.json()
                if isinstance(data, list):
                    print(f"\n   ✅ 章节API: {api} -> {len(data)} chapters")
                    print(f"   示例: {json.dumps(data[0], ensure_ascii=False)[:200]}")
                elif isinstance(data, dict):
                    print(f"\n   ✅ 章节API: {api} -> keys: {list(data.keys())[:5]}")
                    for k, v in data.items():
                        if isinstance(v, list):
                            print(f"      {k}: {len(v)} items")
                            if v:
                                print(f"      [0]: {json.dumps(v[0], ensure_ascii=False)[:200]}")
            except:
                pass
    except:
        pass

# ── Step 3: 测试直接下载音频（需要token） ──
# 从Playwright获取的音频URL示例：
# http://36.5.86.168:35662/myfiles/host/listen/booksdir/WDHC_{book_id}/hash.mp3?token=xxx&expire=xxx
# 注意: token会过期

# 尝试通过解析 assl 变量获取音频URL
# assl 在页面中是一个加密的字符串，可能通过 ts 和 es 解密
# 从之前的Playwright测试: ts='dccc498ee4531778804721', es='310'

# 尝试用 page HTML 中的 assl 解码
# 直接尝试请求一个已知的音频URL（token可能已过期但先测试结构）

# 尝试通过 HTTP 直接获取详情页，解析 assl + ts + es 变量
print(f"\n📄 获取详情页变量用于解密")
detail_url = f"{BASE}/book/detail/{book_id}/0"
r = s.get(detail_url, timeout=15)
if r.status_code == 200:
    # 提取变量
    match_tingId = re.search(r"var tingId = '([^']+)'", r.text)
    match_assl = re.search(r"var assl = '([^']+)'", r.text)
    match_ts = re.search(r"var ts ='([^']+)'", r.text)
    match_es = re.search(r"var es ='([^']+)'", r.text)

    tingId = match_tingId.group(1) if match_tingId else None
    assl_val = match_assl.group(1) if match_assl else None
    ts_val = match_ts.group(1) if match_ts else None
    es_val = match_es.group(1) if match_es else None

    print(f"   tingId: {tingId}")
    print(f"   assl: {'存在' if assl_val else '无'} (长度: {len(assl_val) if assl_val else 0})")
    print(f"   ts: {ts_val}")
    print(f"   es: {es_val}")

    # 尝试不同的解密方法
    if assl_val:
        import base64
        # 尝试 base64 解码
        try:
            # 补齐 base64 padding
            padded = assl_val + '=' * (4 - len(assl_val) % 4) if len(assl_val) % 4 else assl_val
            decoded = base64.b64decode(padded)
            print(f"\n   base64解码: {decoded[:100]}")
            # 可能还需要进一步解密（AES等）
        except Exception as e:
            print(f"\n   base64解码失败: {e}")

        # 尝试 URL-safe base64
        try:
            url_safe = assl_val.replace('-', '+').replace('_', '/')
            padded = url_safe + '=' * (4 - len(url_safe) % 4) if len(url_safe) % 4 else url_safe
            decoded = base64.b64decode(padded)
            print(f"   base64(urlsafe)解码: {decoded[:100]}")
        except:
            pass

# ── Step 4: 直接请求获取章节的ting-with-efi (用于第二个章节验证) ──
second_chapter_id = "3a20a68a-d9e0-0ae3-ac20-4b536fd0cf04"  # 0002_寻仙
api_url2 = f"{BASE}/api/app/docs-listen/{second_chapter_id}/ting-with-efi"
print(f"\n📡 第二章节API: {api_url2}")
r2 = s.get(api_url2, timeout=15)
if r2.status_code == 200:
    data2 = r2.json()
    print(f"   响应: {json.dumps(data2, ensure_ascii=False)}")

print(f"\n✅ 测试完成")
print(f"\n📝 关键发现:")
print(f"   1. API端点: /api/app/docs-listen/{'{chapter_id}'}/ting-with-efi")
print(f"   2. 返回JSON含efi字段(加密的音频信息)")
print(f"   3. 页面变量 assl/ts/es 用于解密音频URL")
print(f"   4. 音频CDN: 36.5.86.168:35662 (带token)")
print(f"   5. 章节列表通过 /book/detail/{'{book_id}'}/{'{offset}'} HTML分页返回")