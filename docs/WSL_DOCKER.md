# 在 WSL2 中用 Docker Compose 部署 cfPROXYIP

本指南对应当前项目：Windows源码在 `C:\Users\Admin\Desktop\cfPROXYIP`，WSL发行版为Ubuntu。2026-11-09实查WSL2、Docker Engine 29.6.1和Compose v5.3.0已可用，Docker运行在WSL内部。本指南不需要再安装Docker Desktop。

部署的是Python管理服务。Cloudflare Worker探针仍单独部署，步骤见 [Worker指南](../worker/README.md)。

## 1. 打开 WSL，确认 Docker 可用

在Windows PowerShell运行：

```powershell
wsl -d Ubuntu
```

下文除注明PowerShell的段落外，都在WSL Ubuntu终端运行：

```bash
docker version
docker compose version
```

`docker version`要同时显示Client与Server。若提示无法连接daemon，原生Docker检查 `sudo systemctl status docker`；Docker Desktop用户检查WSL Integration是否启用。不要在已有可用Docker的系统里再安装另一套。

## 2. 将源码复制到 WSL 文件系统

推荐放在WSL家目录，避免Windows虚拟环境、文件权限和跨文件系统IO带来的差异：

```bash
mkdir -p ~/cfproxyip
rsync -a \
  --exclude='.venv/' --exclude='node_modules/' --exclude='.wrangler/' \
  --exclude='__pycache__/' --exclude='.pytest_cache/' --exclude='.git/' \
  --exclude='data/' --exclude='artifacts/' --exclude='.playwright-mcp/' \
  --include='.env.example' --exclude='.env' --exclude='.env.*' \
  --include='.dev.vars.example' --exclude='.dev.vars*' \
  /mnt/c/Users/Admin/Desktop/cfPROXYIP/ ~/cfproxyip/
cd ~/cfproxyip
```

后续更新可重复以上同步命令。它不会删除WSL侧文件，也不会覆盖已有 `.env`、数据目录或密钥。若Windows用户名或项目目录不同，修改源路径。

## 3. 创建配置，选择 8001 端口

Windows旧服务仍使用8000，本指南让Docker发布8001，避免打开旧版本页面。容器内部固定监听8000，`CFPROXY_HTTP_PORT`仅控制宿主机映射。

运行以下命令：没有 `.env` 时创建并生成随机管理密钥，已有文件时完整保留。

```bash
python3 - <<'PY'
from pathlib import Path
import os
import re
import secrets

path = Path('.env')
if path.exists():
    print('.env 已存在，已保留；请检查端口和管理密钥。')
else:
    text = Path('.env.example').read_text(encoding='utf-8')
    text = re.sub(r'^CFPROXY_TOKEN=.*$', 'CFPROXY_TOKEN=' + secrets.token_hex(32), text, flags=re.M)
    text = re.sub(r'^CFPROXY_HTTP_PORT=.*$', 'CFPROXY_HTTP_PORT=8001', text, flags=re.M)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(text)
    print('已创建 .env；管理密钥保存在 CFPROXY_TOKEN，访问端口为8001。')
PY
nano .env
```

确认 `CFPROXY_HTTP_PORT=8001`，并保存 `CFPROXY_TOKEN` 的实际值，稍后在Web页面“管理密钥”中输入。管理密钥至少16字符；不要把示例说明文字当作密钥。

如已部署自己的Worker，填写两项；未部署就都留空：

```dotenv
CFPROXY_WORKER_URL=https://实际Worker名称.实际子域.workers.dev
CFPROXY_WORKER_TOKEN=与Worker的PROBE_TOKEN相同的随机密钥
```

`CFPROXY_GEO_DB`保持空即可，地区库会使用 `/data/dbip-country-lite.mmdb`。Compose会将 `CFPROXY_HOST`、`CFPROXY_PORT`、`CFPROXY_DB` 设置为容器需要的值，不必填写Windows路径。

## 4. 启动并验证

