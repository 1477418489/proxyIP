FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CFPROXY_HOST=0.0.0.0 \
    CFPROXY_PORT=8000 \
    CFPROXY_DB=/data/cfproxy.sqlite3

WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 cfproxy \
    && useradd --uid 10001 --gid 10001 --no-create-home cfproxy \
    && mkdir -p /data \
    && chown 10001:10001 /data

COPY --chown=10001:10001 cfproxy ./cfproxy

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "cfproxy"]
