"""
网络基础设施 — 代理、DoH DNS、Clash 节点轮换、Session 构建

所有 Source 插件通过这个模块管理网络配置,
避免每个站点重复实现代理 / DNS / Clash 逻辑。
"""

import os
import re
import ssl
import socket
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 禁用 SSL 未验证警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# 代理管理 (全局单例)
# ══════════════════════════════════════════════════════════════

_proxy: Optional[str] = None


def set_proxy(proxy: Optional[str]):
    """设置全局代理, 格式: http://127.0.0.1:7890 或 socks5://127.0.0.1:1080"""
    global _proxy
    _proxy = proxy.strip() if proxy and proxy.strip() else None


def get_proxy() -> Optional[str]:
    """获取当前全局代理地址"""
    return _proxy


def detect_system_proxy() -> Optional[str]:
    """
    自动检测系统代理

    检测顺序:
    1. Windows 注册表
    2. 环境变量 (HTTPS_PROXY / HTTP_PROXY)
    3. 本地常见端口探测 (7890 / 7891 / 7897 / 1080)
    """
    # 1) Windows 注册表
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enable:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if server:
                    if "=" in server:
                        for part in server.split(";"):
                            if part.strip().startswith("http="):
                                server = part.strip()[5:]
                                break
                    if not server.startswith(("http://", "https://", "socks")):
                        server = "http://" + server
                    return server
    except Exception:
        pass

    # 2) 环境变量
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        val = os.environ.get(var)
        if val:
            return val

    # 3) 探测常见 Clash 端口
    for port in (7890, 7891, 7897, 1080):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            proto = "socks5" if port == 1080 else "http"
            return f"{proto}://127.0.0.1:{port}"
        except Exception:
            continue

    return None


# ══════════════════════════════════════════════════════════════
# DoH (DNS over HTTPS) — 防 DNS 污染
# ══════════════════════════════════════════════════════════════

# ── DNS 防污染 ──

_dns_cache: Dict[str, str] = {}
_doh_checked: set = set()  # 标记已检查过 DoH 的域名


def resolve_via_doh(domain: str) -> Optional[str]:
    """通过 DoH 解析域名, 绕过本地 DNS 污染"""
    if domain in _dns_cache:
        return _dns_cache[domain]

    doh_servers = [
        f"https://1.1.1.1/dns-query?name={domain}&type=A",
        f"https://8.8.8.8/resolve?name={domain}&type=A",
    ]
    # 使用独立 session 避免受全局代理/DNS 影响
    doh_session = requests.Session()
    doh_session.trust_env = False
    for url in doh_servers:
        try:
            resp = doh_session.get(
                url, headers={"Accept": "application/dns-json"},
                timeout=5, verify=False,
            )
            data = resp.json()
            ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
            if ips:
                _dns_cache[domain] = ips[0]
                return ips[0]
        except Exception:
            continue
    return None


def get_real_ip(domain: str) -> Optional[str]:
    """
    获取域名的真实IP（自动检测DNS污染）
    
    快速返回，不阻塞主流程。超时默认返回 None。
    """
    if domain in _doh_checked:
        return _dns_cache.get(domain)
    
    try:
        real_ip = resolve_via_doh(domain)
    except Exception:
        real_ip = None
    
    if not real_ip:
        return None
    
    try:
        local_ip = socket.getaddrinfo(domain, 443, socket.AF_INET)[0][4][0]
        if local_ip != real_ip:
            _doh_checked.add(domain)
            return real_ip
    except Exception:
        pass
    
    _doh_checked.add(domain)
    return None


def is_dns_poisoned(domain: str) -> bool:
    """检测本地 DNS 是否被污染"""
    try:
        local_ip = socket.getaddrinfo(domain, 443, socket.AF_INET)[0][4][0]
        real_ip = resolve_via_doh(domain)
        if real_ip and local_ip != real_ip:
            return True
    except Exception:
        pass
    return False


