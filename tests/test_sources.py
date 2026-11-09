import asyncio

import pytest

from cfproxy import network
from cfproxy.config import Settings
from cfproxy.db import Database
from cfproxy.jobs import JobManager, TARGETS
from cfproxy.parsing import Seed, utcnow


@pytest.mark.parametrize("status,length,chunks,message", [
    (302, 0, [], "302"),
    (200, network.MAX_DOWNLOAD + 1, [], "上限"),
    (200, None, [b"x" * (network.MAX_DOWNLOAD // 2 + 1)] * 2, "上限"),
    (200, None, [b"\xff"], "UTF-8"),
])
async def test_source_download_status_size_and_encoding(monkeypatch, status, length, chunks, message):
    class Content:
        async def iter_chunked(self, size):
            for chunk in chunks:
                yield chunk

    class Response:
        content_length = length
        content = Content()
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    Response.status = status

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            return Response()

    monkeypatch.setattr(network.aiohttp, "ClientSession", Session)
    with pytest.raises(ValueError, match=message):
        await network.fetch_text("https://example.com/addresses.txt")


async def test_concurrency_limit_and_persistent_samples(tmp_path):
    db = Database(tmp_path / "concurrency.sqlite3")
    seeds = [Seed(ip, 443, "test", ip, utcnow()) for ip in ("8.8.8.8", "1.1.1.1", "9.9.9.9", "8.8.4.4")]
    db.import_seeds(seeds, {"duplicates": 0, "truncated": False})
    active = peak = 0

    async def measured(host, port, target, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"ok": True, "target": target, "mode": "local", "total_ms": 30, "exit_ip": "1.1.1.1", "worker_colo": None}

    manager = JobManager(db, Settings(db_path=db.path), measured)
    job = manager.start({"mode": "local", "samples": 2, "timeout": 3, "concurrency": 2})
    await manager.task
    assert peak == 2
    assert db.job(job["id"])["completed"] == 4
    for candidate in db.candidates():
        history = db.history(candidate["id"])[0]
        assert len(history["samples"]) == 2 * len(TARGETS)
        assert history["status"] == "passed"


async def test_pool_capacity_is_reported(tmp_path, monkeypatch):
    from cfproxy import db as storage

    monkeypatch.setattr(storage, "MAX_CANDIDATES", 1)
    db = Database(tmp_path / "capacity.sqlite3")
    seeds = [Seed(ip, 443, "test", ip, utcnow()) for ip in ("8.8.8.8", "1.1.1.1")]
    report = db.import_seeds(seeds, {"duplicates": 0, "truncated": False})
    assert report["added"] == 1
    assert report["truncated"] is True
    assert report["total_candidates"] == 1
