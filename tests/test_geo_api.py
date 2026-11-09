"""Country API contracts: geography, latest-result export, and update controls."""

import csv
from datetime import datetime, timedelta, timezone
import io
import re

from fastapi.testclient import TestClient
import pytest

from cfproxy.app import create_app
from cfproxy.config import Settings
from cfproxy.geo import SOURCE
from cfproxy.jobs import TARGETS, summarize
from cfproxy.parsing import Seed, utcnow


ADDRESSES = (
    "8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1",
    "9.9.9.9", "149.112.112.112", "208.67.222.222", "208.67.220.220",
)


def make_app(tmp_path, **settings):
    return create_app(Settings(db_path=str(tmp_path / "geo-api.sqlite3"), **settings))


def seed_candidates(app, addresses=ADDRESSES):
    app.state.db.import_seeds(
        [Seed(address, 443, "测试候选", address, utcnow()) for address in addresses],
        {"duplicates": 0, "truncated": False},
    )
    return {item["host"]: item for item in app.state.db.candidates()}


def sample(country, *, mode="local", ok=True, index=0):
    return {
        "ok": ok, "target": TARGETS[index % len(TARGETS)], "mode": mode,
        "stage": "complete" if ok else "tls", "error": None if ok else "fixture failure",
        "tcp_ms": 5, "tls_ms": 15, "ttfb_ms": 25, "total_ms": 50 + index,
        "exit_ip": "1.2.3.4" if ok else None, "exit_colo": "NRT" if ok else None,
        "worker_colo": "LAX" if mode == "worker" else None, "exit_country": country,
    }


def record(app, candidate, countries=("JP", "JP"), *, mode="local", ok=True,
           checked_at=None, summary_overrides=None, legacy=False):
    db = app.state.db
    config = {"mode": mode, "samples": 1, "timeout": 3, "concurrency": 1, "targets": list(TARGETS)}
    job, result_ids = db.create_job([candidate], config)
    samples = [sample(country, mode=mode, ok=ok, index=i) for i, country in enumerate(countries)]
    summary = summarize(samples, len(TARGETS), mode, job["id"])
    if checked_at:
        summary["checked_at"] = checked_at
    if summary_overrides:
        summary.update(summary_overrides)
    if legacy:
        summary.pop("exit_countries", None)
        summary.pop("country_unknown_samples", None)
        for item in samples:
            item.pop("exit_country", None)
    result_id = result_ids[candidate["id"]]
    db.save_result(result_id, summary, samples)
    db.finish_job(job["id"], "completed")
    return result_id


def install_lookup(app, monkeypatch, codes):
    def lookup(address):
        code = codes.get(address)
        if code is None:
            return None
        return {
            "code": code, "name": {"JP": "日本", "US": "美国"}.get(code, code),
            "source": SOURCE, "version": "2026-11",
        }
    monkeypatch.setattr(app.state.geo, "lookup", lookup)
    app.state.geo.version = "2026-11"
    app.state.geo.build_date = "2026-11-01T00:00:00+00:00"


def hosts(response):
    assert response.status_code == 200, response.text
    return {item["host"] for item in response.json()["items"]}


def test_no_database_means_unknown_without_changing_candidates(tmp_path):
    app = make_app(tmp_path)
    seed_candidates(app, ADDRESSES[:2])
    with TestClient(app, base_url="http://localhost") as client:
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["geo"]["ready"] is False
        assert status.json()["geo"]["updating"] is False
        assert status.json()["geo"]["source"] == SOURCE
        assert str(tmp_path) not in status.text
        items = client.get("/api/candidates").json()["items"]
        assert len(items) == 2
        assert all(item["country"] is None for item in items)
        assert hosts(client.get("/api/candidates", params={"country": "unknown"})) == set(ADDRESSES[:2])
        assert hosts(client.get("/api/candidates", params={"country": "JP"})) == set()
        assert not app.state.geo.path.exists()