def get_host_resolver_rules(domains: List[str]) -> Optional[str]:
    """
    生成 Playwright --host-resolver-rules 参数，绕过 DNS 污染
    
    Args:
        domains: 需要修复的域名列表
    
    Returns:
        如 "MAP www.ting13.cc 38.49.208.62,MAP m.ting13.cc 38.49.208.62" 或 None
    """
    rules = []
    for domain in domains:
        real_ip = get_real_ip(domain)
        if real_ip:
            rules.append(f"MAP {domain} {real_ip}")
    return ",".join(rules) if rules else None


# ══════════════════════════════════════════════════════════════
# TLS 适配器 — 解决部分服务器 SSL 握手失败
# ══════════════════════════════════════════════════════════════

class _TLSAdapter(HTTPAdapter):
    """自定义 TLS 适配器, 降低安全级别以兼容非标 SSL 服务器"""

    def __init__(self, *args, sni_override: Optional[str] = None, **kwargs):
        self._sni_override = sni_override
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if self._sni_override:
            _orig_wrap = ctx.wrap_socket
            _sni = self._sni_override
            def _patched_wrap(sock, server_hostname=None, server_side=False, **kw):
                return _orig_wrap(sock, server_hostname=_sni, server_side=server_side, **kw)
            ctx.wrap_socket = _patched_wrap  # type: ignore[method-assign]
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# ══════════════════════════════════════════════════════════════
# ting13.cc SNI 修复 — ting13.cc 使用 Cloudflare CDN,
# 使用 DoH 动态解析当前有效 IP 并通过 SNI=ting13.cc 直连
# ══════════════════════════════════════════════════════════════

_TING13_SNI = "ting13.cc"
_TING13_WORKING_IP = "103.145.191.203"  # 备用静态IP
_TING13_RESOLVED_IP: Optional[str] = None  # DoH动态解析结果
_TING13_RESOLVED_TIME: float = 0.0

# ── 持久连接池 (避免每次请求都建新连接触发限速) ──

import threading as _threading

_ting13_conn: Optional[ssl.SSLSocket] = None
_ting13_conn_lock = _threading.RLock()


def _get_ting13_ip() -> Optional[str]:
    """获取 ting13.cc 的当前有效IP (优先DoH动态解析, 回退静态IP)"""
    global _TING13_RESOLVED_IP, _TING13_RESOLVED_TIME
    import time as _time
    if _TING13_RESOLVED_IP and _time.time() - _TING13_RESOLVED_TIME < 3600:
        return _TING13_RESOLVED_IP
    real_ip = resolve_via_doh("www.ting13.cc")
    if real_ip:
        _TING13_RESOLVED_IP = real_ip
        _TING13_RESOLVED_TIME = _time.time()
        return real_ip
    return _TING13_WORKING_IP


def _get_ting13_connection(timeout: int = 15) -> ssl.SSLSocket:
    """获取或创建到 ting13.cc 的持久 TLS 连接 (调用者需持有 _ting13_conn_lock)"""
    global _ting13_conn
    if _ting13_conn is None:
        ip = _get_ting13_ip()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        raw = socket.create_connection((ip, 443), timeout=timeout)
        _ting13_conn = ctx.wrap_socket(raw, server_hostname=_TING13_SNI)
        _ting13_conn.settimeout(timeout)
    return _ting13_conn


def _close_ting13_connection():
    """关闭持久连接"""
    global _ting13_conn
    with _ting13_conn_lock:
        if _ting13_conn:
            try:
                _ting13_conn.close()
            except Exception:
                pass
            _ting13_conn = None


def _reset_ting13_connection():
    """重置连接 (出错时调用)"""
    _close_ting13_connection()


def _is_ting13_domain(hostname: Optional[str]) -> bool:
    """检查域名是否属于 ting13.cc 系列"""
    return bool(hostname and "ting13.cc" in hostname)


