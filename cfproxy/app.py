import asyncio
from contextlib import asynccontextmanager
import csv
import hmac
import io
import ipaddress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .config import Settings
from .db import Database
from .geo import GeoDatabase, country_filter, matches_country
from .jobs import JobManager, TARGETS
from .network import SourceFetchError, fetch_text, validate_https_url, validate_proxy_url
from .parsing import MAX_CANDIDATES, MAX_IMPORT, parse_import

Mode = Literal["local", "worker"]
Format = Literal["auto", "txt", "csv"]
CountryBasis = Literal["candidate", "exit"]
STATIC = Path(__file__).parent / "static"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportRequest(Model):
    text: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    source: str = Field(default="手动导入", min_length=1, max_length=80)
    format: Format = "auto"


class SourceRequest(Model):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=2048)
    format: Format = "auto"


class JobRequest(Model):
    candidate_ids: list[int] = Field(default_factory=list, max_length=500)
    mode: Mode = "local"
    concurrency: int = Field(default=8, ge=1, le=16)
    samples: int = Field(default=3, ge=1, le=5)
    timeout: float = Field(default=10, ge=3, le=20)


class FetchRequest(Model):
    use_proxy: bool = False
    proxy_url: SecretStr = Field(default=SecretStr(""), max_length=2048)


