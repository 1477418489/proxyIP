import asyncio
from datetime import datetime, timedelta, timezone
import gzip
from pathlib import Path
from types import SimpleNamespace

import maxminddb
import pytest

from cfproxy import geo, network
from cfproxy.geo import GeoDatabase


class Reader:
    def __init__(self, code="JP", **metadata):
        self.code = code
        self.closed = False
        self.info = dict(
            database_type="DBIP-Country-Lite", ip_version=6,
            build_epoch=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
        ) | metadata

    def metadata(self):
        return SimpleNamespace(**self.info)

    def get(self, address):
        if address == "9.9.9.9":
            return None
        return {"country": {"iso_code": self.code, "names": {"zh-CN": "日本" if self.code == "JP" else "美国"}}}

    def close(self):
        self.closed = True


@pytest.fixture
def database(tmp_path, monkeypatch):
    readers = []

    def open_reader(path, *, mode):
        assert mode == maxminddb.MODE_MEMORY
        content = Path(path).read_bytes()
        if content not in (b"old", b"new"):
            raise maxminddb.InvalidDatabaseError("bad test database")
        reader = Reader("JP" if content == b"old" else "US")
        readers.append(reader)
        return reader

    monkeypatch.setattr(geo.maxminddb, "open_database", open_reader)
    path = tmp_path / "dbip-country-lite.mmdb"
    path.write_bytes(b"old")
    instance = GeoDatabase(path)
    yield instance, readers
    instance.close()


def test_local_lookup_ipv4_ipv6_unknown_and_private(database):
    instance, _ = database
    for address in ("8.8.8.8", "2001:4860:4860::8888"):
        assert instance.lookup(address) == {"code": "JP", "name": "日本", "source": geo.SOURCE, "version": "2026-01"}
    for address in ("9.9.9.9", "127.0.0.1", "2001:db8::1", "not-an-ip"):
        assert instance.lookup(address) is None


@pytest.mark.parametrize("payload", [b"garbage", b"\xab\xcd\xefMaxMind.com\xe0", b"\xab\xcd\xefMaxMind.com\xe1", b""])
def test_real_reader_corrupt_database_does_not_break_startup(tmp_path, payload):
    path = tmp_path / "corrupt.mmdb"
    path.write_bytes(payload)
    instance = GeoDatabase(path)
    assert not instance.status["ready"]
    assert "无效" in instance.status["error"]
    assert str(path) not in instance.status["error"]
    assert instance.lookup("8.8.8.8") is None


