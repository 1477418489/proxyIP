import asyncio
from contextlib import asynccontextmanager

import aiohttp
from fastapi.testclient import TestClient
import pytest

from cfproxy import network
from cfproxy.app import create_app
from cfproxy.config import Settings
from cfproxy.db import Database, PRESETS
from cfproxy.parsing import Seed, utcnow
from test_probes import certificates, tls_server, response


@asynccontextmanager
async def http_proxy(tcp_port, *, status=200):
    """An actual CONNECT relay that records the target and request headers."""
    requests = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        remote = None
        pumps = []
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            if status != 200:
                writer.write(f"HTTP/1.1 {status} Rejected\r\nContent-Length: 0\r\n\r\n".encode())
                await writer.drain()
                return
            incoming, remote = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()

            async def pipe(source, sink):
                while data := await source.read(65536):
                    sink.write(data)
                    await sink.drain()

            pumps = [asyncio.create_task(pipe(reader, remote)), asyncio.create_task(pipe(incoming, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (writer, remote):
                if connection is not None:
                    connection.close()
                    try:
                        await connection.wait_closed()
                    except OSError:
                        pass
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], requests
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("url", ["http://127.0.0.1:7890", "http://user:p%40ss@192.168.1.10:8080/", "http://[::1]:7890", "http://proxy.example:3128"])
def test_valid_proxy_url(url):
    assert network.validate_proxy_url(url) == url


@pytest.mark.parametrize("url", ["socks5://127.0.0.1:1080", "https://proxy.example:443", "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:7890/path", "http://a:pass-marker@proxy.example:7890?x=y", "http://a:pass%0Dmarker@127.0.0.1:7890", "http://0.0.0.0:7890", "http://127.1:7890"])
def test_proxy_url_errors_do_not_echo_credentials(url):
    with pytest.raises(ValueError) as error:
        network.validate_proxy_url(url)
    assert "pass-marker" not in str(error.value)
    assert url not in str(error.value)


@pytest.mark.parametrize("source_ip,authority", [("8.8.8.8", "8.8.8.8:8443"), ("2606:4700:4700::1111", "[2606:4700:4700::1111]:8443")])
async def test_proxy_preserves_tls_sni_host_and_pins_connect_ip(certificates, monkeypatch, source_ip, authority):
    resolved = []
    observed_sni = []
    async def resolver(host, port):
        resolved.append((host, port))
        return [source_ip]
    monkeypatch.setattr(network, "resolve_public", resolver)
    certificates["valid"].set_servername_callback(lambda sock, name, context: observed_sni.append(name))
    async with tls_server(certificates["valid"], response(b"8.8.4.4:443\n")) as (port, origin_requests, _):
        async with http_proxy(port) as (proxy_port, connect_requests):
            result = await network.fetch_text(
                "https://www.cloudflare.com:8443/list.txt?version=1",
                proxy_url=f"http://alice:s3cr3t@127.0.0.1:{proxy_port}",
                ssl_context=certificates["client"],
            )
    assert result == "8.8.4.4:443\n"
    assert resolved == [("www.cloudflare.com", 8443)]
    assert connect_requests[0].startswith(f"CONNECT {authority} HTTP/1.1\r\n".encode())
    assert b"Proxy-Authorization: Basic YWxpY2U6czNjcjN0\r\n" in connect_requests[0]
    assert b"Host: www.cloudflare.com:8443\r\n" in origin_requests[0]
    assert b"GET /list.txt?version=1 HTTP/1.1" in origin_requests[0]
    assert b"Proxy-Authorization:" not in origin_requests[0]
    assert observed_sni == ["www.cloudflare.com"]


async def test_proxy_does_not_disable_certificate_validation(certificates, monkeypatch):
    async def resolver(*args):
        return ["8.8.8.8"]
    monkeypatch.setattr(network, "resolve_public", resolver)
    async with tls_server(certificates["wrong"]) as (port, _, _):
        async with http_proxy(port) as (proxy_port, _):
            with pytest.raises(network.SourceFetchError, match="证书"):
                await network.fetch_text("https://www.cloudflare.com/list.txt", proxy_url=f"http://127.0.0.1:{proxy_port}", ssl_context=certificates["client"])


@pytest.mark.parametrize("payload,message", [
    (b"HTTP/1.1 200 OK\r\nContent-Length: 2097153\r\n\r\n", "上限"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n200001\r\n" + b"x" * 2097153 + b"\r\n0\r\n\r\n", "上限"),
    (response(b"compressed-body", extra_headers=b"Content-Encoding: gzip\r\n"), "压缩"),
    (response(b"moved", status=302), "302"),
], ids=["declared-limit", "stream-limit", "compressed", "redirect"])
async def test_proxy_download_bounds_and_redirects(certificates, monkeypatch, payload, message):
    async def resolver(*args):
        return ["8.8.8.8"]
    monkeypatch.setattr(network, "resolve_public", resolver)
    async with tls_server(certificates["valid"], payload) as (port, _, _):
        async with http_proxy(port) as (proxy_port, connections):
            with pytest.raises(network.SourceFetchError, match=message):
                await network.fetch_text("https://www.cloudflare.com/list.txt", proxy_url=f"http://127.0.0.1:{proxy_port}", ssl_context=certificates["client"])
    assert len(connections) == 1


async def test_proxy_auth_error_is_safe_and_no_fallback(monkeypatch):
    async def resolver(*args):
        return ["8.8.8.8"]
    monkeypatch.setattr(network, "resolve_public", resolver)
    async with http_proxy(0, status=407) as (port, requests):
        with pytest.raises(network.SourceFetchError, match="鉴权") as error:
            await network.fetch_text("https://www.cloudflare.com/list.txt", proxy_url=f"http://user:private-marker@127.0.0.1:{port}")
    assert "private-marker" not in str(error.value)
    assert len(requests) == 1


async def test_proxy_failure_never_requests_source_directly(monkeypatch):
    calls = []
    async def resolver(*args):
        return ["8.8.8.8"]
    monkeypatch.setattr(network, "resolve_public", resolver)

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            await self.connector.close()
        def get(self, url, **kwargs):
            calls.append(kwargs)
            raise aiohttp.ClientConnectionError("http://user:private-marker@proxy.example:7890")

    monkeypatch.setattr(network.aiohttp, "ClientSession", Session)
    with pytest.raises(network.SourceFetchError) as error:
        await network.fetch_text("https://www.cloudflare.com/list.txt", proxy_url="http://user:private-marker@proxy.example:7890")
    assert len(calls) == 1 and calls[0]["proxy"] is not None
    assert "private-marker" not in str(error.value)


async def test_proxy_rejects_private_source_before_connect(monkeypatch):
    calls = []
    monkeypatch.setattr(network.aiohttp, "ClientSession", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError):
        await network.fetch_text("https://127.0.0.1/secret", proxy_url="http://127.0.0.1:7890")
    assert not calls


def test_fetch_api_compatibility_precedence_and_secret_redaction(tmp_path, monkeypatch):
    configured = "http://default:private-marker@127.0.0.1:7890"
    supplied = "http://request:other-marker@127.0.0.1:7891"
    calls = []
    async def fetch(url, *, proxy_url):
        calls.append(proxy_url)
        return "IP,端口\n8.8.8.8,443"
    monkeypatch.setattr("cfproxy.app.fetch_text", fetch)
    app = create_app(Settings(db_path=str(tmp_path / "api.sqlite3"), fetch_proxy=configured))
    with TestClient(app, base_url="http://localhost") as client:
        status = client.get("/api/status")
        assert status.json()["fetch_proxy_configured"] is True
        assert "private-marker" not in status.text
        for payload in (None, {"use_proxy": False, "proxy_url": supplied}, {"use_proxy": True}, {"use_proxy": True, "proxy_url": supplied}):
            result = client.post("/api/sources/1/fetch", json=payload) if payload is not None else client.post("/api/sources/1/fetch")
            assert result.status_code == 200, result.text
            assert "marker" not in result.text
        assert calls == [None, None, configured, supplied]
        for body in ({"use_proxy": True, "proxy_url": {"password": "private-marker"}}, {"use_proxy": "private-marker"}, {"extra": "private-marker"}, {"proxy_url": "private-marker" * 500}):
            result = client.post("/api/sources/1/fetch", json=body)
            assert result.status_code == 422
            assert "private-marker" not in result.text
        result = client.post("/api/sources/1/fetch", json={"use_proxy": True, "proxy_url": "http://user:private-marker@proxy:0"})
        assert result.status_code == 400 and "private-marker" not in result.text
        assert "marker" not in client.get("/api/sources").text
    assert b"private-marker" not in (tmp_path / "api.sqlite3").read_bytes()


def test_missing_proxy_rejected(tmp_path):
    with TestClient(create_app(Settings(db_path=str(tmp_path / "missing.sqlite3"))), base_url="http://localhost") as client:
        result = client.post("/api/sources/1/fetch", json={"use_proxy": True})
        assert result.status_code == 400
        assert "CFPROXY_FETCH_PROXY" in result.text


def test_new_presets_preserve_old_sources_and_history(tmp_path, monkeypatch):
    from cfproxy import db as storage

    monkeypatch.setattr(storage, "PRESETS", PRESETS[:2])
    path = tmp_path / "upgrade.sqlite3"
    db = Database(path)
    custom = db.add_source("我的来源名", PRESETS[2][1], "auto")
    db.record_fetch(custom["id"], count=4, error="旧记录")
    for index in range(49):
        db.add_source(f"custom-{index}", f"https://example.com/{index}.txt", "txt")
    db.import_seeds([Seed("8.8.8.8", 443, "test", "8.8.8.8", utcnow())], {"duplicates": 0, "truncated": False})
    candidate = db.candidates()[0]
    job, _ = db.create_job([candidate], {"mode": "local", "samples": 1, "targets": ["www.cloudflare.com"]})
    db.finish_job(job["id"], "cancelled", "保留的历史记录")
    history = db.history(candidate["id"])
    before = db.source(custom["id"])
    monkeypatch.setattr(storage, "PRESETS", PRESETS)
    upgraded = Database(path)
    assert upgraded.source(custom["id"]) == before
    assert len(upgraded.candidates()) == 1
    assert upgraded.history(candidate["id"]) == history
    assert upgraded.counts()["sources"] == 49 + len(PRESETS)
    Database(path)
    assert upgraded.counts()["sources"] == 49 + len(PRESETS)
    upgraded.add_source("last", "https://example.com/last.txt", "txt")
    with pytest.raises(ValueError, match="自定义"):
        upgraded.add_source("overflow", "https://example.com/overflow.txt", "txt")
