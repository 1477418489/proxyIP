from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "data/cfproxy.sqlite3"
    token: str = ""
    worker_url: str = ""
    worker_token: str = ""
    fetch_proxy: str = field(default="", repr=False)
    geo_db: str = ""

    def __post_init__(self):
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host == "localhost"
        if not loopback and not self.token:
            raise ValueError("非回环监听必须设置 CFPROXY_TOKEN（至少 16 字符）")
        if self.token and len(self.token) < 16:
            raise ValueError("CFPROXY_TOKEN 至少需要 16 字符")
        if not 1 <= self.port <= 65535:
            raise ValueError("CFPROXY_PORT 必须为 1–65535")
        if bool(self.worker_url) != bool(self.worker_token):
            raise ValueError("CFPROXY_WORKER_URL 与 CFPROXY_WORKER_TOKEN 必须同时设置")
        if self.worker_url:
            from .network import validate_https_url

            validate_https_url(self.worker_url, root_only=True)
            if len(self.worker_token) < 32:
                raise ValueError("CFPROXY_WORKER_TOKEN 至少需要 32 字符")
        if self.fetch_proxy:
            from .network import validate_proxy_url

            validate_proxy_url(self.fetch_proxy)

    @property
    def worker_configured(self):
        return bool(self.worker_url and self.worker_token)

    @classmethod
    def from_env(cls):
        load_dotenv(Path.cwd() / ".env", override=False)
        return cls(
            host=os.getenv("CFPROXY_HOST", "127.0.0.1"),
            port=int(os.getenv("CFPROXY_PORT", "8000")),
            db_path=os.getenv("CFPROXY_DB", "data/cfproxy.sqlite3"),
            token=os.getenv("CFPROXY_TOKEN", ""),
            worker_url=os.getenv("CFPROXY_WORKER_URL", ""),
            worker_token=os.getenv("CFPROXY_WORKER_TOKEN", ""),
            fetch_proxy=os.getenv("CFPROXY_FETCH_PROXY", ""),
            geo_db=os.getenv("CFPROXY_GEO_DB", ""),
        )
