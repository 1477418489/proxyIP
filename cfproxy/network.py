"""Public-address-only DNS; the validated records are the records actually dialed."""

import asyncio
import ipaddress
import re
import socket
import ssl
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from yarl import URL

MAX_DOWNLOAD = 2 * 1024 * 1024
# Keep the same conservative policy in Python 3.11+ and the Worker. The
# standard library's special-address tables differ across Python releases.
IPV4_DENY = tuple(ipaddress.ip_network(cidr) for cidr in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
))
IPV6_DENY = tuple(ipaddress.ip_network(cidr) for cidr in (
    "2001::/23", "2001:db8::/32", "2002::/16", "3ffe::/16", "3fff::/20",
))


def public_ip(value: str) -> str:
    if "%" in value:
        raise ValueError("不接受带 zone ID 的地址")
    ip = ipaddress.ip_address(value)
    if ip.version == 4 and any(ip in block for block in IPV4_DENY):
        raise ValueError("只接受公网单播 IP")
    if ip.version == 6 and (
        ip not in ipaddress.ip_network("2000::/3")
        or any(ip in block for block in IPV6_DENY)
    ):
        raise ValueError("只接受公网单播 IP")
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))
    ):
        raise ValueError("只接受公网单播 IP")
    return str(ip)


async def resolve_public(host: str, port: int = 443) -> list[str]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [public_ip(host)]
    async with asyncio.timeout(5):
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    # Reject mixed public/private records, rather than silently selecting a safe one.
    addresses = list(dict.fromkeys(public_ip(record[4][0]) for record in records))
    if not addresses:
        raise ValueError("域名没有可用 A/AAAA 记录")
    if len(addresses) > 16:
        raise ValueError("单域名解析结果超过 16 个，请导入具体 IP")
    return addresses


class SafeResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await resolve_public(host, port)
        return [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": socket.AF_INET6 if ":" in ip else socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
            for ip in addresses
            if family == socket.AF_UNSPEC
            or family == (socket.AF_INET6 if ":" in ip else socket.AF_INET)
        ]

    async def close(self):
        pass


def validate_https_url(value: str, *, root_only=False) -> str:
    if len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("URL 过长或含空白字符")
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.fragment
        or "\\" in value
        or "%" in url.hostname
    ):
        raise ValueError("只接受不含凭据和片段的 HTTPS URL")
    try:
        port = url.port
    except ValueError as exc:
        raise ValueError("URL 端口无效") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL 端口无效")
    if root_only and (url.path not in ("", "/") or url.query):
        raise ValueError("Worker URL 请填写 HTTPS 根地址，不含路径或查询参数")
    try:
        ipaddress.ip_address(url.hostname)
    except ValueError:
        if re.fullmatch(r"[0-9.]+", url.hostname) or ":" in url.hostname:
            raise ValueError("URL IP 地址格式不正确")
        if "." not in url.hostname or url.hostname.lower().endswith((".localhost", ".local")):
            raise ValueError("URL 必须使用公网域名或 IP")
    else:
        public_ip(url.hostname)
    return value


def validate_proxy_url(value: str) -> str:
    """Explicitly chosen download proxy; loopback/private proxy hosts are valid."""
    message = "代理地址须为 http://[用户名:密码@]主机:端口，不含路径或查询参数"
    try:
        if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
            raise ValueError
        parsed = URL(value)
        raw = urlsplit(value)
        if (
            parsed.scheme != "http" or not parsed.host
            or raw.port is None or not 1 <= raw.port <= 65535
            or raw.path not in ("", "/") or raw.query or raw.fragment
            or "%" in parsed.host
            or (parsed.user is not None and ":" in parsed.user)
            or (parsed.password and not parsed.user)
        ):
            raise ValueError
        for field in (parsed.user or "", parsed.password or ""):
            if any(ord(c) < 32 or ord(c) == 127 for c in field):
                raise ValueError
        # Reject a target with an unspecified or multicast literal. Private
        # addresses are intentionally permitted for local proxy applications.
        try:
            address = ipaddress.ip_address(parsed.host)
        except ValueError:
            if ":" in parsed.host or re.fullmatch(r"[0-9.]+", parsed.host):
                raise ValueError
        else:
            if address.is_unspecified or address.is_multicast:
                raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(message) from None
    return value


