"""
测试 v8：用 requests 直接获取悦听巴书籍详情页
"""
import requests
import re
import ssl
import json
from urllib.parse import urljoin
from requests.adapters import HTTPAdapter
import urllib3
urllib3.disable_warnings()

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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})
s.verify = False
s.mount("https://", WeakTLSAdapter())

BASE = "http://www.yuetingba.cn"
book_id = "3a20a667-3381-cb4f-f597-92364e9a0039"
url = f"{BASE}/book/detail/{book_id}/0"

print("🔍 悦听巴 - 书籍详情页 (requests)")
print(f"   URL: {url}")
print("=" * 60)

resp = s.get(url, timeout=30)
print(f"   HTTP {resp.status_code}, 长度: {len(resp.text)}")
html = resp.text

# 保存
with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_detail_raw.html", "w", encoding="utf-8") as f:
    f.write(html)

# 提取 JSON-LD
ld_matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
print(f"\n   JSON-LD scripts: {len(ld_matches)} 个")
for i, ld in enumerate(ld_matches):
    try:
        data = json.loads(ld)
        if isinstance(data, list):
            print(f"   [{i}] 列表 {len(data)} 项")
            for item in data[:3]:
                print(f"       - {json.dumps(item, ensure_ascii=False)[:150]}")
        else:
            print(f"   [{i}] {json.dumps(data, ensure_ascii=False)[:200]}")
    except:
        print(f"   [{i}] 解析失败, 原始: {ld[:200]}")

# 提取所有 script 内容中的变量
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
print(f"\n   Script tags: {len(scripts)} 个")
for i, sc in enumerate(scripts):
    if len(sc) > 20:
        # 找可能的数组/对象
        if 'chapters' in sc.lower() or 'tracks' in sc.lower() or '[' in sc[:100]:
            print(f"   [{i}] 长度: {len(sc)}, 开头: {sc[:200]}")
            # 尝试提取变量
            vars_found = re.findall(r'(?:var|let|const)\s+(\w+)', sc[:500])
            if vars_found:
                print(f"       变量: {vars_found}")
            # 找 window.__ 变量
            win_vars = re.findall(r'window\.(\w+)', sc[:500])
            if win_vars:
                print(f"       window变量: {win_vars}")

# 找API端点
api_urls = re.findall(r'["\'](/api/[^"\']+)["\']', html)
print(f"\n   /api/ 端点: {api_urls[:5]}")

# 找 play 相关 URL
play_urls = re.findall(r'["\']([^"\']*play[^"\']*)["\']', html)
print(f"\n   play 相关: {play_urls[:5]}")

# 检查是否是SPA (body是否为空)
body_content = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL)
if body_content:
    body_text = body_content.group(1)
    # 移除所有script/style标签
    clean = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', body_text, flags=re.DOTALL)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    print(f"\n   Body 纯文本(前300字): {clean[:300]}")
else:
    print(f"\n   Body: 未找到")

print("\n✅ 分析完成")