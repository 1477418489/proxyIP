import asyncio
import logging
import statistics

from .config import Settings
from .db import Database
from .parsing import utcnow
from .probes import normalize_country_code, probe

TARGETS = ("www.cloudflare.com", "speed.cloudflare.com")
log = logging.getLogger(__name__)


def summarize(samples, expected, mode, job_id, state=None):
    good = [s for s in samples if s["ok"]]
    countries = [normalize_country_code(sample.get("exit_country")) for sample in good]
    timings = [s["total_ms"] for s in good]
    median = statistics.median(timings) if timings else None
    rate = len(good) / len(samples) if samples else 0
    if state is None:
        state = "passed" if len(good) == expected else "unstable" if good else "failed"
    complete = len(samples) == expected and state in ("passed", "unstable", "failed")
    score = round(80 * rate + 20 * rate * max(0, 1 - median / 3000), 1) if good and complete else (0 if complete else None)
    return {
        "status": state, "mode": mode, "job_id": job_id, "checked_at": utcnow(),
        "attempts": len(samples), "expected_samples": expected, "successes": len(good),
        "success_rate": round(rate, 4), "p50_ms": round(median, 2) if median is not None else None,
        "max_ms": round(max(timings), 2) if timings else None,
        "jitter_ms": round(statistics.pstdev(timings), 2) if timings else None,
        "score": score, "last_error": next((s.get("error") for s in reversed(samples) if not s["ok"]), None),
        "worker_colos": sorted({s["worker_colo"] for s in samples if s.get("worker_colo")}),
        "exit_ips": sorted({s["exit_ip"] for s in good if s.get("exit_ip")}),
        "exit_countries": sorted({country for country in countries if country is not None}),
        "country_unknown_samples": countries.count(None),
        "targets": list(TARGETS),
    }


class JobManager:
    def __init__(self, db: Database, settings: Settings, probe_function=None):
        self.db = db
        self.settings = settings
        self.probe_function = probe_function
        self.task = None
        self.active_id = None

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def start(self, config):
        if self.running:
            raise ValueError("已有检测任务运行，请等待或取消")
        if config["mode"] == "worker" and not self.settings.worker_configured:
            raise ValueError("尚未配置 Worker URL 和独立密钥，参见 README")
        candidates = self.db.candidates(config["mode"])
        ids = set(config.pop("candidate_ids", []))
        if ids:
            candidates = [c for c in candidates if c["id"] in ids]
            if len(candidates) != len(ids):
                raise ValueError("部分候选不存在，请刷新列表")
        if not candidates:
            raise ValueError("请先导入候选地址")
        if len(candidates) > 500:
            raise ValueError("单次任务最多 500 个候选，请勾选需要检测的地址")
        config["targets"] = list(TARGETS)
        job, results = self.db.create_job(candidates, config)
        self.active_id = job["id"]
        self.task = asyncio.create_task(self._run(job, candidates, results), name=f"probe-{job['id']}")
        return job

    async def _run(self, job, candidates, results):
        probe_function = self.probe_function or probe
        config = job["config"]
        expected = config["samples"] * len(TARGETS)
        semaphore = asyncio.Semaphore(config["concurrency"])

        async def check(candidate):
            async with semaphore:
                samples = []
                self.db.save_result(
                    results[candidate["id"]],
                    summarize(samples, expected, config["mode"], job["id"], "running"),
                    samples,
                )
                for _ in range(config["samples"]):
                    for target in TARGETS:
                        sample = await probe_function(
                            candidate["host"], candidate["port"], target,
                            mode=config["mode"], timeout=config["timeout"],
                            worker_url=self.settings.worker_url or None,
                            worker_token=self.settings.worker_token or None,
                        )
                        samples.append(sample)
                        state = "running" if len(samples) < expected else None
                        summary = summarize(samples, expected, config["mode"], job["id"], state)
                        self.db.save_result(results[candidate["id"]], summary, samples)
                self.db.advance_job(job["id"], summary["status"] == "passed")

        children = [asyncio.create_task(check(candidate)) for candidate in candidates]
        try:
            await asyncio.gather(*children)
        except asyncio.CancelledError:
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            self.db.finish_job(job["id"], "cancelled", "任务已取消，未完成结果不参与导出")
        except Exception:
            log.exception("检测任务发生未处理异常")
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            self.db.finish_job(job["id"], "failed", "检测程序异常，请查看服务日志")
        else:
            self.db.finish_job(job["id"], "completed")
        finally:
            if self.active_id == job["id"]:
                self.active_id = None

    async def cancel(self, id):
        if self.running and self.active_id == id:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                # A task cancelled before its coroutine starts cannot run its cleanup.
                self.db.finish_job(id, "cancelled", "任务已取消")
                self.active_id = None
        return self.db.job(id)

    async def shutdown(self):
        if self.running:
            await self.cancel(self.active_id)
