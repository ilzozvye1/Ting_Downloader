"""
测试脚本 v3：修复 bug + 深入解析已连通站点
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

s = requests.Session()
s.headers.update(HEADERS)
s.verify = False
s.mount("https://", WeakTLSAdapter())


def get(url, **kw):
    try:
        return s.get(url, timeout=20, **kw)
    except Exception as e:
        return None


print("=" * 60)
print("🔍 深入测试: 已连通站点")
print("=" * 60)


# ═══════════════════════════════════════════════════
# 1. 88听书网 - HTTP 能通
# ═══════════════════════════════════════════════════
print("\n" + "=" * 60)
print("1️⃣  88听书网 88tingshu.com (HTTP)")
print("=" * 60)

resp = get("http://www.88tingshu.com/29101/")
if resp:
    html = resp.text
    print(f"   首页 HTTP 200, 长度: {len(html)}")
    print(f"   title: {re.findall(r'<title>([^<]+)</title>', html)[:2]}")
    print(f"   h1: {re.findall(r'<h1[^>]*>([^<]+)</h1>', html)[:3]}")

    # 保存HTML用于分析
    with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/88tingshu_book.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("   HTML 已保存到 tests/88tingshu_book.html")

    # 查找章节链接
    # 找 iframe
    iframes = re.findall(r'<iframe[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    print(f"   iframes: {iframes[:5]}")

    # 试试常见的章节链接模式
    patterns = [
        r'<a[^>]*href=["\']([^"\']*\d+-\d+[^"\']*)["\'][^>]*>([^<]*)</a>',
        r'<a[^>]*href=["\']([^"\']*play[^"\']*)["\'][^>]*>([^<]*)</a>',
        r'<a[^>]*href=["\']([^"\']*player[^"\']*)["\'][^>]*>([^<]*)</a>',
        r'<a[^>]*href=["\']([^"\']*sound[^"\']*)["\'][^>]*>([^<]*)</a>',
    ]
    for pat in patterns:
        matches = re.findall(pat, html, re.IGNORECASE)
        if matches:
            print(f"   章节模式匹配 ({pat[:50]}...): {len(matches)} 个, 示例: {matches[:3]}")
            break
    else:
        # 找 playlist / chapter 相关区域
        for keyword in ['playlist', 'chapter', 'vlink', 'list']:
            m = re.search(rf'(<[^>]*id=["\']{keyword}["\'][^>]*>.*?</(?:div|ul)>)', html, re.DOTALL | re.IGNORECASE)
            if m:
                section = m.group(1)
                links = re.findall(r'<a[^>]*href=["\']([^"\']+)["\']', section)
                print(f"   #{keyword} 区域链接: {len(links)} 个, 示例: {links[:5]}")
else:
    print("   ❌ 主页无法访问")

# 尝试章节页 - 搜索一个已知真实存在的章节URL
# 从之前搜索结果看，格式是 /play/ 开头的
test_urls = [
    "http://www.88tingshu.com/player/29101-1.html",
    "http://www.88tingshu.com/play/29101-1.html",
]
for tu in test_urls:
    r2 = get(tu)
    if r2 and r2.status_code == 200:
        print(f"\n   章节页测试: {tu} -> 200, 长度: {len(r2.text)}")
        # 找音频
        mp3s = re.findall(r'https?://[^\s"\'<>]+\.mp3', r2.text, re.IGNORECASE)
        m4as = re.findall(r'https?://[^\s"\'<>]+\.m4a', r2.text, re.IGNORECASE)
        iframes2 = re.findall(r'<iframe[^>]*src=["\']([^"\']+)["\']', r2.text, re.IGNORECASE)
        audio_src = re.findall(r'<audio[^>]*src=["\']([^"\']+)["\']', r2.text, re.IGNORECASE)
        print(f"   mp3: {len(mp3s)}个, m4a: {len(m4as)}个, iframe: {len(iframes2)}个, audio_tag: {len(audio_src)}个")
        if mp3s: print(f"   mp3示例: {mp3s[0][:120]}")
        if m4as: print(f"   m4a示例: {m4as[0][:120]}")
        if iframes2: print(f"   iframe示例: {iframes2[0]}")
        # 保存
        with open(f"e:/GitHub/AudioBook_dl/audiobook-dl/tests/88tingshu_chapter_{tu.split('/')[-1]}", "w", encoding="utf-8") as f:
            f.write(r2.text)
        break
    elif r2:
        print(f"\n   章节页测试: {tu} -> HTTP {r2.status_code}")
    else:
        print(f"\n   章节页测试: {tu} -> 连接失败")


# ═══════════════════════════════════════════════════
# 2. 爱书音 ishuyin.com
# ═══════════════════════════════════════════════════
print("\n" + "=" * 60)
print("2️⃣  爱书音 ishuyin.com (HTTP)")
print("=" * 60)

resp = get("http://www.ishuyin.com/")
if resp:
    html = resp.text
    print(f"   首页 HTTP 200, 长度: {len(html)}")
    print(f"   title: {re.findall(r'<title>([^<]+)</title>', html)}")

    # 找书籍链接
    book_links = re.findall(r'<a[^>]*href=["\']([^"\']*book[^"\']*)["\'][^>]*>([^<]*)</a>', html)
    print(f"   含'book'链接: {len(book_links)}个, 示例: {book_links[:3]}")

    # 找所有链接
    all_links = re.findall(r'<a[^>]*href=["\']([^"\']+)["\']', html)
    # 找可能的小说链接
    novel_links = [l for l in all_links if re.search(r'(book|novel|player|play|read|detail|view|show)', l)]
    print(f"   小说相关链接: {len(novel_links)}个, 示例: {novel_links[:5]}")

    with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/ishuyin_home.html", "w", encoding="utf-8") as f:
        f.write(html)
else:
    print("   ❌ 无法访问")


# ═══════════════════════════════════════════════════
# 3. 悦听巴 yuetingba.cn
# ═══════════════════════════════════════════════════
print("\n" + "=" * 60)
print("3️⃣  悦听巴 yuetingba.cn (HTTP)")
print("=" * 60)

resp = get("http://www.yuetingba.cn/")
if resp:
    html = resp.text
    print(f"   首页 HTTP 200, 长度: {len(html)}")
    print(f"   title: {re.findall(r'<title>([^<]+)</title>', html)}")

    # 找书籍链接
    book_links = re.findall(r'<a[^>]*href=["\'](/book/[^"\']+)["\'][^>]*>([^<]*)</a>', html)
    if not book_links:
        # 尝试更广泛的模式
        book_links = re.findall(r'<a[^>]*href=["\']([^"\']*detail[^"\']*)["\'][^>]*>([^<]*)</a>', html)
    print(f"   书籍链接: {len(book_links)}个, 示例: {book_links[:5]}")

    with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_home.html", "w", encoding="utf-8") as f:
        f.write(html)

    # 如果找到书籍链接，深入测试
    if book_links:
        test_book_url = urljoin("http://www.yuetingba.cn/", book_links[0][0])
        print(f"\n   测试书籍页: {test_book_url}")
        r2 = get(test_book_url)
        if r2 and r2.status_code == 200:
            book_html = r2.text
            print(f"   书籍页 200, 长度: {len(book_html)}")
            print(f"   title: {re.findall(r'<title>([^<]+)</title>', book_html)}")

            # 找章节
            chapter_links = re.findall(r'<a[^>]*href=["\']([^"\']*\d+[^"\']*)["\'][^>]*>([^<]*)</a>', book_html)
            print(f"   章节链接: {len(chapter_links)}个, 示例: {chapter_links[:5]}")

            with open("e:/GitHub/AudioBook_dl/audiobook-dl/tests/yuetingba_book.html", "w", encoding="utf-8") as f:
                f.write(book_html)
        else:
            print(f"   书籍页: HTTP {r2.status_code if r2 else '连接失败'}")
else:
    print("   ❌ 无法访问")


# ═══════════════════════════════════════════════════
# 4. 喜马拉雅 - 换种方式
# ═══════════════════════════════════════════════════
print("\n" + "=" * 60)
print("4️⃣  喜马拉雅 ximalaya.com - 换API策略")
print("=" * 60)

# 策略1: 直接用音频ID测试
sound_ids = ["355188898", "43755913"]  # 网上公开分享的ID
for sid in sound_ids:
    api_url = f"https://www.ximalaya.com/revision/play/v1/audio?id={sid}&ptype=1"
    r = get(api_url)
    if r and r.status_code == 200:
        try:
            data = r.json()
            src = data.get("data", {}).get("src", "")
            print(f"   sound_id={sid}: src={'✅' if src else '❌'}")
            if src:
                print(f"      audio_url: {src[:120]}")
        except:
            print(f"   sound_id={sid}: 非JSON响应, status={r.status_code}")

# 策略2: 尝试不同的专辑API
album_ids = ["67641798", "47934162", "33039673", "35468714"]
for aid in album_ids[:2]:
    # 版本2 API
    api_v2 = f"https://www.ximalaya.com/revision/album/v1/getTracksList?albumId={aid}&pageNum=1&pageSize=5"
    r = get(api_v2)
    if r and r.status_code == 200:
        try:
            data = r.json()
            tracks = data.get("data", {}).get("tracks", [])
            print(f"   album {aid} v2: {len(tracks)} tracks")
            for t in tracks[:2]:
                print(f"      {t.get('trackId')}: {t.get('title', '')[:30]}")
                if t.get('trackId'):
                    # 拿这个track去获取音频
                    aid_url = f"https://www.ximalaya.com/revision/play/v1/audio?id={t['trackId']}&ptype=1"
                    ar = get(aid_url)
                    if ar and ar.status_code == 200:
                        ad = ar.json()
                        src = ad.get("data", {}).get("src", "")
                        print(f"         src: {'✅' if src else '❌'} {src[:80] if src else ''}")
        except Exception as e:
            print(f"   album {aid} v2 ERROR: {e}")

# 策略3: 用播放页API
print("\n   播放页API:")
r = get("https://www.ximalaya.com/revision/play/v1/audio?id=355188898&ptype=1")
if r:
    print(f"   audio API: status={r.status_code}, body={r.text[:300]}")


# ═══════════════════════════════════════════════════
# 5. 幻听网 - 尝试不同方式
# ═══════════════════════════════════════════════════
print("\n" + "=" * 60)
print("5️⃣  幻听网 ting89.com - 尝试DoH解析")
print("=" * 60)

# 尝试通过 DoH 获取真实 IP
try:
    doh_r = requests.get("https://1.1.1.1/dns-query?name=www.ting89.com&type=A",
                         headers={"Accept": "application/dns-json"}, timeout=10)
    if doh_r.status_code == 200:
        ips = [a["data"] for a in doh_r.json().get("Answer", []) if a.get("type") == 1]
        print(f"   DoH解析: {ips}")
        if ips:
            # 直接通过 IP + Host 头访问
            ip_url = f"http://{ips[0]}/books/9774.html"
            r = requests.get(ip_url, headers={"Host": "www.ting89.com", **HEADERS}, timeout=15)
            print(f"   IP直连: status={r.status_code}, 长度={len(r.text)}")
            if r.status_code == 200:
                # 解析
                chapters = re.findall(r"<a\s+href='(/down/\?[^']+?)'[^>]*>([^<]+)</a>", r.text)
                print(f"   章节: {len(chapters)}个, 示例: {chapters[:3]}")
                if chapters:
                    ch_url = urljoin("http://www.ting89.com/", chapters[0][0])
                    print(f"   章节URL: {ch_url}")
                    r2 = requests.get(ch_url, headers={"Host": "www.ting89.com", **HEADERS}, timeout=15)
                    if r2.status_code == 200:
                        mp3s = re.findall(r'https?://[^\s"\'<>]+\.mp3', r2.text, re.IGNORECASE)
                        print(f"   音频: {len(mp3s)}个")
                        if mp3s: print(f"   MP3: {mp3s[0][:150]}")
                    else:
                        print(f"   章节页: HTTP {r2.status_code}")
except Exception as e:
    print(f"   DoH异常: {e}")


# ═══════════════════════════════════════════════════
# 6. 舒听网 - 尝试绕过Cloudflare
# ═══════════════════════════════════════════════════
print("\n" + "=" * 60)
print("6️⃣  舒听网 shuting5.com - 尝试绕过")
print("=" * 60)

# 用 cf_clearance 之类的cookie试试
for ua in [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15",
]:
    r = requests.get("https://www.shuting5.com/", headers={**HEADERS, "User-Agent": ua}, verify=False, timeout=15)
    print(f"   UA={ua[:40]}...: status={r.status_code}, cf={any(h.lower().startswith('cf-') for h in r.headers)}")

# 试试 HTTP
r = requests.get("http://www.shuting5.com/", headers=HEADERS, timeout=15)
print(f"   HTTP: status={r.status_code}")


print("\n\n" + "=" * 60)
print("📊 测试完成")
print("=" * 60)