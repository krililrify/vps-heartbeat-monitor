# VPS Heartbeat Monitor

面向个人 VPS 运维者的**轻量级心跳监控面板**，适用于约 100–500 台 VPS。部署一次控制端，在网页填写 VPS 的 IP、SSH 端口和 root 密码，系统自动通过 SSH 安装探针。探针每 30 秒向控制端发送心跳，超过 90 秒没有有效心跳即显示离线并记录故障历史。

**部署特点：**
- GitHub 拉取源码，Docker Compose 一键构建和启动。
- 使用 Cloudflare Tunnel 提供 HTTPS 访问，无需为监控面板开放公网 8000 端口。
- 通过网页 SSH 自动安装和管理探针。
- SQLite 持久化存储，不依赖 Redis 或外部数据库。
- 不依赖 Telegram、Webhook 等任何外部通知服务。

## 架构

```text
被监控 VPS
└── hbm-probe（Go + systemd）
    └── HTTPS POST /api/heartbeat
                │
                ▼
        Cloudflare Tunnel
                │
                ▼
控制端 VPS
└── Docker Compose
    └── FastAPI + SQLite（WAL）+ Vue 3 静态页面
                ▲
                │
        Cloudflare Tunnel
                ▲
                │ HTTPS，每 5 秒轮询 /api/nodes
              浏览器
```

- **后端：** Python FastAPI，同步接口与线程池。
- **数据库：** SQLite，使用 `/data` 数据卷持久化。
- **前端：** Vue 3 + Vite。
- **探针：** Go 静态二进制，支持 Linux amd64 和 arm64。
- **公网入口：** Cloudflare Tunnel。
- **状态判定：** 后端每 5 秒执行一次 `sweep`，不依赖浏览器是否打开；心跳接口不执行全表扫描。

500 台节点每 30 秒上报一次，平均约 17 次心跳请求/秒。SQLite WAL 适合这一规模，但实际容量仍取决于磁盘性能、故障事件数量及其他数据库操作。

## 功能清单

- 管理员登录、scrypt 密码哈希、HttpOnly + SameSite=Strict Cookie、CSRF 请求头及登录失败限速。
- 监控仪表盘。
- 节点列表：搜索、筛选、分页、编辑、删除、重装、卸载。
- SSH 自动安装探针，并显示安装进度。
- 每 30 秒上报心跳，超过 90 秒无有效心跳判定离线。
- 故障事件：每个故障周期生成一条记录，恢复时更新原记录。
- 故障历史：按节点和时间筛选、手动删除、按时间清理，危险操作需要二次确认。
- 审计记录。
- 控制端重启后重新计算节点状态。

**明确不做：** Telegram、Webhook 等外部通知，端口或 HTTP 可用性检测，CPU、内存、流量等资源监控，GFW 诊断及多租户。

## 心跳工作原理

探针每 30 秒向控制端发送一次请求：

```http
POST /api/heartbeat
Authorization: Bearer <节点Token>
Content-Type: application/json

{"node_id": 1}
```

- 请求超时为 10 秒，失败后继续重试。
- 控制端使用自身服务器时钟记录心跳接收时间。
- 每个节点拥有独立 Token，数据库仅保存 Token 的 SHA-256 哈希。
- 原始 Token 写入被监控 VPS 的 `/etc/hbm-probe.env`，文件权限为 `600`。
- 控制端每 5 秒扫描节点状态，超过 90 秒没有有效心跳即判定离线。

节点状态：

| 状态 | 含义 |
|---|---|
| `pending` | 等待首次心跳 |
| `installing` | 正在安装探针 |
| `online` | 在线 |
| `offline` | 超过 90 秒未收到有效心跳 |
| `install_failed` | 安装失败 |

安装失败的节点即使收到心跳，也不会显示为在线。

**控制端停机说明：** 控制端停机期间无法接收心跳。重启后，超时节点可能先被标记离线；探针恢复上报后，节点自动恢复在线，并可能产生相应故障事件。

## Docker 部署控制端

以下以 Ubuntu 22.04、GitHub 公共仓库和 Cloudflare Tunnel 为例。

### 1. 安装依赖

使用 root 账号 SSH 登录控制端 VPS。

```bash
apt-get update
apt-get install -y ca-certificates curl git
apt-get install -y docker.io docker-compose-v2
systemctl enable --now docker
```

检查 Docker：

```bash
docker --version
docker compose version
```

如果系统已经安装可用的 Docker 和 Docker Compose 插件，可跳过安装步骤。

### 2. 从 GitHub 拉取项目