def _fetch_via_ting13_sni(url: str, extra_headers: Optional[dict] = None, timeout: int = 8) -> Optional[bytes]:
    """
    通过持久 TLS 连接 + SNI=ting13.cc 获取 ting13.cc 页面内容
    """
    parsed = urlparse(url)
    host_header = parsed.hostname or "www.ting13.cc"
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    if not path:
        path = "/"

    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"User-Agent: {DEFAULT_UA}\r\n"
        f"Accept: */*\r\n"
        f"Connection: keep-alive\r\n"
    )
    if extra_headers:
        for k, v in extra_headers.items():
            req += f"{k}: {v}\r\n"
    req += "\r\n"

    with _ting13_conn_lock:
        try:
            ss = _get_ting13_connection(timeout=max(timeout, 10))
            ss.sendall(req.encode())

            data = b""
            ss.settimeout(timeout)
            content_length = None
            header_end = -1
            while True:
                try:
                    chunk = ss.recv(8192)
                    if not chunk:
                        break
                    data += chunk
                    header_end = data.find(b"\r\n\r\n")
                    if header_end >= 0:
                        headers_lower = data[:header_end].decode("utf-8", errors="replace").lower()
                        for line in headers_lower.split("\r\n"):
                            if line.startswith("content-length:"):
                                try:
                                    content_length = int(line.split(":", 1)[1].strip())
                                except Exception:
                                    pass
                        body_start = header_end + 4
                        if content_length is not None and len(data) - body_start >= content_length:
                            break
                        if content_length is None and "transfer-encoding: chunked" in headers_lower:
                            # 对于 chunked, 检查是否以 0\r\n\r\n 结尾
                            if data.rstrip().endswith(b"\r\n0\r\n\r\n"):
                                break
                except socket.timeout:
                    break
                except Exception:
                    _reset_ting13_connection()
                    return None

            if not data or header_end < 0:
                return None

            # 检查状态码
            status_line = data[: data.find(b"\r\n")].decode("utf-8", errors="replace")
            if "200" not in status_line and "302" not in status_line and "301" not in status_line:
                return None

            headers_lower = data[:header_end].decode("utf-8", errors="replace").lower()
            raw_body = data[header_end + 4:]

            if "transfer-encoding: chunked" in headers_lower:
                return _dechunk(raw_body)

            if content_length is not None:
                raw_body = raw_body[:content_length]

            return raw_body

        except Exception:
            _reset_ting13_connection()
            return None


def _dechunk(data: bytes) -> Optional[bytes]:
    """解码 chunked transfer encoding"""
    result = b""
    pos = 0
    while pos < len(data):
        # Find the chunk size line
        crlf = data.find(b"\r\n", pos)
        if crlf < 0:
            break
        try:
            chunk_size = int(data[pos:crlf], 16)
        except ValueError:
            break
        pos = crlf + 2
        if chunk_size == 0:
            break
        if pos + chunk_size > len(data):
            break
        result += data[pos:pos + chunk_size]
        pos += chunk_size + 2  # skip trailing \r\n
    return result if result else None


def _fetch_json_via_ting13_sni(url: str, extra_headers: Optional[dict] = None, timeout: int = 8) -> Optional[dict]:
    """通过 raw socket + SNI=ting13.cc 获取 ting13.cc JSON 数据"""
    import json as _json

    body = _fetch_via_ting13_sni(url, extra_headers=extra_headers, timeout=timeout)
    if body:
        try:
            return _json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            pass
    return None


# ── curl_cffi 辅助获取 (绕过 Cloudflare TLS 指纹检测) ──

def _fetch_via_cffi(url: str, headers: Optional[dict] = None, timeout: int = 10) -> Optional[bytes]:
    """使用 curl_cffi 获取内容 (模拟 Chrome TLS 指纹)"""
    try:
        from curl_cffi import requests as cffi_requests
        s = cffi_requests.Session(impersonate="chrome120")
        default_headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if headers:
            default_headers.update(headers)
        resp = s.get(url, headers=default_headers, timeout=timeout, verify=False)
        if resp.status_code == 200 and resp.text:
            return resp.text.encode("utf-8")
    except Exception:
        pass
    return None