@pytest.mark.parametrize("metadata", [
    {"database_type": "GeoLite2-Country"}, {"ip_version": 4},
    {"build_epoch": 0}, {"build_epoch": 10**100},
    {"build_epoch": (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()},
])
def test_wrong_database_metadata_is_rejected_and_reader_closed(tmp_path, monkeypatch, metadata):
    path = tmp_path / "bad.mmdb"
    path.write_bytes(b"fixture")
    reader = Reader(**metadata)
    monkeypatch.setattr(geo.maxminddb, "open_database", lambda *args, **kwargs: reader)
    instance = GeoDatabase(path)
    assert not instance.status["ready"] and reader.closed


def test_atomic_install_replaces_data_closes_reader_and_clears_cache(database):
    instance, readers = database
    assert instance.lookup("8.8.8.8")["code"] == "JP"
    instance._install(gzip.compress(b"new"))
    assert instance.path.read_bytes() == b"new"
    assert instance.lookup("8.8.8.8")["code"] == "US"
    assert readers[0].closed and not readers[1].closed
    assert list(instance.path.parent.glob(".dbip-*.tmp")) == []


@pytest.mark.parametrize("failure", ["invalid", "replace", "deadline", "gzip", "oversized-download", "oversized-database"])
def test_failed_install_preserves_file_reader_and_cleans_temp(database, monkeypatch, failure):
    instance, readers = database
    instance.lookup("8.8.8.8")
    payload = gzip.compress(b"new")
    options = {}
    if failure == "invalid":
        payload = gzip.compress(b"invalid mmdb")
    elif failure == "replace":
        def fail_replace(*args):
            raise PermissionError("fixture")
        monkeypatch.setattr(geo.os, "replace", fail_replace)
    elif failure == "deadline":
        options["deadline"] = 0
    elif failure == "gzip":
        payload = payload[:-4]
    elif failure == "oversized-download":
        payload = b"x" * (geo.COMPRESSED_LIMIT + 1)
    else:
        # Exercise the real bounded GzipFile read with highly compressed data.
        payload = gzip.compress(b"x" * (geo.DATABASE_LIMIT + 1))
    with pytest.raises((ValueError, OSError)):
        instance._install(payload, **options)
    assert instance.path.read_bytes() == b"old"
    assert instance.reader is readers[0] and not readers[0].closed
    assert instance.lookup("8.8.8.8")["code"] == "JP"
    assert all(reader.closed for reader in readers[1:])
    assert list(instance.path.parent.glob(".dbip-*.tmp")) == []


async def test_update_falls_back_only_one_month_on_404_and_keeps_proxy(database, monkeypatch):
    instance, _ = database
    calls = []
    proxy = "http://user:secret@127.0.0.1:7890"

    async def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            raise network.SourceFetchError("来源返回 HTTP 404", status_code=404)
        return gzip.compress(b"new")

    monkeypatch.setattr(geo, "fetch_bytes", fetch)
    result = await instance.update(proxy_url=proxy)
    assert result["ready"] and not result["updating"] and result["error"] is None
    month = datetime.now(timezone.utc).replace(day=1)
    assert [call[0].rsplit("-lite-", 1)[1] for call in calls] == [
        month.strftime("%Y-%m.mmdb.gz"), (month - timedelta(days=1)).strftime("%Y-%m.mmdb.gz"),
    ]
    assert all(call[1] == {"proxy_url": proxy, "max_bytes": 8 * 1024 * 1024, "timeout": 60} for call in calls)
    assert "secret" not in str(result)


@pytest.mark.parametrize("status,expected_calls", [(403, 1), (500, 1), (302, 1), (404, 2)])
async def test_update_failure_retains_working_database_and_reports_error(database, monkeypatch, status, expected_calls):
    instance, _ = database
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        raise network.SourceFetchError(f"来源返回 HTTP {status}", status_code=status)

    monkeypatch.setattr(geo, "fetch_bytes", fetch)
    with pytest.raises(ValueError, match="保留"):
        await instance.update()
    assert len(calls) == expected_calls
    assert instance.status["ready"] and not instance.status["updating"]
    assert instance.path.read_bytes() == b"old"
    assert instance.lookup("8.8.8.8")["code"] == "JP"


async def test_update_lock_and_cancellation_preserve_old_data(database, monkeypatch):
    instance, _ = database
    entered = asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(geo, "fetch_bytes", fetch)
    task = asyncio.create_task(instance.update())
    await entered.wait()
    assert instance.status["updating"]
    with pytest.raises(ValueError, match="正在更新"):
        await instance.update()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not instance.status["updating"] and "取消" in instance.status["error"]
    assert instance.path.read_bytes() == b"old"


async def test_binary_download_uses_larger_limit_without_utf8_decoding(monkeypatch):
    payload = b"\xff" * (3 * 1024 * 1024)

    class Content:
        async def iter_chunked(self, size):
            for offset in range(0, len(payload), size):
                yield payload[offset:offset + size]

    class Response:
        status = 200
        headers = {}
        content_length = len(payload)
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            assert not kwargs["auto_decompress"] and not kwargs["trust_env"]
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, url, **kwargs):
            assert not kwargs["allow_redirects"]
            return Response()

    monkeypatch.setattr(network.aiohttp, "ClientSession", Session)
    assert await network.fetch_bytes("https://example.com/file.gz", max_bytes=geo.COMPRESSED_LIMIT, timeout=60) == payload
    with pytest.raises(network.SourceFetchError, match="上限"):
        await network.fetch_text("https://example.com/list.txt")
    Response.status = 404
    with pytest.raises(network.SourceFetchError) as error:
        await network.fetch_bytes("https://example.com/file.gz")
    assert error.value.status_code == 404
