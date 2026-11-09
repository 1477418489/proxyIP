from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from .parsing import MAX_CANDIDATES, endpoint, utcnow

PRESETS = (
    ("xgonce / Cloudflare_IP", "https://raw.githubusercontent.com/xgonce/Cloudflare_IP/main/result.csv", "csv"),
    ("cfnew-ipdb / 香港", "https://raw.githubusercontent.com/byJoey/cfnew-ipdb/main/country/HK.txt", "txt"),
    ("FarelRA / 实测候选", "https://raw.githubusercontent.com/FarelRA/proxyip-tracker/main/result/tested-ips.csv", "csv"),
    ("Xiaobei09 / 新加坡", "https://raw.githubusercontent.com/Xiaobei09/proxyip/main/data/valid/tiers/fast/SG.txt", "txt"),
    ("Wwuyi123 / CF-Proxyip", "https://raw.githubusercontent.com/Wwuyi123/CF-Proxyip/main/proxyip.txt", "txt"),
    ("cfnew-ipdb / 日本", "https://raw.githubusercontent.com/byJoey/cfnew-ipdb/main/country/JP.txt", "txt"),
    ("cfnew-ipdb / 新加坡", "https://raw.githubusercontent.com/byJoey/cfnew-ipdb/main/country/SG.txt", "txt"),
    ("cfnew-ipdb / 美国", "https://raw.githubusercontent.com/byJoey/cfnew-ipdb/main/country/US.txt", "txt"),
)


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def load_summary(value):
    summary = json.loads(value)
    # Legacy results did not record trace.loc. Current GeoIP must never be
    # used to invent a country for a past observation.
    summary.setdefault("exit_countries", [])
    summary.setdefault("country_unknown_samples", summary.get("successes", 0))
    return summary


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS candidates (
                    id INTEGER PRIMARY KEY,
                    host TEXT NOT NULL, port INTEGER NOT NULL,
                    endpoint TEXT NOT NULL UNIQUE, sources TEXT NOT NULL,
                    origins TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE, format TEXT NOT NULL,
                    last_fetch TEXT, last_count INTEGER, last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, mode TEXT NOT NULL,
                    total INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                    passed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, finished_at TEXT,
                    config TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL, summary TEXT NOT NULL, samples TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS result_candidate_mode ON results(candidate_id, mode, id DESC);
                CREATE INDEX IF NOT EXISTS result_job ON results(job_id);
            """)
            for name, url, kind in PRESETS:
                db.execute("INSERT OR IGNORE INTO sources(name,url,format) VALUES (?,?,?)", (name, url, kind))

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db

    def recover(self):
        with self.connect() as db:
            for row in db.execute("SELECT id FROM jobs WHERE state='running'").fetchall():
                self._finish_open_results(db, row["id"], "interrupted", "服务重启，检测未完成")
            db.execute(
                "UPDATE jobs SET state='interrupted',finished_at=?,error=? WHERE state='running'",
                (utcnow(), "服务重启，检测未完成"),
            )

    def import_seeds(self, seeds, report):
        report = dict(report)
        report["added"] = 0
        with self.connect() as db:
            count = db.execute("SELECT count(*) FROM candidates").fetchone()[0]
            for seed in seeds:
                ep = endpoint(seed.host, seed.port)
                existing = db.execute("SELECT * FROM candidates WHERE endpoint=?", (ep,)).fetchone()
                origin = {"input": seed.origin, "source": seed.source, "resolved_at": seed.resolved_at}
                if existing:
                    sources = json.loads(existing["sources"])
                    if seed.source not in sources:
                        sources = (sources + [seed.source])[-20:]
                    origins = (json.loads(existing["origins"]) + [origin])[-20:]
                    db.execute("UPDATE candidates SET sources=?,origins=? WHERE id=?", (dump(sources), dump(origins), existing["id"]))
                    report["duplicates"] += 1
                    continue
                if count >= MAX_CANDIDATES:
                    report["truncated"] = True
                    continue
                db.execute(
                    "INSERT INTO candidates(host,port,endpoint,sources,origins,created_at) VALUES (?,?,?,?,?,?)",
                    (seed.host, seed.port, ep, dump([seed.source]), dump([origin]), utcnow()),
                )
                count += 1
                report["added"] += 1
            report["total_candidates"] = count
        return report

    def counts(self):
        with self.connect() as db:
            return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("candidates", "sources")}

    def candidates(self, mode="local", search="", status="all"):
        with self.connect() as db:
            rows = db.execute("""
                SELECT c.*, r.summary FROM candidates c LEFT JOIN results r
                ON r.id=(SELECT max(r2.id) FROM results r2 WHERE r2.candidate_id=c.id AND r2.mode=?)
                ORDER BY c.id DESC
            """, (mode,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["sources"] = json.loads(item["sources"])
            item["origins"] = json.loads(item["origins"])
            item["latest"] = load_summary(item.pop("summary")) if row["summary"] else None
            state = item["latest"]["status"] if item["latest"] else "untested"
            if status != "all" and status != state:
                continue
            if search and search.casefold() not in (item["endpoint"] + " " + " ".join(item["sources"])).casefold():
                continue
            items.append(item)
        items.sort(key=lambda c: (c["latest"].get("score") if c["latest"] and c["latest"].get("score") is not None else -1), reverse=True)
        return items

    def candidate(self, id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM candidates WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None

    def delete_candidate(self, id):
        with self.connect() as db:
            return bool(db.execute("DELETE FROM candidates WHERE id=?", (id,)).rowcount)

    def history(self, candidate_id):
        with self.connect() as db:
            rows = db.execute("SELECT summary,samples FROM results WHERE candidate_id=? ORDER BY id DESC LIMIT 50", (candidate_id,)).fetchall()
        return [dict(load_summary(row["summary"]), samples=[
            dict({"exit_country": None}, **sample) for sample in json.loads(row["samples"])
        ]) for row in rows]

    def sources(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY id")]

    def source(self, id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None

    def add_source(self, name, url, format):
        with self.connect() as db:
            preset_urls = {item[1] for item in PRESETS}
            custom_count = sum(row[0] not in preset_urls for row in db.execute("SELECT url FROM sources"))
            if custom_count >= 50:
                raise ValueError("自定义来源数量已达到 50 条上限（预设来源不计入）")
            try:
                cursor = db.execute("INSERT INTO sources(name,url,format) VALUES(?,?,?)", (name, url, format))
            except sqlite3.IntegrityError as exc:
                raise ValueError("此来源 URL 已存在") from exc
            return dict(db.execute("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,)).fetchone())

    def record_fetch(self, id, count=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE sources SET last_fetch=?,last_count=?,last_error=? WHERE id=?", (utcnow(), count, error, id))

    def create_job(self, candidates, config):
        id = uuid.uuid4().hex
        now = utcnow()
        results = {}
        config = dict(config, candidates=[{"id": c["id"], "endpoint": c["endpoint"]} for c in candidates])
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,state,mode,total,created_at,config) VALUES(?,'running',?,?,?,?)", (id, config["mode"], len(candidates), now, dump(config)))
            for candidate in candidates:
                summary = {
                    "status": "queued", "mode": config["mode"], "job_id": id,
                    "checked_at": now, "attempts": 0, "expected_samples": config["samples"] * len(config["targets"]),
                    "successes": 0, "success_rate": 0, "p50_ms": None, "max_ms": None,
                    "jitter_ms": None, "score": None, "last_error": None,
                    "worker_colos": [], "exit_ips": [], "targets": config["targets"],
                    "exit_countries": [], "country_unknown_samples": 0,
                }
                cursor = db.execute("INSERT INTO results(job_id,candidate_id,mode,summary,samples) VALUES(?,?,?,?,?)", (id, candidate["id"], config["mode"], dump(summary), "[]"))
                results[candidate["id"]] = cursor.lastrowid
        return self.job(id), results

    def save_result(self, id, summary, samples):
        with self.connect() as db:
            db.execute("UPDATE results SET summary=?,samples=? WHERE id=?", (dump(summary), dump(samples), id))

    def advance_job(self, id, passed):
        with self.connect() as db:
            db.execute("UPDATE jobs SET completed=completed+1,passed=passed+? WHERE id=?", (int(passed), id))

    @staticmethod
    def _finish_open_results(db, id, state, error):
        rows = db.execute("SELECT id,summary FROM results WHERE job_id=?", (id,)).fetchall()
        for row in rows:
            summary = json.loads(row["summary"])
            if summary["status"] in ("running", "queued"):
                summary.update(status=state, score=None, checked_at=utcnow(), last_error=error)
                db.execute("UPDATE results SET summary=? WHERE id=?", (dump(summary), row["id"]))

    def finish_job(self, id, state, error=None):
        with self.connect() as db:
            self._finish_open_results(db, id, state if state != "failed" else "interrupted", error)
            db.execute("UPDATE jobs SET state=?,finished_at=?,error=? WHERE id=?", (state, utcnow(), error, id))

    @staticmethod
    def _job(row):
        if not row:
            return None
        result = dict(row)
        result["config"] = json.loads(result["config"])
        return result

    def job(self, id):
        with self.connect() as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone())

    def jobs(self):
        with self.connect() as db:
            return [self._job(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30")]

    def export_candidates(self, mode, max_age_hours):
        threshold = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        exported = []
        for candidate in self.candidates(mode):
            latest = candidate["latest"]
            if not latest or latest["status"] != "passed":
                continue
            if not latest["expected_samples"] or latest["attempts"] != latest["expected_samples"] or latest["successes"] != latest["attempts"]:
                continue
            if datetime.fromisoformat(latest["checked_at"]) < threshold:
                continue
            exported.append(candidate)
        return exported
