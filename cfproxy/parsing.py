import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import ipaddress
import re

from .network import MAX_DOWNLOAD, public_ip, resolve_public

MAX_IMPORT = 500
MAX_CANDIDATES = 5000


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_endpoint(value: str) -> tuple[str, int]:
    text = value.strip()
    if not text or len(text) > 300 or re.search(r"[\s/@?#%\\]", text):
        raise ValueError("请输入 IP/域名及可选端口，不接受 URL、凭据或空白")
    port = 443
    if text.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", text)
        if not match:
            raise ValueError("IPv6 端口格式应为 [IPv6]:port")
        host = match[1]
        if ipaddress.ip_address(host).version != 6:
            raise ValueError("方括号只适用于 IPv6")
        if match[2]:
            port = int(match[2])
    elif text.count(":") == 1:
        host, raw_port = text.rsplit(":", 1)
        if not re.fullmatch(r"[0-9]{1,5}", raw_port):
            raise ValueError("端口必须是数字")
        port = int(raw_port)
    else:
        host = text
    if not 1 <= port <= 65535 or port == 25:
        raise ValueError("端口必须在 1–65535，且不能为 Workers 禁止的 25")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if ":" in host or re.fullmatch(r"[0-9.]+", host):
            raise ValueError("IP 地址格式不正确")
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if len(host) > 253 or "." not in host or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in host.split(".")
        ):
            raise ValueError("域名格式不正确")
        return host, port
    return public_ip(str(ip)), port


@dataclass
class Seed:
    host: str
    port: int
    source: str
    origin: str
    resolved_at: str


def input_records(text: str, format: str):
    text = text.lstrip("\ufeff\r\n \t")
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    if format == "auto":
        fields = [f.strip().lower() for f in next(csv.reader([first_line]), [])]
        # A CSV can legitimately omit the port (default 443). Treating its
        # other IP-valued columns as TXT could import an exit IP as a candidate.
        format = "csv" if "ip" in fields else "txt"
    if format == "csv":
        reader = csv.DictReader(io.StringIO(text))
        columns = {name.strip().lower(): name for name in reader.fieldnames or []}
        if "ip" not in columns:
            raise ValueError("CSV 必须含 IP 列，可选 port/端口列；不能用 cf-meta-ip 代替")
        for row in reader:
            host = (row.get(columns["ip"]) or "").strip()
            port_col = columns.get("port", columns.get("端口"))
            port = (row.get(port_col) or "443").strip()
            yield endpoint(host, port) if ":" in host and not host.startswith("[") else f"{host}:{port}"
    else:
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            for item in re.split(r"[,，]", line):
                if item.strip():
                    yield item.strip()


async def parse_import(text: str, source="手动导入", format="auto"):
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise ValueError("导入文本必须是有效 UTF-8") from None
    if len(encoded) > MAX_DOWNLOAD:
        raise ValueError("导入文本超过 2 MiB")
    records = []
    truncated = False
    try:
        for item in input_records(text, format):
            if len(records) >= MAX_IMPORT:
                truncated = True
                break
            records.append(item)
    except csv.Error:
        raise ValueError("CSV 格式无效或单个字段超过 128 KiB") from None
    if not records:
        raise ValueError("没有可导入的候选地址")
    semaphore = asyncio.Semaphore(8)

    async def expand(raw):
        try:
            host, port = parse_endpoint(raw)
            async with semaphore:
                addresses = await resolve_public(host, port)
            now = utcnow()
            return [Seed(ip, port, source, raw, now) for ip in addresses], None
        except (ValueError, OSError, TimeoutError, UnicodeError) as exc:
            reason = str(exc) or "DNS 解析超时"
            return [], f"{raw[:120]}：{reason[:160]}"

    expanded = await asyncio.gather(*(expand(raw) for raw in records))
    seeds = []
    seen = set()
    errors = []
    duplicates = 0
    rejected = 0
    for items, error in expanded:
        if error:
            rejected += 1
            if len(errors) < 20:
                errors.append(error)
        for seed in items:
            key = endpoint(seed.host, seed.port)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if len(seeds) >= MAX_IMPORT:
                truncated = True
                continue
            seeds.append(seed)
    return seeds, {
        "read": len(records),
        "resolved": len(seeds),
        "duplicates": duplicates,
        "rejected": rejected,
        "truncated": truncated,
        "errors": errors,
    }
