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
from urllib.parse import quote

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

_dns_cache: Dict[str, str] = {}


def resolve_via_doh(domain: str) -> Optional[str]:
    """通过 DoH 解析域名, 绕过本地 DNS 污染"""
    if domain in _dns_cache:
        return _dns_cache[domain]

    doh_servers = [
        f"https://1.1.1.1/dns-query?name={domain}&type=A",
        f"https://8.8.8.8/resolve?name={domain}&type=A",
    ]
    for url in doh_servers:
        try:
            resp = requests.get(
                url, headers={"Accept": "application/dns-json"},
                timeout=8, verify=False,
            )
            data = resp.json()
            ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
            if ips:
                _dns_cache[domain] = ips[0]
                return ips[0]
        except Exception:
            continue
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


# ══════════════════════════════════════════════════════════════
# TLS 适配器 — 解决部分服务器 SSL 握手失败
# ══════════════════════════════════════════════════════════════

class _TLSAdapter(HTTPAdapter):
    """自定义 TLS 适配器, 降低安全级别以兼容非标 SSL 服务器"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


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
    """
    session = requests.Session()

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

    # 代理: 优先参数, 否则全局; "__none__" 表示强制不用代理
    p = proxy if proxy is not None else _proxy
    if p and p != "__none__":
        session.proxies = {"http": p, "https": p}

    return session


def fetch_page(url: str, **session_kwargs) -> bytes:
    """
    获取页面内容 (带 SSL 容错、重试、DoH DNS 防污染)

    支持传入 build_session 的关键字参数来自定义 Session。
    """
    from urllib.parse import urlparse

    session = build_session(**session_kwargs)

    # 先尝试直接请求
    try:
        resp = session.get(url, timeout=30, verify=False)
        resp.raise_for_status()
        return resp.content
    except Exception:
        pass

    # 如果失败, 尝试 DoH 解析真实 IP
    parsed = urlparse(url)
    domain = parsed.hostname
    if domain:
        real_ip = resolve_via_doh(domain)
        if real_ip:
            try:
                local_ip = socket.getaddrinfo(domain, 443, socket.AF_INET)[0][4][0]
            except Exception:
                local_ip = None
            if local_ip and local_ip != real_ip:
                new_url = url.replace(f"://{domain}", f"://{real_ip}", 1)
                headers = {"Host": domain}
                resp = session.get(new_url, headers=headers, timeout=30, verify=False)
                resp.raise_for_status()
                return resp.content

    # 最后兜底
    resp = session.get(url, timeout=30, verify=False)
    resp.raise_for_status()
    return resp.content


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