def _fetch_json_via_cffi(url: str, headers: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
    """使用 curl_cffi 获取 JSON 数据"""
    import json as _json
    body = _fetch_via_cffi(url, headers=headers, timeout=timeout)
    if body:
        try:
            return _json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            pass
    return None


# ══════════════════════════════════════════════════════════════
# Session 构建
# ══════════════════════════════════════════════════════════════

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# User-Agent 池 — 模拟不同浏览器/设备, 降低被识别为爬虫的概率
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]


def random_ua() -> str:
    """从 UA 池中随机选取一个 User-Agent"""
    import random as _rnd
    return _rnd.choice(_UA_POOL)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)


def build_session(
    *,
    user_agent: str = DEFAULT_UA,
    referer: str = "",
    cookies: Optional[dict] = None,
    proxy: Optional[str] = None,
    use_tls_adapter: bool = True,
    max_retries: int = 3,
    use_proxy_pool: bool = False,
) -> requests.Session:
    """
    构建带重试、TLS 容错、cookies 和代理的 Session

    Args:
        user_agent: User-Agent 头
        referer: Referer 头
        cookies: 要注入的 cookies
        proxy: 代理地址 (None 则使用全局代理)
        use_tls_adapter: 是否使用自定义 TLS 适配器
        max_retries: 最大重试次数
        use_proxy_pool: 是否使用代理池 (优先级高于 proxy 参数)
    """
    session = requests.Session()
    # 避免 requests 自动读取环境变量代理导致链路被意外劫持
    session.trust_env = False

    if use_tls_adapter:
        retry = Retry(total=max_retries, backoff_factor=1,
                      status_forcelist=[502, 503, 504])
        adapter = _TLSAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

    session.headers.update({"User-Agent": user_agent})
    if referer:
        session.headers["Referer"] = referer

    if cookies:
        session.cookies.update(cookies)

    # 代理: 优先代理池, 其次参数, 否则全局; "__none__" 表示强制不用代理
    p = None
    if use_proxy_pool and _proxy_pool:
        p = _proxy_pool.get_current_proxy_url()
    if not p:
        p = proxy if proxy is not None else _proxy
    if p and p != "__none__":
        session.proxies = {"http": p, "https": p}

    return session


def fetch_page(url: str, **session_kwargs) -> bytes:
    """
    获取页面内容 (DoH 防污染 + SSL 容错 + 自动代理 + ting13 SNI修复 + curl_cffi)

    多策略回退: curl_cffi → SNI raw socket → 标准HTTP → DoH直连
    """
    session_kwargs.setdefault("max_retries", 0)

    parsed = urlparse(url)
    domain = parsed.hostname

    # ── ting13.cc 系列: 多策略 ──
    if _is_ting13_domain(domain):
        # 策略1: curl_cffi (绕过Cloudflare TLS指纹检测)
        result = _fetch_via_cffi(url, timeout=10)
        if result is not None:
            return result

        # 策略2: SNI raw socket
        body = _fetch_via_ting13_sni(url, timeout=8)
        if body is not None:
            return body

    session = build_session(**session_kwargs)

    # 第一次尝试：使用系统 DNS（短超时，快速失败）
    try:
        resp = session.get(url, timeout=8, verify=False)
        resp.raise_for_status()
        return resp.content
    except Exception:
        pass

    # 第二次尝试：DoH 解析真实 IP 直连（仅当 DNS 可能被污染时）
    if domain:
        real_ip = get_real_ip(domain)
        if real_ip:
            new_url = url.replace(f"://{domain}", f"://{real_ip}", 1)
            try:
                resp = session.get(new_url, headers={"Host": domain}, timeout=8, verify=False)
                resp.raise_for_status()
                return resp.content
            except Exception:
                pass

    # 第三次尝试：使用原 URL 再试最后一次
    resp = session.get(url, timeout=10, verify=False)
    resp.raise_for_status()
    return resp.content