def _loopback_host(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def create_app(settings: Settings | None = None, *, probe_function=None):
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    geo = GeoDatabase(settings.geo_db or Path(settings.db_path).parent / "dbip-country-lite.mmdb")
    manager = JobManager(db, settings, probe_function)
    imports = asyncio.Lock()
    source_fetches = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        db.recover()
        try:
            yield
        finally:
            await manager.shutdown()
            geo.close()

    app = FastAPI(title="cfPROXYIP", version="0.3.0", lifespan=lifespan)
    app.state.db = db
    app.state.manager = manager
    app.state.settings = settings
    app.state.geo = geo

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host_header = request.headers.get("host", "")
        try:
            host_url = urlsplit("http://" + host_header)
            host = host_url.hostname or ""
            if host_url.username or host_url.password or host_url.path or host_url.query or host_url.fragment:
                raise ValueError("invalid Host")
            _ = host_url.port
        except ValueError:
            return JSONResponse({"detail": "Host 无效"}, status_code=400)
        if not settings.token and not _loopback_host(host):
            return JSONResponse({"detail": "本地模式仅接受 localhost/回环 Host"}, status_code=403)
        protected = request.url.path.startswith("/api/") or request.url.path in ("/docs", "/openapi.json", "/redoc")
        if protected and settings.token:
            expected = f"Bearer {settings.token}".encode()
            supplied = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "请输入管理 Token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                try:
                    parsed_origin = urlsplit(origin)
                    _ = parsed_origin.port
                except ValueError:
                    return JSONResponse({"detail": "Origin 无效"}, status_code=403)
                if parsed_origin.scheme not in ("http", "https") or parsed_origin.netloc.lower() != host_header.lower() or parsed_origin.path or parsed_origin.query or parsed_origin.fragment:
                    return JSONResponse({"detail": "拒绝跨域写操作"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
            # Bound even chunked bodies before Pydantic/JSON parsing allocates memory.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    return JSONResponse({"detail": "请求体超过 2 MiB"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid_input(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # SecretStr masks its repr, but Pydantic's error.input can still hold
        # the complete original object, including credentials or extra fields.
        if request.url.path == "/api/geo/update" or (request.url.path.startswith("/api/sources/") and request.url.path.endswith("/fetch")):
            return JSONResponse({"detail": "拉取参数格式错误，请检查 use_proxy 和代理地址"}, status_code=422)
        return await request_validation_exception_handler(request, error)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status():
        return {
            "worker_configured": settings.worker_configured,
            "fetch_proxy_configured": bool(settings.fetch_proxy),
            "geo": geo.status,
            "active_job": db.job(manager.active_id) if manager.running else None,
            "counts": db.counts(), "targets": list(TARGETS),
            "limits": {"max_candidates": MAX_CANDIDATES, "max_import": MAX_IMPORT},
        }

    @app.get("/api/candidates")
    async def candidates(
        mode: Mode = "local", search: str = Query(default="", max_length=100), status: str = "all",
        country: str = Query(default="all", max_length=10), country_basis: CountryBasis = "candidate",
    ):
        selected_country = country_filter(country)
        items = [geo.decorate(item) for item in db.candidates(mode, status=status)]
        items = [item for item in items if matches_country(item, selected_country, country_basis)]
        if search:
            query = search.casefold()
            items = [item for item in items if query in " ".join([
                item["endpoint"], *item["sources"],
                (item["country"] or {}).get("code", ""), (item["country"] or {}).get("name", ""),
                *(item["latest"] or {}).get("exit_countries", []),
            ]).casefold()]
        return {"items": items}

    def fetch_proxy(body):
        if body is None or not body.use_proxy:
            return None
        proxy_url = body.proxy_url.get_secret_value().strip() or settings.fetch_proxy
        if not proxy_url:
            raise HTTPException(400, "请填写 HTTP 代理地址，或配置 CFPROXY_FETCH_PROXY")
        return validate_proxy_url(proxy_url)

    @app.post("/api/geo/update")
    async def update_geo(body: FetchRequest | None = None):
        if geo.lock.locked():
            raise HTTPException(409, "地区数据库正在更新，请稍后再试")
        return await geo.update(proxy_url=fetch_proxy(body))

    async def import_text(text, source, format):
        if imports.locked():
            raise HTTPException(409, "已有导入正在进行")
        async with imports:
            seeds, report = await parse_import(text, source, format)
            return db.import_seeds(seeds, report)

    @app.post("/api/import")
    async def import_candidates(body: ImportRequest):
        return await import_text(body.text, body.source, body.format)

    @app.delete("/api/candidates/{id}")
    async def delete_candidate(id: int):
        if manager.running:
            raise HTTPException(409, "运行期间不能删除候选，请先取消任务")
        if not db.delete_candidate(id):
            raise HTTPException(404, "候选不存在")
        return {"deleted": id}

    @app.get("/api/candidates/{id}/history")
    async def history(id: int):
        if not db.candidate(id):
            raise HTTPException(404, "候选不存在")
        return {"items": db.history(id)}

    @app.get("/api/sources")
    async def sources():
        return {"items": db.sources()}

    @app.post("/api/sources", status_code=201)
    async def add_source(body: SourceRequest):
        validate_https_url(body.url)
        return db.add_source(body.name, body.url, body.format)

    @app.post("/api/sources/{id}/fetch")
    async def collect(id: int, body: FetchRequest | None = None):
        source = db.source(id)
        if not source:
            raise HTTPException(404, "来源不存在")
        if source_fetches.locked():
            raise HTTPException(409, "已有来源正在拉取")
        proxy_url = fetch_proxy(body)
        async with source_fetches:
            try:
                content = await fetch_text(source["url"], proxy_url=proxy_url)
                report = await import_text(content, source["name"], source["format"])
            except (SourceFetchError, ValueError) as exc:
                message = str(exc) or "来源请求超时"
                # Never store URLs with query strings in errors from upstream libraries.
                if "?" in message:
                    message = "来源网络请求失败，请检查 URL 和网络"
                db.record_fetch(id, error=message[:300])
                raise HTTPException(400, message[:300]) from exc
            except HTTPException:
                raise
            except Exception:
                db.record_fetch(id, error="来源请求失败，请检查 HTTPS、DNS 和网络")
                raise HTTPException(400, "来源请求失败，请检查 HTTPS、DNS 和网络") from None
            db.record_fetch(id, count=report["added"])
            report["fetch_via"] = "proxy" if proxy_url else "direct"
            return report

    @app.post("/api/jobs", status_code=201)
    async def start_job(body: JobRequest):
        if manager.running:
            raise HTTPException(409, "已有检测任务正在运行")
        return manager.start(body.model_dump())

    @app.get("/api/jobs")
    async def jobs():
        return {"items": db.jobs()}

    @app.get("/api/jobs/{id}")
    async def job(id: str):
        result = db.job(id)
        if not result:
            raise HTTPException(404, "任务不存在")
        return result

    @app.post("/api/jobs/{id}/cancel")
    async def cancel(id: str):
        result = await manager.cancel(id)
        if not result:
            raise HTTPException(404, "任务不存在")
        return result

    @app.get("/api/export")
    async def export(
        mode: Mode = "local", format: Literal["txt", "csv"] = "txt",
        max_age_hours: int = Query(default=24, ge=1, le=168),
        country: str = Query(default="all", max_length=10), country_basis: CountryBasis = "candidate",
    ):
        selected_country = country_filter(country)
        items = [geo.decorate(item) for item in db.export_candidates(mode, max_age_hours)]
        items = [item for item in items if matches_country(item, selected_country, country_basis)]
        if format == "txt":
            data = "".join(c["endpoint"] + "\n" for c in items)
            media = "text/plain"
        else:
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["ip", "port", "mode", "score", "success_rate", "samples", "p50_ms", "checked_at", "candidate_country", "exit_countries", "geo_source", "geo_version"])
            for item in items:
                latest = item["latest"]
                region = item["country"] or {}
                writer.writerow([
                    item["host"], item["port"], mode, latest["score"], latest["success_rate"], latest["attempts"],
                    latest["p50_ms"], latest["checked_at"], region.get("code", ""),
                    ";".join(latest["exit_countries"]), region.get("source", ""), region.get("version", ""),
                ])
            data, media = "\ufeff" + output.getvalue(), "text/csv"
        return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="proxyip-{mode}.{format}"'})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
