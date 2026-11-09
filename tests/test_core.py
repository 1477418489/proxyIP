import asyncio
from datetime import datetime, timedelta, timezone
import socket

from fastapi.testclient import TestClient
import pytest

from cfproxy.app import create_app
from cfproxy.config import Settings
from cfproxy.db import Database
from cfproxy.jobs import JobManager, TARGETS, summarize
from cfproxy.network import public_ip, resolve_public, validate_https_url
from cfproxy.parsing import Seed, parse_endpoint, parse_import, utcnow


@pytest.mark.parametrize("raw", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "0.0.0.0", "192.0.2.1", "224.0.0.1", "240.1.2.3", "::", "::1", "fc00::1", "ff02::1", "fe80::1", "::ffff:8.8.8.8", "2002:0808:0808::1", "2001:db8::1"])
def test_public_guard(raw):
    with pytest.raises(ValueError):
        public_ip(raw)


@pytest.mark.parametrize("raw,expected", [("8.8.8.8", ("8.8.8.8", 443)), ("[2606:4700:4700::1111]:8443", ("2606:4700:4700::1111", 8443)), ("Example.COM:2096", ("example.com", 2096))])
def test_endpoint(raw, expected):
    assert parse_endpoint(raw) == expected


@pytest.mark.parametrize("raw", ["https://8.8.8.8", "user@a.example", "8.8.8.8:0", "8.8.8.8:65536", "8.8.8.8:25", "8.8.8.8\r\nx", "1.2.3.999", "[8.8.8.8]:443", "8.8.8.8:"])
def test_endpoint_invalid(raw):
    with pytest.raises(ValueError):
        parse_endpoint(raw)


async def test_reject_mixed_dns(monkeypatch):
    async def fake(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ("8.8.8.8", "127.0.0.1")]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake)
    with pytest.raises(ValueError):
        await resolve_public("mixed.example")


async def test_import_csv_uses_candidate_not_exit():
    seeds, report = await parse_import("\ufeffIP,cf-meta-ip,端口\n8.8.8.8,1.1.1.1,8443\n8.8.8.8,9.9.9.9,8443\n127.0.0.1,8.8.4.4,443", "test", "auto")
    assert [(s.host, s.port) for s in seeds] == [("8.8.8.8", 8443)]
    assert report["duplicates"] == 1
    assert report["rejected"] == 1


async def test_csv_auto_without_port_keeps_only_ip_column():
    seeds, report = await parse_import("IP,cf-meta-ip,Colo\n8.8.8.8,1.1.1.1,HKG", format="auto")
    assert [(seed.host, seed.port) for seed in seeds] == [("8.8.8.8", 443)]
    assert report["rejected"] == 0


async def test_csv_leading_blank_and_huge_field():
    seeds, _ = await parse_import("\n\nIP,端口\n8.8.8.8,443")
    assert seeds[0].host == "8.8.8.8"
    with pytest.raises(ValueError, match="CSV"):
        await parse_import("IP,端口\n" + "a" * 140000 + ",443", format="csv")


async def test_import_truncation_and_expansion(monkeypatch):
    async def fake(host, port):
        return ["8.8.8.8", "8.8.4.4"]
    monkeypatch.setattr("cfproxy.parsing.resolve_public", fake)
    seeds, report = await parse_import("\n".join(["some.example:443"] * 501))
    assert len(seeds) == 2
    assert report["truncated"] is True
    assert report["read"] == 500
    assert report["duplicates"] == 998


@pytest.mark.parametrize("url", ["http://example.com/x", "https://user:pass@example.com", "https://127.0.0.1/", "https://127.1/", "https://2130706433/", "https://0177.0.0.1/", "https://[::ffff:8.8.8.8]/", "https://example.com/#x", "https://example.com:0/", "https://localhost/"])
def test_source_url_guard(url):
    with pytest.raises(ValueError):
        validate_https_url(url)


def good_sample(ms=100, **overrides):
    return {"ok": True, "target": TARGETS[0], "mode": "local", "stage": "complete", "error": None, "tcp_ms": 2, "tls_ms": 30, "ttfb_ms": 65, "total_ms": ms, "exit_ip": "1.1.1.1", "exit_colo": "HKG", "worker_colo": None, **overrides}


