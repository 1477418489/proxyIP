"""Monthly DB-IP country data, queried locally and installed atomically."""

import asyncio
from datetime import datetime, timedelta, timezone
import gzip
import io
import os
from pathlib import Path
import tempfile
import time
import zlib

import maxminddb

from .network import SourceFetchError, fetch_bytes, public_ip
from .probes import normalize_country_code

SOURCE = "DB-IP Country Lite"
COMPRESSED_LIMIT = 8 * 1024 * 1024
DATABASE_LIMIT = 32 * 1024 * 1024


def country_filter(value: str) -> str:
    if value in ("all", "unknown"):
        return value
    code = normalize_country_code(value)
    if code is None:
        raise ValueError("国家/地区筛选须为 ISO 两位码、all 或 unknown")
    return code


def matches_country(candidate: dict, country="all", basis="candidate") -> bool:
    if country == "all":
        return True
    if basis == "candidate":
        codes = [candidate["country"]["code"]] if candidate.get("country") else []
    else:
        codes = (candidate.get("latest") or {}).get("exit_countries", [])
    return not codes if country == "unknown" else country in codes


class GeoDatabase:
    def __init__(self, path):
        self.path = Path(path)
        self.reader = None
        self.cache = {}
        self.build_date = None
        self.version = None
        self.error = None
        self.lock = asyncio.Lock()
        if self.path.exists():
            try:
                self.reader, self.build_date, self.version = self._read(self.path)
            except (OSError, ValueError, maxminddb.InvalidDatabaseError):
                self.error = "地区库无效，请重新下载；检测功能仍可使用"

    @staticmethod
    def _read(path):
        reader = None
        try:
            if not 0 < path.stat().st_size <= DATABASE_LIMIT:
                raise ValueError("地区数据库为空或超过 32 MiB 上限")
            reader = maxminddb.open_database(str(path), mode=maxminddb.MODE_MEMORY)
            metadata = reader.metadata()
            if metadata.database_type != "DBIP-Country-Lite" or metadata.ip_version != 6:
                raise ValueError("需要支持 IPv4/IPv6 的 DB-IP Country Lite 数据库")
            built = datetime.fromtimestamp(metadata.build_epoch, timezone.utc)
            if not 2000 <= built.year or built > datetime.now(timezone.utc) + timedelta(days=7):
                raise ValueError("地区数据库版本无效")
            for address in ("8.8.8.8", "2001:4860:4860::8888"):
                record = reader.get(address)
                if not isinstance(record, dict) or not isinstance(record.get("country"), dict):
                    raise ValueError("地区数据库内容缺少国家字段")
                if not normalize_country_code(record["country"].get("iso_code")):
                    raise ValueError("地区数据库国家代码无效")
            return reader, built.isoformat(), built.strftime("%Y-%m")
        except Exception:
            if reader is not None:
                reader.close()
            # Malformed MMDB metadata can also raise TypeError, OverflowError,
            # struct.error or IndexError in the reader. Treat all reader errors
            # as invalid data without exposing paths or library internals.
            raise ValueError("地区数据库无效或无法读取") from None

    @property
    def status(self):
        return {
            "ready": self.reader is not None, "updating": self.lock.locked(),
            "version": self.version, "build_date": self.build_date,
            "source": SOURCE, "error": self.error,
        }

    def lookup(self, address: str):
        if self.reader is None:
            return None
        if address in self.cache:
            return self.cache[address]
        result = None
        try:
            record = self.reader.get(public_ip(address)) or {}
            country = record.get("country") or {}
            code = normalize_country_code(country.get("iso_code"))
            names = country.get("names") or {}
            if code:
                name = names.get("zh-CN") or names.get("en") or code
                if not isinstance(name, str):
                    name = code
                result = {"code": code, "name": name[:80], "source": SOURCE, "version": self.version}
        except Exception:
            # Individual corrupt records must not break the candidate list.
            pass
        if len(self.cache) >= 10000:
            self.cache.clear()
        self.cache[address] = result
        return result

    def decorate(self, candidate):
        return dict(candidate, country=self.lookup(candidate["host"]))

    def _install(self, compressed: bytes, *, deadline=None):
        if len(compressed) > COMPRESSED_LIMIT:
            raise ValueError("地区库下载超过 8 MiB 上限")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                content = stream.read(DATABASE_LIMIT + 1)
            if len(content) > DATABASE_LIMIT:
                raise ValueError("地区数据库解压超过 32 MiB 上限")
        except (OSError, EOFError, zlib.error):
            raise ValueError("地区库 gzip 文件损坏或不完整") from None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        new_reader = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".dbip-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            new_reader, built, version = self._read(temporary)
            # asyncio's timer cannot interrupt bounded synchronous decoding or
            # fsync. Check again before committing, so an expired update never
            # replaces a working database after the network deadline.
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            os.replace(temporary, self.path)
            # There is no await between replace and swap: other requests see
            # either complete old data or complete new data, never half a file.
            old_reader = self.reader
            self.reader, self.build_date, self.version = new_reader, built, version
            new_reader = None
            self.cache.clear()
            if old_reader is not None:
                old_reader.close()
        finally:
            if new_reader is not None:
                new_reader.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def update(self, *, proxy_url=None):
        if self.lock.locked():
            raise ValueError("地区数据库正在更新，请稍后再试")
        async with self.lock:
            self.error = None
            now = datetime.now(timezone.utc)
            this_month = now.replace(day=1)
            months = (this_month.strftime("%Y-%m"), (this_month - timedelta(days=1)).strftime("%Y-%m"))
            try:
                deadline = time.monotonic() + 60
                async with asyncio.timeout(60):
                    for index, month in enumerate(months):
                        try:
                            content = await fetch_bytes(
                                f"https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz",
                                proxy_url=proxy_url, max_bytes=COMPRESSED_LIMIT, timeout=60,
                            )
                        except SourceFetchError as exc:
                            if index == 0 and exc.status_code == 404:
                                continue
                            raise
                        self._install(content, deadline=deadline)
                        break
            except asyncio.CancelledError:
                self.error = "地区库更新已取消，保留原有数据库"
                raise
            except SourceFetchError as exc:
                self.error = f"地区库下载失败：{exc}；保留原有数据库"
                raise ValueError(self.error) from None
            except TimeoutError:
                self.error = "地区库更新超时，保留原有数据库"
                raise ValueError(self.error) from None
            except (OSError, ValueError, maxminddb.InvalidDatabaseError):
                self.error = "地区库验证或保存失败，保留原有数据库"
                raise ValueError(self.error) from None
        return self.status

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.cache.clear()
