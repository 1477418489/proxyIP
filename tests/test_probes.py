"""Real OpenSSL handshakes, including TLS through an actual WebSocket relay."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import socket
import ssl

import aiohttp
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from cfproxy import probes

TARGET = probes.TARGETS[0]
TOKEN = "test-worker-token-with-32-characters"
TRACE = f"h={TARGET}\nip=1.2.3.4\ncolo=HKG\ntls=TLSv1.3\n".encode()


def response(body=TRACE, status=200, extra_headers=b""):
    return (
        f"HTTP/1.1 {status} Result\r\nContent-Length: {len(body)}\r\n".encode()
        + extra_headers
        + b"Connection: close\r\n\r\n"
        + body
    )


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    folder = tmp_path_factory.mktemp("probe-certificates")
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cfproxy test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = folder / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))

    def server_context(name, hosts, expired=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
            .issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=10))
            .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=10))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host) for host in hosts]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        cert_path = folder / f"{name}.pem"
        key_path = folder / f"{name}.key"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        return context

    client = ssl.create_default_context(cafile=str(ca_path))
    return {
        "client": client,
        "valid": server_context("valid", [*probes.TARGETS, "probe.test"]),
        "wrong": server_context("wrong", ["wrong.example"]),
        "expired": server_context("expired", [*probes.TARGETS], expired=True),
    }


@asynccontextmanager
async def tls_server(context, payload=response(), *, stall=False, fragmented=False, abrupt=False):
    requests = []
    tasks = set()
    disconnected = asyncio.Event()

    async def handler(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            if stall:
                await reader.read()
            elif fragmented:
                for offset in range(0, len(payload), 7):
                    writer.write(payload[offset : offset + 7])
                    await writer.drain()
                    await asyncio.sleep(0)
            else:
                writer.write(payload)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            if abrupt:
                writer.transport.abort()
            else:
                writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass
            disconnected.set()
            tasks.discard(task)

    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1], requests, disconnected
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def allow_test_loopback(monkeypatch):
    # This bypass exists only in tests; the production probe API has no flag
    # that disables public-address validation.
    monkeypatch.setattr(probes, "public_ip", lambda value: str(ipaddress.ip_address(value)))


@pytest.mark.asyncio
async def test_local_real_tls_and_expected_sni(certificates, allow_test_loopback):
    observed_sni = []
    certificates["valid"].set_servername_callback(lambda sock, name, ctx: observed_sni.append(name))
    async with tls_server(certificates["valid"], fragmented=True) as (port, requests, _):
        result = await probes.probe("127.0.0.1", port, TARGET, ssl_context=certificates["client"])
    assert result["ok"] is True, result
    assert result["stage"] == "complete"
    assert result["exit_ip"] == "1.2.3.4"
    assert result["exit_colo"] == "HKG"
    assert result["exit_country"] is None
    assert result["worker_colo"] is None
    assert result["ttfb_ms"] >= result["tls_ms"] >= 0
    assert observed_sni[-1] == TARGET
    assert f"Host: {TARGET}\r\n".encode() in requests[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("certificate", ["wrong", "expired"])
async def test_certificate_errors_fail(certificates, allow_test_loopback, certificate):
    async with tls_server(certificates[certificate]) as (port, _, _):
        result = await probes.probe("127.0.0.1", port, TARGET, ssl_context=certificates["client"])
    assert result["ok"] is False
    assert result["stage"] == "tls"
    assert "SSLCertVerificationError" in result["error"]
    assert result["exit_country"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("loc,expected", [
    ("JP", "JP"), ("us", "US"), ("FR", "FR"), (None, None),
    ("XX", None), ("T1", None), ("ZZ", None), ("QQ", None), ("Japan", None),
])
async def test_country_metadata_does_not_change_availability(
    certificates, allow_test_loopback, loc, expected
):
    body = TRACE if loc is None else TRACE + f"loc={loc}\n".encode()
    async with tls_server(certificates["valid"], response(body)) as (port, _, _):
        result = await probes.probe("127.0.0.1", port, TARGET, ssl_context=certificates["client"])
    assert result["ok"] is True, result
    assert result["exit_country"] == expected
    assert result["exit_colo"] == "HKG"


@pytest.mark.asyncio
async def test_untrusted_certificate_fails(certificates, allow_test_loopback):
    async with tls_server(certificates["valid"]) as (port, _, _):
        result = await probes.probe("127.0.0.1", port, TARGET)
    assert result["ok"] is False
    assert result["stage"] == "tls"


BAD_RESPONSES = [
    response(b"<html>not trace</html>"),
    response(status=302, extra_headers=b"Location: https://example.com/\r\n"),
    response(TRACE.replace(TARGET.encode(), b"wrong.example")),
    response(TRACE.replace(b"ip=1.2.3.4", b"ip=garbage")),
    response(TRACE.replace(b"colo=HKG", b"colo=hk")),
    response(TRACE + b"h=duplicate\n"),
    b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n" + TRACE,
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n40\r\n" + TRACE,
    b"HTTP/1.1 200 OK\r\nContent-Length: 65537\r\n\r\n",
    response(extra_headers=b"X-Large: " + b"x" * 16384 + b"\r\n"),
    response(extra_headers=b"Content-Encoding: gzip\r\n"),
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n"
    + f"{len(TRACE):x}\r\n".encode() + TRACE + b"\r\n0\r\n\r\n",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", BAD_RESPONSES)
async def test_invalid_or_incomplete_http_fails(certificates, allow_test_loopback, payload):
    async with tls_server(certificates["valid"], payload) as (port, _, _):
        result = await probes.probe("127.0.0.1", port, TARGET, ssl_context=certificates["client"])
    assert result["ok"] is False, payload
    assert result["stage"] == "http"


@pytest.mark.asyncio
async def test_complete_chunked_response(certificates, allow_test_loopback):
    payload = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + f"{len(TRACE):x}\r\n".encode() + TRACE + b"\r\n0\r\n\r\n"
    )
    async with tls_server(certificates["valid"], payload, fragmented=True) as (port, _, _):
        result = await probes.probe("127.0.0.1", port, TARGET, ssl_context=certificates["client"])
    assert result["ok"] is True, result


@pytest.mark.asyncio
async def test_clean_close_delimited_response(certificates, allow_test_loopback):
    payload = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + TRACE
    async with tls_server(certificates["valid"], payload) as (port, _, _):
        result = await probes.probe("127.0.0.1", port, TARGET, ssl_context=certificates["client"])
    assert result["ok"] is True, result


@pytest.mark.asyncio
async def test_abrupt_tls_eof_cannot_complete_close_delimited_response(certificates, allow_test_loopback):
    payload = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + TRACE
    async with tls_server(certificates["valid"], payload, abrupt=True) as (port, _, _):
        result = await probes.probe("127.0.0.1", port, TARGET, ssl_context=certificates["client"])
    assert result["ok"] is False
    assert result["stage"] == "http"


@pytest.mark.asyncio
async def test_local_timeout_and_cancellation_release_connection(certificates, allow_test_loopback):
    async with tls_server(certificates["valid"], stall=True) as (port, _, disconnected):
        result = await probes.probe("127.0.0.1", port, TARGET, timeout=0.15, ssl_context=certificates["client"])
        assert result["ok"] is False
        assert result["stage"] == "http"
        assert "deadline" in result["error"]
        await asyncio.wait_for(disconnected.wait(), 1)
    async with tls_server(certificates["valid"], stall=True) as (port, requests, disconnected):
        task = asyncio.create_task(probes.probe("127.0.0.1", port, TARGET, ssl_context=certificates["client"]))
        async with asyncio.timeout(1):
            while not requests:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(disconnected.wait(), 1)


class _LoopbackResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        assert host == "probe.test"
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET, "proto": 0, "flags": socket.AI_NUMERICHOST}]

    async def close(self):
        pass


@asynccontextmanager
async def websocket_relay(context, tcp_port, *, variant=None):
    connections = []
    closed = asyncio.Event()

    async def handler(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.query["ip"] == "8.8.8.8"
        assert request.query["port"] == "443"
        if variant == "redirect":
            return web.Response(status=302, headers={"Location": "https://127.0.0.1/private"})
        websocket = web.WebSocketResponse(max_msg_size=65536)
        await websocket.prepare(request)
        reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
        connections.append(writer)
        ready = {"type": "ready", "protocol": 1, "candidate": "8.8.8.8:443", "colo": "NRT", "connect_ms": 12}
        if variant == "candidate":
            ready["candidate"] = "1.1.1.1:443"
        if variant == "protocol":
            ready["protocol"] = 2
        await websocket.send_json(ready)

        async def pump():
            while data := await reader.read(16384):
                # Arbitrary TLS record fragmentation across real WS messages.
                for offset in range(0, len(data), 31):
                    await websocket.send_bytes(data[offset : offset + 31])
            await websocket.close()

        pump_task = asyncio.create_task(pump())
        try:
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.BINARY:
                    writer.write(message.data)
                    await writer.drain()
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            closed.set()
        return websocket

    app = web.Application()
    app.router.add_get("/probe", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"https://probe.test:{port}/", connections, closed
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_worker_memorybio_through_real_ws_tls_relay(certificates, monkeypatch):
    monkeypatch.setattr(probes, "SafeResolver", _LoopbackResolver)
    async with tls_server(certificates["valid"], fragmented=True) as (port, requests, _):
        async with websocket_relay(certificates["valid"], port) as (url, _, closed):
            result = await probes.probe("8.8.8.8", 443, TARGET, "worker", worker_url=url, worker_token=TOKEN, ssl_context=certificates["client"])
            await asyncio.wait_for(closed.wait(), 1)
    assert result["ok"] is True, result
    assert result["worker_colo"] == "NRT"
    assert result["tcp_ms"] == 12
    assert result["exit_colo"] == "HKG"
    assert result["exit_ip"] == "1.2.3.4"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["candidate", "protocol", "redirect"])
async def test_worker_rejects_unverified_ready_and_redirects(certificates, monkeypatch, variant):
    monkeypatch.setattr(probes, "SafeResolver", _LoopbackResolver)
    async with tls_server(certificates["valid"]) as (port, requests, _):
        async with websocket_relay(certificates["valid"], port, variant=variant) as (url, _, _):
            result = await probes.probe("8.8.8.8", 443, TARGET, "worker", worker_url=url, worker_token=TOKEN, ssl_context=certificates["client"])
    assert result["ok"] is False
    assert result["stage"] == "connect"
    assert len(requests) == 0
    if variant == "redirect":
        assert "redirects are not accepted" in result["error"]
    else:
        assert "metadata mismatch" in result["error"]


@pytest.mark.asyncio
async def test_worker_rejects_target_certificate_and_timeout(certificates, monkeypatch):
    monkeypatch.setattr(probes, "SafeResolver", _LoopbackResolver)
    async with tls_server(certificates["wrong"]) as (port, _, _):
        async with websocket_relay(certificates["valid"], port) as (url, _, closed):
            result = await probes.probe("8.8.8.8", 443, TARGET, "worker", worker_url=url, worker_token=TOKEN, ssl_context=certificates["client"])
            await asyncio.wait_for(closed.wait(), 1)
            assert result["ok"] is False
            assert result["stage"] == "tls"
    async with tls_server(certificates["valid"], stall=True) as (port, _, _):
        async with websocket_relay(certificates["valid"], port) as (url, _, closed):
            result = await probes.probe("8.8.8.8", 443, TARGET, "worker", timeout=0.3, worker_url=url, worker_token=TOKEN, ssl_context=certificates["client"])
            await asyncio.wait_for(closed.wait(), 1)
            assert result["ok"] is False
            assert "deadline" in result["error"]


@pytest.mark.asyncio
async def test_worker_cancellation_closes_websocket_and_tcp(certificates, monkeypatch):
    monkeypatch.setattr(probes, "SafeResolver", _LoopbackResolver)
    async with tls_server(certificates["valid"], stall=True) as (port, requests, _):
        async with websocket_relay(certificates["valid"], port) as (url, _, closed):
            task = asyncio.create_task(probes.probe(
                "8.8.8.8", 443, TARGET, "worker", worker_url=url,
                worker_token=TOKEN, ssl_context=certificates["client"],
            ))
            async with asyncio.timeout(1):
                while not requests:
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(closed.wait(), 1)


@pytest.mark.asyncio
async def test_production_validation_and_no_worker_fallback():
    result = await probes.probe("127.0.0.1", 443, TARGET)
    assert result["stage"] == "validation"
    assert result["ok"] is False
    result = await probes.probe("8.8.8.8", 443, TARGET, "worker")
    assert result["ok"] is False
    assert result["stage"] == "connect"
    assert result["tcp_ms"] is None


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.1", "224.0.0.1", "::1", "ff02::1"])
def test_trace_exit_must_be_public(address):
    with pytest.raises(probes.ProbeError):
        probes._validate_trace(TRACE.replace(b"ip=1.2.3.4", f"ip={address}".encode()), TARGET)