def seed_db(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.import_seeds([Seed("8.8.8.8", 443, "test", "8.8.8.8", utcnow())], {"duplicates": 0, "truncated": False})
    return db


def test_scoring_counts_failures():
    samples = [good_sample(100), good_sample(300), good_sample(20, ok=False, error="timeout")]
    summary = summarize(samples, 3, "local", "job")
    assert summary["status"] == "unstable"
    assert summary["successes"] == 2
    assert summary["p50_ms"] == 200
    assert summary["jitter_ms"] == 100
    assert summary["score"] < 67
    assert summarize(samples, 6, "local", "job", "running")["score"] is None


def config(mode="local"):
    return {"mode": mode, "samples": 1, "timeout": 3, "concurrency": 1, "targets": list(TARGETS)}


def test_export_latest_attempt_and_mode_and_ttl(tmp_path):
    db = seed_db(tmp_path)
    candidate = db.candidates()[0]
    job, ids = db.create_job([candidate], config())
    samples = [good_sample(), good_sample()]
    summary = summarize(samples, 2, "local", job["id"])
    db.save_result(ids[candidate["id"]], summary, samples)
    db.finish_job(job["id"], "completed")
    assert len(db.export_candidates("local", 24)) == 1
    assert db.export_candidates("worker", 24) == []
    old = dict(summary, checked_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    db.save_result(ids[candidate["id"]], old, samples)
    assert db.export_candidates("local", 24) == []
    db.save_result(ids[candidate["id"]], summary, samples)
    job2, _ = db.create_job([candidate], config())
    assert db.export_candidates("local", 24) == []
    db.finish_job(job2["id"], "cancelled", "cancel")
    assert db.export_candidates("local", 24) == []
    assert db.candidates()[0]["latest"]["status"] == "cancelled"
    assert len(db.history(candidate["id"])) == 2


def test_restart_recovery(tmp_path):
    db = seed_db(tmp_path)
    job, _ = db.create_job(db.candidates(), config())
    db.recover()
    assert db.job(job["id"])["state"] == "interrupted"
    assert db.candidates()[0]["latest"]["status"] == "interrupted"


async def test_cancel_closes_active_samples_and_blocks_second_job(tmp_path):
    db = seed_db(tmp_path)
    started, closed = asyncio.Event(), asyncio.Event()
    async def slow(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    manager = JobManager(db, Settings(db_path=db.path), slow)
    job = manager.start(config())
    await started.wait()
    with pytest.raises(ValueError, match="已有"):
        manager.start(config())
    await manager.cancel(job["id"])
    assert closed.is_set()
    assert db.job(job["id"])["state"] == "cancelled"
    assert db.export_candidates("local", 24) == []


async def test_full_job_and_unexpected_failure(tmp_path):
    db = seed_db(tmp_path)
    async def good(*args, **kwargs):
        return good_sample(target=args[2])
    manager = JobManager(db, Settings(db_path=db.path), good)
    job = manager.start(config())
    await manager.task
    assert db.job(job["id"])["completed"] == 1
    assert db.job(job["id"])["passed"] == 1
    assert len(db.export_candidates("local", 24)) == 1
    async def broken(*args, **kwargs):
        raise RuntimeError("fixture")
    manager.probe_function = broken
    job = manager.start(config())
    await manager.task
    assert db.job(job["id"])["state"] == "failed"
    assert db.export_candidates("local", 24) == []


def test_api_auth_origin_and_flow(tmp_path):
    token = "test-management-token-32-characters"
    app = create_app(Settings(db_path=str(tmp_path / "api.sqlite3"), token=token))
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/status").status_code == 401
        client.headers["Authorization"] = "Bearer " + token
        response = client.post("/api/import", json={"text": "8.8.8.8\n127.0.0.1"})
        assert response.status_code == 200
        assert response.json()["added"] == 1
        assert response.json()["rejected"] == 1
        assert client.post("/api/import", json={"text": "1.1.1.1"}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/import", json={"text": "1.1.1.1"}, headers={"Origin": "http://[bad"}).status_code == 403
        assert client.post("/api/import", json={"text": "IP,端口\n" + "a" * 140000 + ",443", "format": "csv"}).status_code == 400
        assert client.post("/api/jobs", json={"mode": "worker"}).status_code == 400
        assert client.post("/api/jobs", json={"timeout": 1000}).status_code == 422
        assert client.post("/api/sources", json={"name": "private", "url": "https://127.0.0.1/"}).status_code == 400
        assert client.get("/api/export").text == ""
        candidate = client.get("/api/candidates").json()["items"][0]
        assert client.delete(f"/api/candidates/{candidate['id']}").status_code == 200


def test_local_host_guard_and_nonlocal_startup(tmp_path):
    with pytest.raises(ValueError, match="非回环"):
        Settings(host="0.0.0.0")
    app = create_app(Settings(db_path=str(tmp_path / "local.sqlite3")))
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/status", headers={"host": "evil.example"}).status_code == 403
        assert client.post("/api/import", content=b"a" * (2 * 1024 * 1024 + 1)).status_code == 413
