"""
测试 v9：获取悦听巴章节播放页 + 提取音频URL
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})
s.verify = False
s.mount("https://", WeakTLSAdapter())

BASE = "http://www.yuetingba.cn"

# 章节信息
book_id = "3a20a667-3381-cb4f-f597-92364e9a0039"
chapter_id = "3a20a68a-d9e0-ce93-6c13-4dc2f86c96b6"
chapter_title = "0001_序章 缘起"
ting_url = f"{BASE}/book/ting/{chapter_id}"

print("=" * 60)
print("🔍 悦听巴 - 章节播放页")
print(f"   {chapter_title}")
print(f"   URL: {ting_url}")
print("=" * 60)

resp = s.get(ting_url, timeout=30)
print(f"   HTTP {resp.status_code}, 长度: {len(resp.text)}")

html = resp.text

# 保存
with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_chapter.html", "w", encoding="utf-8") as f:
    f.write(html)

# 找音频链接
audios = re.findall(r'<audio[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
sources = re.findall(r'<source[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
mp3s = re.findall(r'https?://[^\s"\'<>]+\.mp3', html, re.IGNORECASE)
m4as = re.findall(r'https?://[^\s"\'<>]+\.m4a', html, re.IGNORECASE)

print(f"\n   <audio> src: {audios[:3]}")
print(f"   <source> src: {sources[:3]}")
print(f"   MP3链接: {len(mp3s)}个, 示例: {[m[:120] for m in mp3s[:3]]}")
print(f"   M4A链接: {len(m4as)}个, 示例: {[m[:120] for m in m4as[:3]]}")

# 找所有 js 变量/API 调用中的URL
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
for i, sc in enumerate(scripts):
    if 'src' in sc.lower() or 'url' in sc.lower() or 'audio' in sc.lower():
        # 找各种音频URL模式
        url_patterns = re.findall(r'(?:src|url|audioUrl|mp3|m4a)\s*[:=]\s*["\']([^"\']+)["\']', sc, re.IGNORECASE)
        if url_patterns:
            print(f"\n   Script[{i}] 发现URL: {url_patterns[:5]}")

        # 找 base64编码的URL
        b64 = re.findall(r'atob\s*\(\s*["\']([^"\']+)["\']\s*\)', sc)
        if b64:
            print(f"   Script[{i}] base64: {b64[:3]}")

# 找所有链接
links = re.findall(r'<a[^>]*href=["\']([^"\']+)["\']', html, re.IGNORECASE)
audio_links = [l for l in links if any(k in l.lower() for k in ['.mp3', '.m4a', '.ogg', '.wav', 'audio', '.m3u8'])]
print(f"\n   音频相关链接: {audio_links[:5]}")

# 如果有 src，尝试下载验证
if mp3s:
    test_url = mp3s[0]
    print(f"\n   测试下载: {test_url}")
    try:
        audio_resp = s.head(test_url, timeout=15)
        print(f"   HEAD请求: HTTP {audio_resp.status_code}")
        cl = audio_resp.headers.get('Content-Length', 'N/A')
        ct = audio_resp.headers.get('Content-Type', 'N/A')
        print(f"   Content-Length: {cl}, Content-Type: {ct}")
    except Exception as e:
        print(f"   HEAD失败: {e}")

if m4as:
    test_url = m4as[0]
    print(f"\n   测试下载: {test_url}")
    try:
        audio_resp = s.head(test_url, timeout=15)
        print(f"   HEAD请求: HTTP {audio_resp.status_code}")
        cl = audio_resp.headers.get('Content-Length', 'N/A')
        ct = audio_resp.headers.get('Content-Type', 'N/A')
        print(f"   Content-Length: {cl}, Content-Type: {ct}")
    except Exception as e:
        print(f"   HEAD失败: {e}")

# 打印body文本
body = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL)
if body:
    clean = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', body.group(1), flags=re.DOTALL)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    print(f"\n   Body文本: {clean[:300]}")

print("\n✅ 分析完成")