```bash
cd /root
git clone https://github.com/krililrify/vps-heartbeat-monitor.git
cd /root/vps-heartbeat-monitor
```

项目目录：

```text
/root/vps-heartbeat-monitor
```

如果该目录已经存在，不要重复克隆；进入现有目录继续操作。

### 3. 配置环境变量

复制配置模板：

```bash
cd /root/vps-heartbeat-monitor
cp .env.example .env
nano .env
```

根据自己的部署情况填写：

```dotenv
PUBLIC_URL=https://ip.govg.de
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请替换为你自己的强密码
SELF_IPS=控制端VPS的公网IP
```

参数说明：

- `PUBLIC_URL`：监控面板的完整 HTTPS 地址，必须与 Cloudflare Tunnel 发布的域名一致。
- `ADMIN_USERNAME`：管理员账号。
- `ADMIN_PASSWORD`：首次启动创建管理员时使用的密码。请使用至少 12 位、不常见的强密码，不要复用其他网站的密码。
- `SELF_IPS`：填写控制端 VPS 的公网 IP，避免系统将控制端自身作为被监控目标。

如果需要填写多个 IP，请以项目实际支持的格式为准。

保存后限制配置文件权限：

```bash
chmod 600 .env
```

### 4. 构建并启动容器

```bash
docker compose up -d --build
```

查看容器状态：

```bash
docker compose ps
```

确认应用容器状态为 `healthy`。

检查本机 HTTP 服务：

```bash
curl -s -o /dev/null -w 'HTTP 状态码：%{http_code}\n' http://127.0.0.1:8000
```

预期返回：

```text
HTTP 状态码：200
```

正常情况下，应用端口应仅绑定本机：

```text
127.0.0.1:8000->8000/tcp
```

**不要将应用端口直接暴露到公网。** 不要将端口映射修改为 `0.0.0.0:8000:8000`。

首次启动会使用 `.env` 中的账号创建管理员，密码少于 12 位或属于常见弱密码时会拒绝启动。

管理员创建成功后，可以按照项目实际配置移除 `.env` 中的 `ADMIN_PASSWORD`，避免长期保留初始化密码。修改前请确认管理员账号已经成功创建，并妥善保管密码。

## HTTPS 配置：Cloudflare Tunnel

本项目推荐使用 Cloudflare Tunnel 发布面板。

与传统 Caddy/Nginx 反向代理方式不同，**使用 Cloudflare Tunnel 不需要在控制端额外安装 Caddy，也不需要开放公网 8000 端口**。

访问链路：

```text
浏览器
  │
  │ HTTPS
  ▼
Cloudflare
  │
  │ Tunnel
  ▼
控制端 VPS
  │
  │ HTTP
  ▼
127.0.0.1:8000
```

### 1. 创建 Cloudflare Tunnel