def fetch_json(url: str, headers: Optional[dict] = None, timeout: int = 15, **session_kwargs) -> Optional[dict]:
    """
    获取JSON数据 (DoH 防污染 + 自动代理 + ting13 SNI修复 + curl_cffi)

    多策略回退: curl_cffi → SNI raw socket → 标准HTTP → DoH直连
    """
    session_kwargs.setdefault("max_retries", 0)
    
    parsed = urlparse(url)
    domain = parsed.hostname

    # ── ting13.cc 系列: 多策略 ──
    if _is_ting13_domain(domain):
        # 策略1: curl_cffi JSON
        result = _fetch_json_via_cffi(url, headers=headers, timeout=timeout)
        if result is not None:
            return result

        # 策略2: SNI raw socket JSON
        result = _fetch_json_via_ting13_sni(url, extra_headers=headers, timeout=timeout)
        if result is not None:
            return result

    session = build_session(**session_kwargs)
    
    # 第一次尝试
    try:
        resp = session.get(url, headers=headers or {}, timeout=timeout, verify=False)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    
    # 第二次：DoH 真实 IP 直连
    if domain:
        real_ip = get_real_ip(domain)
        if real_ip:
            new_url = url.replace(f"://{domain}", f"://{real_ip}", 1)
            try:
                resp = session.get(new_url, headers=dict(headers or {}, **{"Host": domain}), timeout=timeout, verify=False)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass
    
    return None


# ══════════════════════════════════════════════════════════════
# Clash API 集成 — 自动轮换代理节点
# ══════════════════════════════════════════════════════════════