首次启动会创建独立的Docker命名卷，**不会自动读取Windows现有候选和历史**。要保留已有数据，先完成下文“可选：迁移Windows现有数据”，再执行本节启动命令。

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=80 cfproxy
```

Windows浏览器打开 <http://127.0.0.1:8001>，在“管理密钥”中输入 `.env` 的 `CFPROXY_TOKEN`。WSL2通常会把本地监听服务转发给Windows；如果页面打不开，先在WSL运行 `curl -I http://127.0.0.1:8001/`，再检查端口映射、Windows端口占用或本机的WSL网络设置。

用容器中的实际配置核对API，可避免误访问旧Windows服务，也不需要把密钥放进命令行：

```bash
docker compose exec -T cfproxy python - <<'PY'
import json
import os
import urllib.request

headers = {'Authorization': 'Bearer ' + os.environ['CFPROXY_TOKEN']}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
    request = urllib.request.Request('http://127.0.0.1:8000' + path, headers=headers)
    with opener.open(request, timeout=5) as response:
        return json.load(response)

schema = get('/openapi.json')
status = get('/api/status')
assert '/api/geo/update' in schema['paths'], '后端版本过旧，请重新构建镜像'
print(json.dumps({'version': schema['info']['version'], 'counts': status['counts'], 'geo': status['geo']}, ensure_ascii=False, indent=2))
PY
```

本版本应包含 `/api/geo/update` 接口和 `geo` 状态。未迁移时初始候选数为0，主动导入后再检测。新容器尚无地区库时，点击“下载地区库”即可。

## 5. 可选：使用 Windows 上的 HTTP / Mixed 代理

容器中的 `127.0.0.1` 指容器自身。你当前使用原生WSL Docker和NAT网络，从容器访问Windows代理应使用Windows在WSL网络中的IP。

在WSL查询：

```bash
ip route show default
```

`default via` 后面的地址是Windows网关。本机本次查到 `172.26.128.1`，并验证从WSL到该地址7897端口的TCP连接可建立；地址可能随WSL重启变化。这个TCP检查不等同容器中完整代理下载已验证。

Windows代理软件需要允许WSL虚拟网络的连接（Clash类软件通常称“允许局域网 / Allow LAN”），且Windows防火墙允许该来源。无需把代理暴露给公网。

按实际IP和代理端口在 `.env` 配置，例如：

```dotenv
CFPROXY_FETCH_PROXY=http://172.26.128.1:7897
```

保存后重新创建容器以读取环境：

```bash
docker compose up -d --force-recreate
```

在页面“来源与导入”里勾选“使用 HTTP 代理拉取”，代理输入框留空就会用环境默认值；也可直接在输入框填写地址用于当次下载。地区库下载复用这个开关。

如果改用Docker Desktop，通常使用 `http://host.docker.internal:7897`。原生WSL Docker即使添加 `host.docker.internal:host-gateway`，映射到的也是WSL/Docker宿主，不是Windows代理所在机器。WSL镜像网络允许WSL进程访问Windows localhost，也不代表Docker容器的localhost等于Windows。

`CFPROXY_FETCH_PROXY`只影响运行中应用的来源/地区库下载；它不配置 `docker pull`、镜像构建中的pip或本地/Worker检测路径。镜像拉取失败时，需单独排查Docker daemon网络；构建中的pip失败则排查构建网络，不能靠修改这个应用变量解决。

## 可选：迁移 Windows 现有数据

只适用于第一次向**空Docker数据卷**导入。直接运行本节导入命令遇到已有数据会拒绝覆盖。若已在Docker中使用过项目，应先另行备份并规划合并，不要删除卷来绕过检查。

先等Windows端检测任务结束，停止旧Python服务，避免迁移期间继续产生新历史。在Windows项目目录的PowerShell执行：