1. 打开 [Cloudflare Zero Trust 控制台](https://one.dash.cloudflare.com/)。
2. 进入 `Networks` → `Tunnels`。
3. 创建一个 Cloudflared Tunnel，例如命名为 `vps-heartbeat-monitor`。
4. 选择 Linux 系统和对应架构。Ubuntu x86_64 VPS 选择 `amd64`。
5. 按控制台提供的指引安装并连接 Tunnel。

建议为控制端单独创建 Tunnel，并妥善保管 Tunnel Token。

### 2. 安装 cloudflared

以下是 Ubuntu 上的安装命令：

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings

curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null

echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list

sudo apt-get update
sudo apt-get install -y cloudflared
```

检查版本：

```bash
cloudflared --version
```

### 3. 安装并启动 Tunnel 服务

回到 Cloudflare Tunnel 创建页面，在安装指引中选择 Linux，并复制控制台生成的完整安装命令。

通常会提供类似下面的命令：

```bash
sudo cloudflared service install <你的完整TunnelToken>
```

上面只是格式示例，请以 Cloudflare 控制台生成的实际命令为准。

**安全提醒：** Tunnel Token 是敏感凭证，不要公开、发给他人或提交到 GitHub。

安装完成后，返回 Cloudflare 控制台，确认 Tunnel 状态为 `Healthy`，且存在活动连接。

检查本机服务：

```bash
systemctl status cloudflared --no-pager
```

查看日志：

```bash
journalctl -u cloudflared -n 50 --no-pager
```

### 4. 发布监控面板域名

在 Cloudflare Tunnel 中进入应用路由设置，添加一个已发布应用程序。

示例配置：

| 字段 | 填写内容 |
|---|---|
| 主机名 | `ip.govg.de` |
| 路径 | 留空 |
| 服务类型 | `HTTP` |
| 服务地址 | `127.0.0.1:8000` |

如果界面要求完整 URL，则填写：

```text
http://127.0.0.1:8000
```

保存后，浏览器访问：

```text
https://ip.govg.de
```

如果使用其他域名，请将示例替换为自己的域名。

**重要：** `.env` 中的 `PUBLIC_URL` 必须与发布的 HTTPS 域名完全一致。Cloudflare Tunnel 负责公网 HTTPS 入口，到本机服务使用 HTTP 即可。

### 5. 检查整个部署

检查 Docker：

```bash
cd /root/vps-heartbeat-monitor
docker compose ps
```

检查本机服务：

```bash
curl -I http://127.0.0.1:8000
```

检查 Tunnel：

```bash
systemctl status cloudflared --no-pager
journalctl -u cloudflared -n 50 --no-pager
```

最终确认：

- Docker 应用正常运行，健康检查通过。
- 本机 `127.0.0.1:8000` 返回正常响应。
- Cloudflare Tunnel 状态为 `Healthy`。
- 浏览器可以通过 HTTPS 打开登录页面。
- 管理员账号可以正常登录。

如果 Tunnel 健康但网页打不开，优先检查发布的主机名、服务地址、`PUBLIC_URL` 和本机应用状态。

## 添加第一台 VPS

1. 登录监控面板。
2. 进入「添加 VPS」。
3. 填写节点名称、IP、SSH 端口和 root 密码。
4. 点击「连接并安装」。
5. 等待安装流程完成。

页面依次显示：

```text
等待连接
    ↓
验证 SSH
    ↓
安装探针
    ↓
启动服务
    ↓
等待首次心跳
    ↓
安装成功
```

被监控 VPS 需要满足：

- Linux x86_64 或 ARM64。
- 使用 systemd。
- 允许相应的 root 密码登录方式。
- 可以通过 HTTPS 访问控制端的 `PUBLIC_URL`。
- 防火墙允许 SSH 连接。

首次连接会记录 SSH 主机指纹（TOFU）。之后重装时，如果主机指纹不一致，系统会拒绝连接并提示检查。

### 验证掉线判定

在被监控 VPS 上执行：

```bash
systemctl stop hbm-probe
```

等待约 90–95 秒，节点应变为红色「离线」，故障历史中出现一条「故障中」记录。

随后执行：

```bash
systemctl start hbm-probe
```

下一次心跳到达后，节点应恢复在线，原故障记录更新为「已恢复」。

## 安装失败排查

| 提示 | 处理方法 |
|---|---|
| 连接超时或被拒绝 | 检查 IP、SSH 端口、目标 VPS 防火墙 |
| SSH 认证失败 | 检查 root 密码及 SSH 登录配置 |
| 主机指纹不一致 | 确认 VPS 是否重装；核实安全后再删除节点并重新添加 |
| 不支持的架构或系统 | 仅支持 Linux x86_64 / ARM64 + systemd |
| 未收到首次心跳 | 执行 `journalctl -u hbm-probe -n 50`，检查目标 VPS 能否访问 `PUBLIC_URL` |
| 控制端缺少探针文件 | 检查 `docker compose build` 日志，确认探针编译成功 |
| 面板无法访问 | 检查 Docker、Tunnel 状态、发布的主机名和服务地址 |
| Tunnel 不健康 | 查看 `journalctl -u cloudflared -n 50 --no-pager` |

## 故障历史管理

故障历史默认**永久保留，不自动清理**。

- 删除节点不会删除该节点的历史。
- 未恢复的事件在节点删除后会标记为「节点已删除」。
- 删除选中记录和按时间清理均需要二次确认。
- 批量清理还需要输入「清理历史」。
- 故障中的事件不可删除。

请定期检查磁盘空间并备份数据库。长期运行时，故障历史可能持续增加。

## 备份与恢复

### 备份数据库

在控制端项目目录执行：

```bash
cd /root/vps-heartbeat-monitor

docker compose exec hbm python -c "import sqlite3;s=sqlite3.connect('/data/hbm.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"

docker compose cp hbm:/data/backup.db ./backup-$(date +%F).db
```

将生成的备份文件复制到其他安全位置保存，不要只保留在原控制端 VPS 上。

### 恢复数据库

**恢复会覆盖当前数据库。操作前请先备份当前数据，并确认备份文件正确。**

以下示例假设备份文件已经放在项目目录中：

```bash
cd /root/vps-heartbeat-monitor

docker compose stop hbm

docker compose cp ./backup-2026-01-01.db hbm:/data/hbm.db

docker compose run --rm --no-deps --entrypoint sh hbm \
  -c "rm -f /data/hbm.db-wal /data/hbm.db-shm"

docker compose start hbm
```

请将 `backup-2026-01-01.db` 替换为实际备份文件名。

## 探针卸载

推荐在面板节点详情页执行「卸载探针」，并按提示输入 root 密码。

也可以在被监控 VPS 上手动执行：

```bash
systemctl disable --now hbm-probe.service

rm -f \
  /etc/systemd/system/hbm-probe.service \
  /usr/local/bin/hbm-probe \
  /etc/hbm-probe.env

systemctl daemon-reload
```

手动卸载前，请确认服务名称和文件路径与实际安装情况一致。

## 升级控制端

升级前建议先备份数据库。

```bash
cd /root/vps-heartbeat-monitor

git pull
docker compose up -d --build
```

检查更新后状态：

```bash
docker compose ps
docker compose logs --tail=100 hbm
```

升级前请检查项目是否存在数据库迁移或配置变更要求，不要盲目覆盖重要数据。

## 卸载控制端

停止并删除容器：

```bash
cd /root/vps-heartbeat-monitor
docker compose down
```

如果确认不再需要监控数据库，并希望同时删除 Compose 管理的数据卷：

```bash
docker compose down -v
```

**注意：** `docker compose down -v` 会删除相关数据卷，可能导致节点信息、管理员信息和故障历史永久丢失。执行前务必备份。

卸载 Docker 应用不会自动删除 Cloudflare 控制台中的 Tunnel。若不再使用，请在 Cloudflare 控制台中单独移除对应路由和 Tunnel。

## 安全注意事项

- 该系统需要使用 root SSH 凭据登录并管理被监控 VPS，请谨慎选择控制端和被监控节点。
- 管理面板应仅通过 HTTPS 访问，不要使用明文 HTTP 将管理员密码或 SSH 密码暴露给公网。
- SSH 密码只在当前安装任务内存中使用，不入库、不入日志；错误响应不应回显输入密码。
- 安装流程执行预定义命令，网页不提供任意 Shell。
- 目标地址限制为公网 IP 字面量，阻止内网、回环、链路本地、组播、云元数据地址及 `SELF_IPS` 中配置的地址。
- `SELF_IPS` 应填写控制端公网 IP。
- 安装并发默认 5、连接超时 10 秒、队列上限 50。
- CORS 默认关闭，按同源方式工作。
- 登录限速按来源 IP 计。经过代理时，来源 IP 识别依赖代理头配置；不要盲目信任来自不可信来源的 `X-Forwarded-For`。
- 控制端应用端口只绑定 `127.0.0.1`，避免直接从公网访问应用。
- Cloudflare Tunnel Token 属于敏感凭证。若怀疑泄露，请在 Cloudflare 控制台刷新凭证并更新服务。
- 如果面板域名可以被公网访问，可考虑在 Cloudflare 层配置 Access 策略，进一步限制管理页面的访问范围。
- 被监控 VPS 遭到入侵时，攻击者可能读取该节点的探针 Token。应及时移除或重新安装受影响节点的探针。

## 已知限制

- 单管理员、单实例，使用 SQLite，不提供高可用。
- 探针 Token 以明文保存在被监控 VPS 的 `/etc/hbm-probe.env` 中，权限为 `600`。
- 主机指纹采用首次信任（TOFU），首次连接无法完全抵御中间人攻击。
- 前端主要适配桌面浏览器。
- 当前前端未提供 `package-lock.json`，开发环境首次执行 `npm install` 后建议提交锁定文件。
- 控制端自身停机期间无法接收心跳；恢复后可能先记录离线事件，再随着探针上报恢复在线。
- 故障历史默认永久保留，需要自行负责数据库备份和磁盘空间管理。

## 开发与测试

### 后端测试

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -v
```

### 前端构建

```bash
cd frontend
npm install
npm run build
```

### Go 探针测试

```bash
cd probe
go test ./...
```

## 许可证

本项目采用 MIT 许可证，适合个人使用和商业用途。使用、修改和再分发时，请遵守仓库中的许可证文件及各项依赖的许可要求。

主要依赖包括：

- FastAPI：MIT
- uvicorn：BSD-3-Clause
- Pydantic：MIT
- Paramiko：LGPL-2.1
- Vue：MIT
- Vite：MIT
- Go 标准库：BSD 风格许可证

分发项目或镜像时，请保留适用的许可证声明，并自行核实依赖版本对应的许可证及分发义务。