class SourceFetchError(ValueError):
    """A fixed, credential-free diagnostic that can be persisted and shown."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


async def fetch_text(url: str, *, proxy_url: str | None = None, ssl_context: ssl.SSLContext | None = None) -> str:
    content = await fetch_bytes(url, proxy_url=proxy_url, ssl_context=ssl_context)
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceFetchError("来源必须使用 UTF-8 编码") from None


async def fetch_bytes(
    url: str, *, proxy_url: str | None = None, ssl_context: ssl.SSLContext | None = None,
    max_bytes: int = MAX_DOWNLOAD, timeout: float = 20,
) -> bytes:
    """Bounded HTTPS download shared by lists and the explicit GeoIP update."""
    if not 0 < max_bytes <= 8 * 1024 * 1024 or not 0 < timeout <= 60:
        raise ValueError("下载参数超出允许范围")
    validate_https_url(url)
    if proxy_url is not None:
        validate_proxy_url(proxy_url)
    try:
        async with asyncio.timeout(timeout):
            return await _fetch_bytes(url, proxy_url, ssl_context, max_bytes, timeout)
    except (SourceFetchError, asyncio.CancelledError):
        raise
    except aiohttp.ClientHttpProxyError as exc:
        if exc.status == 407:
            raise SourceFetchError("代理鉴权失败，请检查用户名和密码") from None
        raise SourceFetchError("代理拒绝建立 HTTPS 通道，请检查代理服务") from None
    except aiohttp.ClientProxyConnectionError:
        raise SourceFetchError("无法连接代理，请检查地址、端口以及后端机器到代理的连接") from None
    except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError):
        raise SourceFetchError("来源 HTTPS 证书验证失败") from None
    except TimeoutError:
        raise SourceFetchError("通过代理拉取超时" if proxy_url else "来源拉取超时") from None
    except ValueError:
        raise SourceFetchError("来源 DNS/IP 校验失败，只允许公网地址") from None
    except (aiohttp.ClientError, OSError):
        raise SourceFetchError("通过代理拉取失败，请检查代理与来源网络" if proxy_url else "来源网络请求失败，请检查 HTTPS、DNS 和网络") from None


async def _fetch_bytes(url, proxy_url, ssl_context, max_bytes, timeout):
    headers = {"User-Agent": "cfPROXYIP/0.3", "Accept": "*/*", "Accept-Encoding": "identity"}
    request_options = {}
    request_url = url
    if proxy_url:
        source = URL(url)
        addresses = await resolve_public(source.host, source.port or 443)
        # CONNECT must use the validated literal: giving the original hostname
        # to the proxy would allow a second, unchecked DNS resolution there.
        address = next((ip for ip in addresses if ":" not in ip), addresses[0])
        request_url = source.with_host(address)
        headers["Host"] = source.raw_authority
        request_options["server_hostname"] = source.host
        proxy = URL(proxy_url)
        request_options["proxy"] = proxy.with_user(None)
        if proxy.user is not None:
            request_options["proxy_auth"] = aiohttp.BasicAuth(proxy.user, proxy.password or "", encoding="utf-8")
        # The explicitly supplied proxy can be local; only its resolver is
        # unrestricted. The source address above remains pinned and public.
        connector = aiohttp.TCPConnector(use_dns_cache=False)
    else:
        connector = aiohttp.TCPConnector(resolver=SafeResolver(), use_dns_cache=False)
    if ssl_context is not None:
        request_options["ssl"] = ssl_context  # internal test CA; no API flag
    async with aiohttp.ClientSession(
        connector=connector, timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False,
        auto_decompress=False,
    ) as session:
        async with session.get(
            request_url,
            allow_redirects=False,
            headers=headers,
            **request_options,
        ) as response:
            if response.status != 200:
                raise SourceFetchError(f"来源返回 HTTP {response.status}（不跟随重定向）", status_code=response.status)
            if response.headers.get("Content-Encoding", "identity").lower().strip() != "identity":
                raise SourceFetchError("来源使用了 HTTP 压缩编码；只接受 identity 响应")
            limit_message = f"来源超过 {max_bytes // (1024 * 1024)} MiB 下载上限"
            if response.content_length and response.content_length > max_bytes:
                raise SourceFetchError(limit_message)
            content = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise SourceFetchError(limit_message)
    return bytes(content)