```powershell
@'
from pathlib import Path
import shutil
import sqlite3

source = Path('data/cfproxy.sqlite3').resolve()
folder = Path('data/wsl-migration')
folder.mkdir(exist_ok=True)
target = folder / 'cfproxy.sqlite3'
if not source.is_file():
    raise SystemExit('原数据库不存在，请检查项目目录')
if target.exists():
    raise SystemExit('迁移快照已存在，请先另存旧快照，避免覆盖')
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
    with sqlite3.connect(target) as snapshot:
        original.backup(snapshot)
geo = source.parent / 'dbip-country-lite.mmdb'
if geo.is_file():
    shutil.copy2(geo, folder / geo.name)
print('一致性快照已保存到 data/wsl-migration')
'@ | .\.venv\Scripts\python.exe -
```

SQLite backup接口会包含WAL中的已提交数据，不能用复制单个运行中 `.sqlite3` 文件代替。快照包含候选、来源、任务和结果；`.env`不包含在迁移文件里，Worker配置需在WSL单独填写。

返回WSL，将下面整块命令一起执行：创建新的迁移目录、复制快照、构建镜像，用一次性容器导入Compose实际使用的数据卷并设置权限，最后启动应用。任一步失败都会中止，导入成功才启动。若已有 `migration` 目录，先另存旧快照，再使用一个新的迁移目录，不要复用残留文件。

```bash
(
set -e
cd ~/cfproxyip
mkdir migration
rsync -a /mnt/c/Users/Admin/Desktop/cfPROXYIP/data/wsl-migration/ ./migration/
test -s migration/cfproxy.sqlite3
docker compose config --quiet
docker compose build
docker compose run --rm --no-deps --user 0:0 \
  -v "$PWD/migration:/migration:ro" --entrypoint python cfproxy -c '
from pathlib import Path
import os
import shutil

source = Path("/migration")
target = Path("/data")
if not (source / "cfproxy.sqlite3").is_file():
    raise SystemExit("迁移数据库不存在，未写入数据卷")
if any(target.iterdir()):
    raise SystemExit("数据卷非空，拒绝覆盖；请保留现有数据")
for name in ("cfproxy.sqlite3", "dbip-country-lite.mmdb"):
    original = source / name
    if original.is_file():
        destination = target / name
        shutil.copyfile(original, destination)
        os.chown(destination, 10001, 10001)
        os.chmod(destination, 0o600)
os.chown(target, 10001, 10001)
print("已导入空数据卷，可以启动应用")
'
docker compose up -d
)
```

然后执行第4节的API检查，确认候选数量与迁移前一致。保留Windows旧库和迁移快照，直到新环境验证完成。本次已有Windows候选数为1,001，实际以生成快照时的数量为准。

## 更新、停止与数据保留

- 更新代码：重复第2节同步，运行 `docker compose up -d --build`。
- 仅修改 `.env`：运行 `docker compose up -d --force-recreate`；`docker compose restart`不会更新容器环境变量。
- 看日志：`docker compose logs -f --tail=100 cfproxy`，Ctrl+C只退出日志查看。
- 停止：`docker compose down`，命名卷仍保留。
- 不要运行 `docker compose down -v`，它会删除候选、历史和地区库所在的命名卷。

一直从同一个WSL项目目录执行Compose，且不要随意改变 `COMPOSE_PROJECT_NAME` 或 `-p`。命名卷归属Compose项目，项目名改变会看起来像启动了一个空库。

`restart: unless-stopped`在Docker daemon启动后生效；它不负责替你在Windows开机时启动WSL。

## 验证范围与参考

已核对本机WSL/Docker状态，Compose默认/自定义端口插值通过配置校验。本文没有执行镜像构建、启动新服务或迁移用户数据，不代表容器端代理和Cloudflare Worker路径已实测。

- [Microsoft：WSL网络、Windows宿主IP及NAT/镜像模式](https://learn.microsoft.com/en-us/windows/wsl/networking)
- [Docker Compose端口配置](https://docs.docker.com/reference/compose-file/services/#ports)
- [Docker Compose环境插值](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