def test_candidate_and_observed_country_filters_are_distinct(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    candidates = seed_candidates(app, ADDRESSES[:4])
    install_lookup(app, monkeypatch, {ADDRESSES[0]: "JP", ADDRESSES[1]: "US", ADDRESSES[3]: "JP"})
    record(app, candidates[ADDRESSES[0]], ("US", "US"))
    record(app, candidates[ADDRESSES[1]], ("JP", "US"))
    record(app, candidates[ADDRESSES[2]], ("JP", None))
    record(app, candidates[ADDRESSES[0]], ("JP", "JP"), mode="worker")
    with TestClient(app, base_url="http://localhost") as client:
        expected = {
            ("candidate", "all"): set(ADDRESSES[:4]),
            ("candidate", "unknown"): {ADDRESSES[2]},
            ("candidate", "JP"): {ADDRESSES[0], ADDRESSES[3]},
            ("candidate", "US"): {ADDRESSES[1]},
            ("exit", "all"): set(ADDRESSES[:4]),
            ("exit", "unknown"): {ADDRESSES[3]},
            ("exit", "JP"): {ADDRESSES[1], ADDRESSES[2]},
            ("exit", "US"): {ADDRESSES[0], ADDRESSES[1]},
        }
        for (basis, country), addresses in expected.items():
            assert hosts(client.get("/api/candidates", params={"country_basis": basis, "country": country})) == addresses
        items = {item["host"]: item for item in client.get("/api/candidates").json()["items"]}
        assert items[ADDRESSES[1]]["latest"]["exit_countries"] == ["JP", "US"]
        assert items[ADDRESSES[2]]["latest"]["country_unknown_samples"] == 1
        assert hosts(client.get("/api/candidates", params={"mode": "worker", "country_basis": "exit", "country": "JP"})) == {ADDRESSES[0]}
        assert hosts(client.get("/api/candidates", params={"mode": "worker", "country_basis": "exit", "country": "unknown"})) == set(ADDRESSES[1:4])
        # Candidate geography can change with a new database; historical loc does not.
        install_lookup(app, monkeypatch, {ADDRESSES[0]: "US"})
        refreshed = client.get("/api/candidates").json()["items"]
        changed = next(item for item in refreshed if item["host"] == ADDRESSES[0])
        assert changed["country"]["code"] == "US"
        assert changed["latest"]["exit_countries"] == ["US"]


@pytest.mark.parametrize("path", ["/api/candidates", "/api/export"])
@pytest.mark.parametrize("params", [
    {"country": "Japan"}, {"country": "XX"}, {"country": "T1"},
    {"country_basis": "colo"},
])
def test_invalid_country_filters_are_rejected(tmp_path, path, params):
    with TestClient(make_app(tmp_path), base_url="http://localhost") as client:
        result = client.get(path, params=params)
        assert result.status_code in (400, 422), result.text
        assert "detail" in result.json()


def test_old_history_has_unknown_exit_and_does_not_rewrite_storage(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    candidate = seed_candidates(app, ADDRESSES[:1])[ADDRESSES[0]]
    install_lookup(app, monkeypatch, {ADDRESSES[0]: "JP", "1.2.3.4": "US"})
    result_id = record(app, candidate, legacy=True)
    db = app.state.db
    with db.connect() as connection:
        before = tuple(connection.execute("SELECT summary,samples FROM results WHERE id=?", (result_id,)).fetchone())
    assert "exit_country" not in before[1]
    with TestClient(app, base_url="http://localhost") as client:
        latest = client.get("/api/candidates").json()["items"][0]["latest"]
        assert latest["exit_countries"] == []
        assert latest["country_unknown_samples"] == latest["successes"] == 2
        history = client.get(f"/api/candidates/{candidate['id']}/history").json()["items"][0]
        assert history["exit_countries"] == []
        assert history["country_unknown_samples"] == history["successes"] == 2
        assert all(item["exit_country"] is None for item in history["samples"])
        assert hosts(client.get("/api/candidates", params={"country_basis": "exit", "country": "JP"})) == set()
    with db.connect() as connection:
        after = tuple(connection.execute("SELECT summary,samples FROM results WHERE id=?", (result_id,)).fetchone())
    assert after == before


def test_country_export_respects_latest_completeness_ttl_and_mode(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    candidates = seed_candidates(app)
    install_lookup(app, monkeypatch, {
        address: "US" if index == 1 else "JP"
        for index, address in enumerate(ADDRESSES[:-1])
    })
    record(app, candidates[ADDRESSES[0]], ("US", "US"))
    record(app, candidates[ADDRESSES[1]], ("JP", "US"))
    record(app, candidates[ADDRESSES[2]])
    record(app, candidates[ADDRESSES[2]], ok=False)  # Old JP success must not revive.
    record(app, candidates[ADDRESSES[3]], checked_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    record(app, candidates[ADDRESSES[4]], ("JP",), summary_overrides={"status": "passed"})
    record(app, candidates[ADDRESSES[5]])
    job, _ = app.state.db.create_job([candidates[ADDRESSES[5]]], {"mode": "local", "samples": 1, "targets": list(TARGETS)})
    app.state.db.finish_job(job["id"], "cancelled", "fixture cancellation")
    record(app, candidates[ADDRESSES[6]], mode="worker")
    record(app, candidates[ADDRESSES[7]], (None, None))

    with TestClient(app, base_url="http://localhost") as client:
        def exported(country="all", basis="candidate", mode="local"):
            result = client.get("/api/export", params={"country": country, "country_basis": basis, "mode": mode})
            assert result.status_code == 200, result.text
            return set(result.text.splitlines())

        assert exported() == {f"{ADDRESSES[i]}:443" for i in (0, 1, 7)}
        assert exported("JP") == {f"{ADDRESSES[0]}:443"}
        assert exported("US") == {f"{ADDRESSES[1]}:443"}
        assert exported("JP", "exit") == {f"{ADDRESSES[1]}:443"}
        assert exported("US", "exit") == {f"{ADDRESSES[i]}:443" for i in (0, 1)}
        assert exported("unknown") == exported("unknown", "exit") == {f"{ADDRESSES[7]}:443"}
        assert exported("JP", "exit", "worker") == {f"{ADDRESSES[6]}:443"}

        response = client.get("/api/export", params={"format": "csv", "country": "JP", "country_basis": "exit"})
        assert response.status_code == 200
        reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
        assert {"candidate_country", "exit_countries", "geo_source", "geo_version"}.issubset(reader.fieldnames)
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["ip"] == ADDRESSES[1]
        assert rows[0]["candidate_country"] == "US"
        assert set(re.findall(r"\b[A-Z]{2}\b", rows[0]["exit_countries"])) == {"JP", "US"}
        assert rows[0]["geo_source"] == SOURCE
        assert rows[0]["geo_version"] == "2026-11"


def test_geo_update_proxy_priority_and_validation_redaction(tmp_path, monkeypatch):
    configured = "http://default:private-marker@127.0.0.1:7890"
    supplied = "http://request:other-marker@127.0.0.1:7891"
    app = make_app(tmp_path, fetch_proxy=configured)
    calls = []

    async def update(*, proxy_url=None):
        calls.append(proxy_url)
        return app.state.geo.status

    monkeypatch.setattr(app.state.geo, "update", update)
    with TestClient(app, base_url="http://localhost") as client:
        for payload in (None, {"use_proxy": False, "proxy_url": supplied}, {"use_proxy": True}, {"use_proxy": True, "proxy_url": supplied}):
            result = client.post("/api/geo/update") if payload is None else client.post("/api/geo/update", json=payload)
            assert result.status_code == 200, result.text
            assert "marker" not in result.text
        assert calls == [None, None, configured, supplied]
        for payload in (
            {"proxy_url": {"password": "private-marker"}},
            {"use_proxy": "private-marker"}, {"extra": "private-marker"},
            {"proxy_url": "private-marker" * 500},
        ):
            result = client.post("/api/geo/update", json=payload)
            assert result.status_code == 422
            assert "private-marker" not in result.text
        result = client.post("/api/geo/update", json={"use_proxy": True, "proxy_url": "http://user:private-marker@proxy:0"})
        assert result.status_code == 400
        assert "private-marker" not in result.text
        assert len(calls) == 4
        assert "marker" not in client.get("/api/status").text
        assert "marker" not in client.get("/api/sources").text
    assert b"private-marker" not in (tmp_path / "geo-api.sqlite3").read_bytes()


def test_geo_update_requires_proxy_when_requested(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    called = False

    async def update(*, proxy_url=None):
        nonlocal called
        called = True
        return app.state.geo.status

    monkeypatch.setattr(app.state.geo, "update", update)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/api/geo/update", json={"use_proxy": True})
        assert response.status_code == 400
        assert not called


def test_geo_update_rejects_concurrent_request(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    called = False

    async def update(*, proxy_url=None):
        nonlocal called
        called = True
        return app.state.geo.status

    monkeypatch.setattr(app.state.geo, "update", update)
    with TestClient(app, base_url="http://localhost") as client:
        client.portal.call(app.state.geo.lock.acquire)
        try:
            assert client.get("/api/status").json()["geo"]["updating"] is True
            response = client.post("/api/geo/update")
            assert response.status_code == 409, response.text
            assert not called
        finally:
            client.portal.call(app.state.geo.lock.release)
        assert client.get("/api/status").json()["geo"]["updating"] is False
