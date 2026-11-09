"""Strict TLS/HTTP samples over a fixed IP or an authenticated Worker tunnel."""

from __future__ import annotations

import asyncio
import math
import re
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import h11

from .network import SafeResolver, public_ip, validate_https_url

TARGETS = ("www.cloudflare.com", "speed.cloudflare.com")
HEADER_LIMIT = 16 * 1024
BODY_LIMIT = 64 * 1024
FRAME_LIMIT = 64 * 1024
COLO_RE = re.compile(r"[A-Z]{3}\Z")
# ISO 3166-1 alpha-2 assignments. Special values such as Cloudflare's XX/T1
# describe missing metadata or Tor, and are not countries. Keep this local so
# classifying a trace never needs another network request or package.
COUNTRY_CODES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP
KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY
MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY
QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ
VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())


def normalize_country_code(value) -> str | None:
    """Return a known country code; geographic metadata never proves routing."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if code in COUNTRY_CODES else None


class ProbeError(Exception):
    """An invalid or incomplete probe, never a positive availability result."""


class _LocalTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    async def read(self) -> bytes:
        return await self.reader.read(16 * 1024)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        try:
            async with asyncio.timeout(0.5):
                await self.writer.wait_closed()
        except (Exception, asyncio.CancelledError):
            # Closing the transport has already been initiated. Cleanup must
            # not replace the original timeout/cancellation exception.
            pass


class _WorkerTransport:
    def __init__(self, session: aiohttp.ClientSession, websocket):
        self.session = session
        self.websocket = websocket

    async def read(self) -> bytes:
        message = await self.websocket.receive()
        if message.type == aiohttp.WSMsgType.BINARY:
            return message.data
        if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
            return b""
        if message.type == aiohttp.WSMsgType.ERROR:
            raise ProbeError("Worker WebSocket error")
        raise ProbeError("Worker sent a non-binary frame after ready")

    async def write(self, data: bytes) -> None:
        for offset in range(0, len(data), FRAME_LIMIT):
            await self.websocket.send_bytes(data[offset : offset + FRAME_LIMIT])

    async def close(self) -> None:
        try:
            async with asyncio.timeout(0.5):
                await self.websocket.close()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            await self.session.close()


def _worker_endpoint(worker_url: str) -> str:
    validate_https_url(worker_url, root_only=True)
    url = urlsplit(worker_url)
    return urlunsplit(("wss", url.netloc, "/probe", "", ""))


async def _open_worker(
    host: str,
    port: int,
    worker_url: str,
    worker_token: str,
    ssl_context: ssl.SSLContext | None,
):
    endpoint = _worker_endpoint(worker_url)
    if not worker_token or "\r" in worker_token or "\n" in worker_token:
        raise ProbeError("Worker token is missing or invalid")
    connector = aiohttp.TCPConnector(
        resolver=SafeResolver(), ssl=ssl_context or True, use_dns_cache=False
    )

    async def reject_redirect(session, context, params):
        raise ProbeError("Worker redirects are not accepted")

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    session = aiohttp.ClientSession(
        connector=connector,
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=None),
        trace_configs=[trace],
    )
    transport = None
    try:
        websocket = await session.ws_connect(
            endpoint,
            params={"ip": host, "port": str(port)},
            headers={"Authorization": f"Bearer {worker_token}"},
            max_msg_size=FRAME_LIMIT,
            autoclose=True,
            autoping=True,
        )
        transport = _WorkerTransport(session, websocket)
        message = await websocket.receive()
        if message.type != aiohttp.WSMsgType.TEXT:
            raise ProbeError("Worker did not send a ready message")
        try:
            ready = message.json()
        except (TypeError, ValueError):
            raise ProbeError("Worker ready message is not JSON") from None
        candidate = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        connect_ms = ready.get("connect_ms") if isinstance(ready, dict) else None
        if (
            not isinstance(ready, dict)
            or ready.get("type") != "ready"
            or type(ready.get("protocol")) is not int
            or ready["protocol"] != 1
            or ready.get("candidate") != candidate
            or not isinstance(ready.get("colo"), str)
            or not COLO_RE.fullmatch(ready["colo"])
            or type(connect_ms) not in (int, float)
            or not math.isfinite(connect_ms)
            or not 0 <= connect_ms <= 25_000
        ):
            raise ProbeError("Worker ready protocol/candidate/metadata mismatch")
        return transport, ready
    except BaseException:
        if transport is not None:
            await transport.close()
        else:
            await session.close()
        raise


class _TLSStream:
    """OpenSSL does TLS; transports only move opaque TLS records."""

    def __init__(self, transport, target: str, context: ssl.SSLContext):
        self.transport = transport
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self.ssl_object = context.wrap_bio(
            self.incoming, self.outgoing, server_side=False, server_hostname=target
        )
        self.eof = False

    async def _flush(self):
        while self.outgoing.pending:
            await self.transport.write(self.outgoing.read(FRAME_LIMIT))

    async def _call(self, operation):
        while True:
            try:
                result = operation()
            except ssl.SSLWantReadError:
                await self._flush()
                if self.eof:
                    raise ProbeError("TLS stream ended before the response completed")
                data = await self.transport.read()
                if data:
                    self.incoming.write(data)
                else:
                    self.eof = True
                    self.incoming.write_eof()
            except ssl.SSLWantWriteError:
                await self._flush()
            else:
                await self._flush()
                return result

    async def handshake(self):
        await self._call(self.ssl_object.do_handshake)

    async def write(self, data: bytes):
        offset = 0
        while offset < len(data):
            offset += await self._call(lambda: self.ssl_object.write(data[offset:]))

    async def read(self) -> bytes:
        try:
            return await self._call(lambda: self.ssl_object.read(16 * 1024))
        except ssl.SSLZeroReturnError:
            # TLS close_notify is a clean EOF for an HTTP close-delimited
            # response. Abrupt TCP EOF (SSLEOFError) must still fail.
            return b""


def _validate_trace(body: bytes, target: str) -> dict:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ProbeError("Trace body is not UTF-8") from None
    fields = {}
    for line in text.splitlines():
        if not line:
            continue
        if "=" not in line:
            raise ProbeError("Response is not a Cloudflare trace")
        name, value = line.split("=", 1)
        if not name or name in fields:
            raise ProbeError("Trace has duplicate or invalid fields")
        fields[name] = value
    if fields.get("h") != target:
        raise ProbeError("Trace target hostname mismatch")
    try:
        exit_ip = public_ip(fields.get("ip", ""))
    except ValueError:
        raise ProbeError("Trace has no valid exit IP") from None
    colo = fields.get("colo", "")
    if not COLO_RE.fullmatch(colo):
        raise ProbeError("Trace has no valid colo")
    return {
        "exit_ip": exit_ip,
        "exit_colo": colo,
        "exit_country": normalize_country_code(fields.get("loc")),
    }


async def _read_trace(stream: _TLSStream, target: str, result: dict, started: float):
    connection = h11.Connection(h11.CLIENT, max_incomplete_event_size=HEADER_LIMIT)
    request = connection.send(
        h11.Request(
            method="GET",
            target="/cdn-cgi/trace",
            headers=[
                ("Host", target),
                ("User-Agent", "cfPROXYIP/1"),
                ("Accept", "text/plain"),
                ("Accept-Encoding", "identity"),
                ("Connection", "close"),
            ],
        )
    )
    await stream.write(request + connection.send(h11.EndOfMessage()))
    body = bytearray()
    header_prefix = bytearray()
    headers_complete = False
    got_response = False
    eof = False
    while True:
        event = connection.next_event()
        if event is h11.NEED_DATA:
            if eof:
                raise ProbeError("Incomplete HTTP response")
            chunk = await stream.read()
            if chunk and result["ttfb_ms"] is None:
                result["ttfb_ms"] = round((time.perf_counter() - started) * 1000, 3)
            if not headers_complete:
                header_prefix.extend(chunk)
                end = header_prefix.find(b"\r\n\r\n")
                header_size = end + 4 if end >= 0 else len(header_prefix)
                if header_size > HEADER_LIMIT:
                    raise ProbeError("HTTP headers exceed 16 KiB")
                if end >= 0:
                    headers_complete = True
                    header_prefix.clear()
            if not chunk:
                eof = True
            connection.receive_data(chunk)
        elif isinstance(event, h11.Response):
            if event.status_code != 200:
                raise ProbeError(f"Unexpected HTTP status {event.status_code}")
            header_names = {name for name, _ in event.headers}
            if b"transfer-encoding" in header_names and b"content-length" in header_names:
                raise ProbeError("Conflicting HTTP response framing")
            for name, value in event.headers:
                if name == b"content-encoding" and value.lower() != b"identity":
                    raise ProbeError("Compressed trace responses are not accepted")
                if name == b"content-length" and int(value) > BODY_LIMIT:
                    raise ProbeError("HTTP body exceeds 64 KiB")
            got_response = True
        elif isinstance(event, h11.Data):
            body.extend(event.data)
            if len(body) > BODY_LIMIT:
                raise ProbeError("HTTP body exceeds 64 KiB")
        elif isinstance(event, h11.EndOfMessage):
            if not got_response:
                raise ProbeError("Missing HTTP response")
            return _validate_trace(bytes(body), target)
        elif isinstance(event, (h11.InformationalResponse, h11.ConnectionClosed)):
            raise ProbeError("Unexpected or incomplete HTTP response")
        elif event is h11.PAUSED:
            raise ProbeError("Unexpected HTTP protocol state")


async def probe(
    host: str,
    port: int,
    target: str,
    mode: str = "local",
    timeout: float = 10,
    worker_url: str | None = None,
    worker_token: str | None = None,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> dict:
    """One complete sample. Cancellation propagates; failures are structured.

    tcp_ms is measured at the connecting machine (Worker in worker mode).
    tls_ms is observed at this Python process. ttfb_ms and total_ms include
    connection establishment, including the outer WebSocket in worker mode.
    ssl_context is an internal test seam; production uses the system CA store.
    """
    started = time.perf_counter()
    result = {
        "ok": False,
        "target": target,
        "mode": mode,
        "stage": "validation",
        "error": None,
        "tcp_ms": None,
        "tls_ms": None,
        "ttfb_ms": None,
        "total_ms": None,
        "exit_ip": None,
        "exit_colo": None,
        "exit_country": None,
        "worker_colo": None,
    }
    transport = None
    try:
        if target not in TARGETS:
            raise ProbeError("Unsupported probe target")
        if mode not in ("local", "worker"):
            raise ProbeError("Unsupported probe mode")
        if type(port) is not int or not 1 <= port <= 65535 or port == 25:
            raise ProbeError("Invalid candidate port")
        if not math.isfinite(timeout) or not 0 < timeout <= 20:
            raise ProbeError("Timeout must be greater than zero and at most 20 seconds")
        host = public_ip(host)
        async with asyncio.timeout(timeout):
            result["stage"] = "connect"
            connect_started = time.perf_counter()
            if mode == "worker":
                if not worker_url or not worker_token:
                    raise ProbeError("Worker URL and token are required")
                transport, ready = await _open_worker(
                    host, port, worker_url, worker_token, ssl_context
                )
                result["worker_colo"] = ready["colo"]
                result["tcp_ms"] = round(float(ready["connect_ms"]), 3)
            else:
                reader, writer = await asyncio.open_connection(
                    host,
                    port,
                    family=socket.AF_INET6 if ":" in host else socket.AF_INET,
                    flags=socket.AI_NUMERICHOST,
                )
                transport = _LocalTransport(reader, writer)
                result["tcp_ms"] = round((time.perf_counter() - connect_started) * 1000, 3)
            result["stage"] = "tls"
            stream = _TLSStream(transport, target, ssl_context or ssl.create_default_context())
            tls_started = time.perf_counter()
            await stream.handshake()
            result["tls_ms"] = round((time.perf_counter() - tls_started) * 1000, 3)
            result["stage"] = "http"
            result.update(await _read_trace(stream, target, result, started))
            result["ok"] = True
            result["stage"] = "complete"
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        result["error"] = "Overall probe deadline exceeded"
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {str(error)[:300]}"
    finally:
        result["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if transport is not None:
            await transport.close()
    return result