class ClashRotator:
    """通过 Clash External Controller API 自动轮换代理节点"""

    COMMON_PORTS = [9090, 9097, 36925]

    def __init__(self, api_url: Optional[str] = None, secret: str = ""):
        self.api_url = api_url.rstrip("/") if api_url else ""
        self.secret = secret
        self.group_name: Optional[str] = None
        self.nodes: List[str] = []
        self.current_idx: int = -1
        self._headers: dict = {"Content-Type": "application/json"}
        if secret:
            self._headers["Authorization"] = f"Bearer {secret}"

    # ── 检测 ──

    def auto_detect(self) -> bool:
        """自动检测 Clash API 地址和密钥"""
        self._try_read_clash_config()
        if self.api_url and self._ping(self.api_url):
            return True
        for port in self.COMMON_PORTS:
            url = f"http://127.0.0.1:{port}"
            if self._ping(url):
                self.api_url = url
                return True
        return False

    def _try_read_clash_config(self):
        """尝试从 Clash Verge 配置文件读取 API 地址和密钥"""
        config_dirs = [
            os.path.join(os.path.expanduser("~"), "AppData", "Roaming",
                         "io.github.clash-verge-rev.clash-verge-rev"),
            os.path.join(os.path.expanduser("~"), ".config", "clash-verge-rev"),
            os.path.join(os.path.expanduser("~"), ".config", "clash-verge"),
        ]
        for config_dir in config_dirs:
            config_file = os.path.join(config_dir, "config.yaml")
            if os.path.isfile(config_file):
                try:
                    with open(config_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    for line in content.splitlines():
                        line = line.strip()
                        if line.startswith("external-controller:"):
                            addr = line.split(":", 1)[1].strip()
                            if addr and not self.api_url:
                                self.api_url = f"http://{addr}"
                        if line.startswith("secret:"):
                            val = line.split(":", 1)[1].strip()
                            if val and not self.secret:
                                self.secret = val
                                self._headers["Authorization"] = f"Bearer {val}"
                    if self.api_url:
                        break
                except Exception:
                    continue

    def _ping(self, url: str) -> bool:
        try:
            resp = requests.get(
                f"{url}/version", headers=self._headers, timeout=2
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ── 加载节点 ──

    def load_nodes(self) -> List[str]:
        """获取可切换的代理节点列表"""
        if not self.api_url:
            return []
        try:
            resp = requests.get(
                f"{self.api_url}/proxies",
                headers=self._headers, timeout=5,
            )
            data = resp.json()
            proxies = data.get("proxies", {})

            group_types = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
            special = {"DIRECT", "REJECT", "GLOBAL", "COMPATIBLE"}

            selector_groups = [
                (name, info)
                for name, info in proxies.items()
                if info.get("type") == "Selector" and name not in special
            ]
            if not selector_groups:
                return []

            selector_groups.sort(
                key=lambda x: len(x[1].get("all", [])), reverse=True
            )
            group_name, group_info = selector_groups[0]
            self.group_name = group_name

            self.nodes = [
                n for n in group_info.get("all", [])
                if n not in special
                and proxies.get(n, {}).get("type", "") not in group_types
            ]

            current = group_info.get("now", "")
            if current in self.nodes:
                self.current_idx = self.nodes.index(current)
            else:
                self.current_idx = 0

            return self.nodes
        except Exception:
            return []

    # ── 切换 ──

    def rotate(self) -> Optional[str]:
        """切换到下一个节点, 返回新节点名"""
        if not self.nodes or not self.group_name:
            return None
        self.current_idx = (self.current_idx + 1) % len(self.nodes)
        node_name = self.nodes[self.current_idx]
        try:
            encoded_group = quote(self.group_name, safe="")
            resp = requests.put(
                f"{self.api_url}/proxies/{encoded_group}",
                json={"name": node_name},
                headers=self._headers, timeout=5,
            )
            if resp.status_code in (200, 204):
                return node_name
        except Exception:
            pass
        return None

    def get_current_node(self) -> Optional[str]:
        if self.nodes and 0 <= self.current_idx < len(self.nodes):
            return self.nodes[self.current_idx]
        return None


# ══════════════════════════════════════════════════════════════
# Proxy Pool 集成 — 从代理池获取 IP
# ══════════════════════════════════════════════════════════════

class ProxyPool:
    """通过代理池 API 自动获取和释放代理 IP"""

    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self._current_proxy: Optional[dict] = None
        self._headers = {"X-API-Key": api_key}

    def get_proxy(self) -> Optional[dict]:
        """
        从代理池获取一个新代理
        
        Returns:
            {
                "http": "http://ip:port",
                "https": "http://ip:port",
                "ip": "ip地址",
                "port": 端口号
            }
        """
        try:
            resp = requests.get(
                f"{self.api_url}/api/proxy/get",
                headers=self._headers,
                timeout=10
            )
            data = resp.json()
            if data.get("code") == 200:
                proxy_data = data["data"]
                ip = proxy_data["ip"]
                port = proxy_data["port"]
                proxy_url = f"http://{ip}:{port}"
                self._current_proxy = {
                    "http": proxy_url,
                    "https": proxy_url,
                    "ip": ip,
                    "port": port
                }
                return self._current_proxy
        except Exception:
            pass
        return None

    def release_proxy(self, success: bool = True):
        """释放当前使用的代理"""
        if not self._current_proxy:
            return
        try:
            requests.post(
                f"{self.api_url}/api/proxy/release",
                headers={**self._headers, "Content-Type": "application/json"},
                json={
                    "ip": self._current_proxy["ip"],
                    "port": self._current_proxy["port"],
                    "success": success
                },
                timeout=5
            )
        except Exception:
            pass
        self._current_proxy = None

    def rotate(self) -> Optional[dict]:
        """轮换到新代理（先释放旧的，再获取新的）"""
        self.release_proxy(success=True)
        return self.get_proxy()

    def get_current_proxy_url(self) -> Optional[str]:
        """获取当前代理的 URL（用于 build_session）"""
        if self._current_proxy:
            return self._current_proxy["http"]
        return None


# ══════════════════════════════════════════════════════════════
# 全局代理池单例
# ══════════════════════════════════════════════════════════════

_proxy_pool: Optional[ProxyPool] = None


def set_proxy_pool(api_url: Optional[str], api_key: Optional[str]):
    """设置全局代理池"""
    global _proxy_pool
    if api_url and api_key:
        _proxy_pool = ProxyPool(api_url, api_key)
    else:
        _proxy_pool = None


def get_proxy_pool() -> Optional[ProxyPool]:
    """获取全局代理池"""
    return _proxy_pool
