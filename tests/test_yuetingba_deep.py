"""
测试脚本 v4：深入解析悦听巴 yuetingba.cn 的书籍详情和音频
"""
import requests
import re
import ssl
import json
from urllib.parse import urljoin
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
})
s.verify = False
s.mount("https://", WeakTLSAdapter())

BASE = "http://www.yuetingba.cn"

def get(url, **kw):
    try:
        return s.get(url, timeout=20, **kw)
    except Exception as e:
        print(f"   ❌ 请求失败: {e}")
        return None


# ═══════════════════════════════════════════════════
# Step 1: 进入一本具体书籍的详情页
# ═══════════════════════════════════════════════════
book_detail = "/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0"  # 问道红尘
url = f"{BASE}{book_detail}"

print("=" * 60)
print("🔍 悦听巴 - 书籍详情页")
print(f"   URL: {url}")
print("=" * 60)

resp = get(url)
if resp and resp.status_code == 200:
    html = resp.text
    print(f"   ✅ HTTP 200, 长度: {len(html)}")

    # 保存
    with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_detail.html", "w", encoding="utf-8") as f:
        f.write(html)

    # 提取书名
    title_m = re.search(r'<title>([^<]+)</title>', html)
    if title_m:
        print(f"   📖 书名: {title_m.group(1).strip()}")

    # 提取 h1 或其他标题
    h1s = re.findall(r'<h\d[^>]*class=["\']book[^"\']*["\'][^>]*>([^<]+)</h\d>', html, re.IGNORECASE)
    if h1s:
        print(f"   标题标签: {h1s[:3]}")

    # 找章节列表区域 - 搜索常见的章节容器
    for key in ['chapter', 'playlist', 'episode', 'track', 'catalog']:
        m = re.search(rf'<[^>]*id=["\']{key}[^"\']*["\'][^>]*>(.*?)</(?:div|ul|section)>', html, re.DOTALL | re.IGNORECASE)
        if m:
            section = m.group(1)
            links = re.findall(r'<a[^>]*href=["\']([^"\']+)["\']([^>]*)>([^<]+)</a>', section)
            print(f"   📋 #{key} 区域: {len(links)} 个链接")
            for href, attrs, text in links[:5]:
                print(f"      - {text.strip()[:30]} -> {href}")
            break
    else:
        # 没找到特定ID，找所有包含数字的链接
        all_links = re.findall(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>', html)
        # 过滤可能的章节链接
        chapter_candidates = []
        for href, text in all_links:
            if re.search(r'/(?:play|chapter|episode|track|sound|\d+)', href, re.IGNORECASE):
                chapter_candidates.append((href.strip(), text.strip()))

        print(f"   📋 可能的章节链接: {len(chapter_candidates)} 个")
        for href, text in chapter_candidates[:10]:
            print(f"      - {text[:40]} -> {href[:80]}")

        # 如果章节太多，找看起来像是具体章节编号的
        numbered = [(h, t) for h, t in chapter_candidates if re.search(r'/(\d+)$', h) or re.search(r'chapter|play|episode', h, re.IGNORECASE)]
        print(f"   📋 含编号/关键词的: {len(numbered)} 个")
        for href, text in numbered[:10]:
            print(f"      - {text[:40]} -> {href}")
else:
    print(f"   ❌ HTTP {resp.status_code if resp else 'N/A'}")


# ═══════════════════════════════════════════════════
# Step 2: 搜索"问道红尘"找到章节列表
# ═══════════════════════════════════════════════════
print("\n" + "=" * 60)
print("🔍 搜索API尝试")
print("=" * 60)

# 搜一下看看有没有API
search_url = f"{BASE}/search?type=1&name=问道红尘"
resp = get(search_url)
if resp and resp.status_code == 200:
    print(f"   搜索页: 200, 长度: {len(resp.text)}")
    with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_search.html", "w", encoding="utf-8") as f:
        f.write(resp.text)

# ═══════════════════════════════════════════════════
# Step 3: 仔细分析 detail 页面 HTML 中的章节结构
# ═══════════════════════════════════════════════════
print("\n" + "=" * 60)
print("🔍 分析 detail 页面结构")
print("=" * 60)

with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_detail.html", "r", encoding="utf-8") as f:
    html = f.read()

# 找所有 script 标签中的 JSON / JS 变量
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
for i, script in enumerate(scripts):
    # 找可能的API URL / 章节数据
    if 'chapters' in script.lower() or 'tracks' in script.lower() or 'audio' in script.lower():
        print(f"   Script[{i}] (含音频/章节相关):")
        print(f"      {script[:500]}")
        print(f"      ---")

    # 找 API 端点
    apis = re.findall(r'(?:api|url|src|href)\s*[:=]\s*["\']([^"\']+)["\']', script)
    if apis:
        for a in apis:
            if any(k in a.lower() for k in ['api', 'play', 'audio', 'chapter', 'track']):
                print(f"   Script[{i}] 发现API: {a}")

# 找所有包含 'play' 或 'chapter' 或 'track' 或 'episode' 的链接
all_hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
play_links = [h for h in all_hrefs if 'play' in h.lower() or 'chapter' in h.lower() or 'track' in h.lower() or 'episode' in h.lower()]
print(f"\n   含 play/chapter/track/episode 的链接: {len(play_links)} 个")
for l in play_links[:15]:
    print(f"      {l}")

# 看看有没有 data-* 属性中的章节信息
data_attrs = re.findall(r'data-(\w+)\s*=\s*["\']([^"\']+)["\']', html)
for attr, val in data_attrs:
    if any(k in attr.lower() for k in ['chapter', 'track', 'audio', 'play', 'episode']):
        print(f"   data-{attr} = {val[:80]}")

# 检查是否有 ng-* 或 vue 指令
if 'ng-' in html or 'v-' in html.lower():
    print(f"   ⚠️ 可能使用 Angular/Vue 动态渲染 (SPA)")

print("\n✅ 分析完成")