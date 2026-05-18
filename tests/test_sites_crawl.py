"""
综合测试脚本 v2：逐个测试各有声书网站的爬取可行性
改进: 使用自定义 SSL context 绕过弱证书 / 低 TLS 版本问题
"""
import requests
import re
import ssl
import json
import socket
from urllib.parse import urljoin

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 构造兼容弱 SSL 服务器的自定义 Adapter ──
from requests.adapters import HTTPAdapter

class WeakTLSAdapter(HTTPAdapter):
    """降低 SSL 安全级别，兼容使用自签名/过期证书或低版本 TLS 的服务器"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def make_session():
    """创建带弱 TLS 兼容的 Session"""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    adapter = WeakTLSAdapter()
    s.mount("https://", adapter)
    s.mount("http://", HTTPAdapter())
    return s


SESSION = make_session()


def safe_get(url, referer=None, timeout=20, session=None):
    """安全请求"""
    s = session or SESSION
    h = dict(HEADERS)
    if referer:
        h["Referer"] = referer
    try:
        resp = s.get(url, headers=h, timeout=timeout)
        return resp.status_code, resp.text, resp
    except requests.exceptions.SSLError as e:
        return -1, f"SSL错误: {str(e)[:200]}", None
    except requests.exceptions.ConnectionError as e:
        return -2, f"连接错误: {str(e)[:200]}", None
    except requests.exceptions.Timeout:
        return -3, "超时", None
    except Exception as e:
        return 0, f"错误: {str(e)[:200]}", None


def safeprint(key, val):
    """截断过长的值"""
    s = str(val)
    if len(s) > 400:
        s = s[:400] + "...(截断)"
    print(f"    {key}: {s}")


def run_test(name, url, parser=None, desc=""):
    """运行单个测试"""
    print(f"\n  📋 [{name}] {desc}")
    print(f"     URL: {url}")

    status, content, resp = safe_get(url)

    if status < 0:
        print(f"     ❌ {content}")
        return {"name": name, "status": "FAIL", "error": content}

    if status == 403:
        # 被 Cloudflare 拦截或其他 WAF
        blocked = "cloudflare" in content.lower() or "cf-" in str(resp.headers if resp else "").lower()
        print(f"     ❌ HTTP 403 {'(疑似 Cloudflare 拦截)' if blocked else ''}")
        return {"name": name, "status": "BLOCKED_403"}

    if status != 200:
        print(f"     ❌ HTTP {status}")
        return {"name": name, "status": f"HTTP_{status}"}

    print(f"     ✅ HTTP 200 (响应: {len(content)} 字符)")

    if parser:
        try:
            result = parser(content, url, resp)
            if result:
                # 美化输出
                for k, v in result.items():
                    safeprint(k, v)
                return {"name": name, "status": "OK", "data": result}
            else:
                print(f"     ⚠️  解析返回空")
                return {"name": name, "status": "EMPTY"}
        except Exception as e:
            print(f"     ❌ 解析异常: {e}")
            return {"name": name, "status": "PARSE_ERR", "error": str(e)}
    else:
        return {"name": name, "status": "OK_RAW"}


# ═══════════════════════════════════════════════════
# 1. 幻听网 ting89.com
# ═══════════════════════════════════════════════════

def test_ting89():
    book_id = "9774"
    all = []

    # 首页
    all.append(run_test("ting89-首页", "http://www.ting89.com/", desc="基础连通性"))

    # 书籍页
    r = run_test("ting89-书籍页", f"http://www.ting89.com/books/{book_id}.html",
                 parser=lambda html, url, resp: parse_ting89_book(html, url),
                 desc="书籍章节列表")
    all.append(r)

    # 如果有章节链接，测试音频页
    if r.get("status") == "OK" and r.get("data", {}).get("sample_chapters"):
        ch = r["data"]["sample_chapters"][0]
        all.append(run_test("ting89-音频页", ch["url"],
                            parser=lambda h, u, r: parse_ting89_audio(h, u),
                            desc="提取音频直链"))

    return {"name": "幻听网 ting89.com", "tests": all}


def parse_ting89_book(html, url):
    title_m = re.search(r'<h1[^>]*>([^<]+)</h1>', html) or re.search(r'<title>([^<]+)</title>', html)
    title = title_m.group(1).strip() if title_m else None

    # ting89 的章节格式: <a href='/down/?book-chapter.html'>章名</a>
    chapters = re.findall(r"<a\s+href='(/down/\?[^']+?)'[^>]*>([^<]+)</a>", html)
    chapter_infos = []
    for href, name in chapters:
        chapter_infos.append({"title": name.strip(), "url": urljoin(url, href)})

    return {
        "title": title,
        "chapter_count": len(chapter_infos),
        "sample_chapters": chapter_infos[:3],
    }


def parse_ting89_audio(html, url):
    # 模式: url=(http://.../xxx.mp3)
    m = re.search(r"url=(https?://[^\s'\"]+\.mp3)", html, re.IGNORECASE)
    if m:
        return {"audio_url": m.group(1)}

    # 找 mp3/m4a
    for pat in [r'https?://[^\s"\'<>]+\.mp3', r'https?://[^\s"\'<>]+\.m4a']:
        found = re.findall(pat, html, re.IGNORECASE)
        if found:
            return {"audio_url": found[0]}

    # audio 标签
    m = re.search(r'<audio[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return {"audio_url": urljoin(url, m.group(1))}

    return {"error": "未找到音频链接", "html_preview": html[:500]}


# ═══════════════════════════════════════════════════
# 2. 88听书网 88tingshu.com
# ═══════════════════════════════════════════════════

def test_88tingshu():
    all = []

    # 先试 HTTPS，不行试 HTTP
    book_id = "29101"
    for scheme in ["https://", "http://"]:
        url = f"{scheme}www.88tingshu.com/{book_id}/"
        r = run_test(f"88tingshu-{scheme}", url,
                     parser=parse_88tingshu_book if scheme == "https://" else parse_88tingshu_book,
                     desc="书籍主页")
        all.append(r)
        if r["status"] == "OK":
            break

    return {"name": "88听书网 88tingshu.com", "tests": all}


def parse_88tingshu_book(html, url):
    title_m = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
    if not title_m:
        title_m = re.search(r'class=["\']book-title["\'][^>]*>([^<]+)<', html)
    if not title_m:
        title_m = re.search(r'<title>([^<]+)</title>', html)
    title = title_m.group(1).strip() if title_m else None

    # 找到 id="playlist" 下的链接
    playlist_m = re.search(r'id=["\']playlist["\'][^>]*>(.*?)</(?:div|ul)>', html, re.DOTALL)
    if playlist_m:
        html_part = playlist_m.group(1)
    else:
        html_part = html

    chapters = re.findall(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>', html_part)
    chapter_infos = []
    for href, name in chapters[:20]:  # 只取前20个
        if "javascript" in href.lower() or "#" == href.strip():
            continue
        full_url = urljoin(url, href)
        chapter_infos.append({"title": name.strip()[:30], "url": full_url})

    return {
        "title": title,
        "chapter_count": len(chapter_infos),
        "sample_chapters": chapter_infos[:3],
    }


# ═══════════════════════════════════════════════════
# 3. 喜马拉雅 ximalaya.com
# ═══════════════════════════════════════════════════

def test_ximalaya():
    all = []

    # 使用一个真实存在的免费专辑
    # 先用网页测试连通性
    all.append(run_test("xlmy-首页", "https://www.ximalaya.com/", desc="基础连通性"))

    # 测试免费专辑
    album_id = "67641798"
    all.append(run_test("xlmy-专辑页", f"https://www.ximalaya.com/album/{album_id}",
                        parser=lambda h, u, r: {"title": re.findall(r'<title>([^<]+)</title>', h)},
                        desc="专辑页面"))

    # 测试 API
    api_url = f"https://www.ximalaya.com/revision/play/v1/show?id={album_id}&sort=0&size=5&ptype=1"
    all.append(run_test("xlmy-API列表", api_url,
                        parser=lambda h, u, r: parse_xlmy_album_api(h),
                        desc="专辑章节API"))

    # 测试单个音频 API（先拿到 trackId）
    for t in all:
        if t.get("status") == "OK" and t.get("data", {}).get("tracks"):
            track_id = t["data"]["tracks"][0].get("trackId")
            if track_id:
                audio_api = f"https://www.ximalaya.com/revision/play/v1/audio?id={track_id}&ptype=1"
                all.append(run_test("xlmy-音频API", audio_api,
                                    parser=parse_xlmy_audio_api,
                                    desc="获取直链"))
                break

    return {"name": "喜马拉雅 ximalaya.com", "tests": all}


def parse_xlmy_album_api(content):
    try:
        data = json.loads(content)
        tracks = data.get("data", {}).get("tracksAudioPlay", [])
        return {
            "total_tracks": len(tracks),
            "tracks": [{"trackId": t.get("trackId"), "trackName": t.get("trackName")}
                       for t in tracks[:3]],
        }
    except:
        return {"error": "JSON解析失败", "raw_preview": content[:200]}


def parse_xlmy_audio_api(content):
    try:
        data = json.loads(content)
        src = data.get("data", {}).get("src", "")
        return {
            "audio_url": src[:150] if src else "EMPTY",
            "can_play": data.get("data", {}).get("canPlay"),
            "is_paid": data.get("data", {}).get("isPaid"),
        }
    except:
        return {"error": "JSON解析失败"}


# ═══════════════════════════════════════════════════
# 4. 舒听网 shuting5.com
# ═══════════════════════════════════════════════════

def test_shuting5():
    all = []
    all.append(run_test("shuting5-首页", "https://www.shuting5.com/", desc="基础连通性"))
    all.append(run_test("shuting5-HTTP", "http://www.shuting5.com/", desc="HTTP回退"))
    return {"name": "舒听网 shuting5.com", "tests": all}


# ═══════════════════════════════════════════════════
# 5. 中文听书网 tingzh.com
# ═══════════════════════════════════════════════════

def test_tingzh():
    all = []
    all.append(run_test("tingzh-首页", "https://www.tingzh.com/", desc="基础连通性"))
    all.append(run_test("tingzh-HTTP", "http://www.tingzh.com/", desc="HTTP回退"))
    return {"name": "中文听书网 tingzh.com", "tests": all}


# ═══════════════════════════════════════════════════
# 6. 爱书音 ishuyin.com
# ═══════════════════════════════════════════════════

def test_ishuyin():
    all = []
    all.append(run_test("ishuyin-首页", "https://www.ishuyin.com/", desc="基础连通性"))
    all.append(run_test("ishuyin-HTTP", "http://www.ishuyin.com/", desc="HTTP回退"))
    return {"name": "爱书音 ishuyin.com", "tests": all}


# ═══════════════════════════════════════════════════
# 7. 悦听巴 yuetingba.cn
# ═══════════════════════════════════════════════════

def test_yuetingba():
    all = []
    all.append(run_test("yuetingba-首页", "https://www.yuetingba.cn/", desc="基础连通性"))
    all.append(run_test("yuetingba-HTTP", "http://www.yuetingba.cn/", desc="HTTP回退"))
    return {"name": "悦听巴 yuetingba.cn", "tests": all}


# ═══════════════════════════════════════════════════
# 主函数
# ═══════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════╗")
    print("║     有声书网站爬取可行性测试 v2 (弱TLS兼容)           ║")
    print("╚══════════════════════════════════════════════════════╝")

    all_results = {}

    sites = [
        ("幻听网 ting89.com", test_ting89),
        ("88听书网 88tingshu.com", test_88tingshu),
        ("喜马拉雅 ximalaya.com", test_ximalaya),
        ("舒听网 shuting5.com", test_shuting5),
        ("中文听书网 tingzh.com", test_tingzh),
        ("爱书音 ishuyin.com", test_ishuyin),
        ("悦听巴 yuetingba.cn", test_yuetingba),
    ]

    for name, func in sites:
        print(f"\n{'='*60}")
        print(f"🔍 {name}")
        print(f"{'='*60}")
        try:
            all_results[name] = func()
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            import traceback
            traceback.print_exc()
            all_results[name] = {"name": name, "error": str(e)}

    # ── 最终总结 ──
    print("\n\n")
    print("╔══════════════════════════════════════════════════════╗")
    print("║                   📊 最终汇总                        ║")
    print("╚══════════════════════════════════════════════════════╝")

    summary = []
    for name, result in all_results.items():
        if "error" in result:
            summary.append(f"  ❌ {name}: 异常 - {result['error'][:80]}")
            continue

        tests = result.get("tests", [])
        ok = [t for t in tests if t["status"] == "OK"]
        fail = [t for t in tests if t["status"] != "OK"]

        # 判断整体可爬取性
        has_book = any("书籍" in t.get("name", "") or "album" in t.get("data", {}).get("title", [""])[0] if t.get("data") else False for t in ok)
        has_audio = any("音频" in t.get("name", "") or t.get("data", {}).get("audio_url", "").startswith("http") for t in ok)

        status = "✅ 可爬" if has_book else ("⚠️ 部分可爬" if ok else "❌ 不可爬")
        detail = f"{len(ok)}/{len(tests)} OK"
        if has_book:
            detail += ", 书籍页可解析"
        if has_audio:
            detail += ", 音频直链可提取"

        summary.append(f"  {status} {name}: {detail}")
        print(summary[-1])

    # 保存
    output = "e:/GitHub/AudioBook_dl/audiobook-dl/tests/crawl_test_results.json"
    with open(output, "w", encoding="utf-8") as f:
        # 清理不可序列化对象
        clean = {}
        for k, v in all_results.items():
            if isinstance(v, dict):
                clean[k] = {kk: vv for kk, vv in v.items() if kk != "resp"}
            else:
                clean[k] = str(v)
        json.dump(clean, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n📁 详细结果: {output}")


if __name__ == "__main__":
    main()