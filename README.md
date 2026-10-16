# VPS Heartbeat Monitor

面向个人 VPS 运维者的**轻量级心跳监控面板**（100–500 台）。部署一次控制端，在网页填写 VPS 的 IP、SSH 端口和 root 密码，系统自动通过 SSH 安装探针；探针每 30 秒向控制端发送心跳，超过 90 秒没有有效心跳即显示离线并记录故障历史。**不依赖任何外部通知服务。**

## 架构

```
海外 VPS: hbm-probe (Go, systemd) ──HTTPS POST /api/heartbeat──▶ 反向代理(Caddy/Nginx, HTTPS)
                                                                  └▶ 控制端容器: FastAPI + SQLite(WAL) + Vue3 静态页
浏览器 ──HTTPS，每 5 秒轮询 /api/nodes──▶ 同上
```

- 后端：Python FastAPI（同步接口 + 线程池）；数据库：SQLite（`/data` 卷）；前端：Vue 3 + Vite；探针：Go 静态二进制（amd64 / arm64）。
- 选择 SQLite 的原因：500 台 × 30 秒 ≈ 17 次写入/秒，对 SQLite(WAL) 绰绰有余；无需 Redis 等额外组件。
- 状态判定由后端单线程定时扫描（每 5 秒一次 `sweep`），**不依赖浏览器是否打开**，心跳接口不做全表扫描。

## 功能清单

管理员登录（scrypt 哈希、HttpOnly+SameSite=Strict Cookie、CSRF 头、登录失败限速）· 仪表盘 · 节点列表（搜索/筛选/分页/编辑/删除/重装/卸载）· SSH 自动安装（带进度）· 心跳与 90 秒离线判定 · 故障事件（每个故障周期一条，恢复时更新原记录）· 故障历史（按节点/时间筛选、手动删除、手动清理，均需二次确认）· 审计记录。

**明确不做**：Telegram/Webhook 等任何外部通知、端口/HTTP 检测、资源监控、GFW 诊断、多租户。

## 心跳工作原理

- 探针每 30 秒 `POST /api/heartbeat`（Header `Authorization: Bearer <节点Token>`，Body `{"node_id": N}`），请求超时 10 秒，失败继续重试。
- 控制端用**服务器时钟**记录接收时间；每个节点独立 Token，数据库只存 SHA-256 哈希，原始 Token 只在安装时写入目标 VPS 的 `/etc/hbm-probe.env`（权限 600）。
- 状态：`pending`（等待首次心跳）、`installing`、`online`、`offline`（>90 秒）、`install_failed`。安装失败的节点即使收到心跳也不会显示为在线。
- 控制端重启时立即从数据库重新计算：超过 90 秒无心跳的节点会被标记离线。**注意**：控制端自身停机期间探针无法上报，重启后这些节点会先被判为离线，收到心跳后自动恢复（产生一条故障事件）。

## Docker 部署

```bash
git clone <你的仓库地址> vps-heartbeat-monitor && cd vps-heartbeat-monitor
cp .env.example .env        # 编辑：PUBLIC_URL、ADMIN_USERNAME、ADMIN_PASSWORD、SELF_IPS
docker compose up -d --build
docker compose ps           # 健康检查应为 healthy
```

首次启动会用 `.env` 中的账号创建管理员（密码 <12 位或常见弱密码将拒绝启动）。创建后可从 `.env` 删除 `ADMIN_PASSWORD`。
忘记密码：`docker compose exec hbm python -c "import sqlite3;c=sqlite3.connect('/data/hbm.db');c.execute('delete from admin');c.commit()"`，在 `.env` 设置新账号后 `docker compose restart`。

### HTTPS 配置（必须）

容器只监听 `127.0.0.1:8000`。用 Caddy 反代并自动签发证书（`Caddyfile`）：

```
hbm.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

`PUBLIC_URL` 必须是 `https://hbm.example.com`。管理员密码和 SSH 密码都经过此连接传输，**不要用明文 HTTP 暴露**。

## 添加第一台 VPS

1. 登录后进入「添加 VPS」，填写名称、IP、SSH 端口、root 密码，点「连接并安装」。
2. 页面依次显示：等待连接 → 验证 SSH → 安装探针 → 启动服务 → 等待首次心跳 → 安装成功。
3. 目标 VPS 需：Linux（x86_64/aarch64）、systemd、允许 root 密码登录、能访问控制端 HTTPS 地址。
4. 首次连接会记录 SSH 主机指纹（TOFU），之后重装若指纹不一致会拒绝并提示。

### 验证掉线判定

在目标 VPS 执行 `systemctl stop hbm-probe`，约 90–95 秒后节点变红为「离线」，「故障历史」出现一条「故障中」记录；`systemctl start hbm-probe` 后下一次心跳到达即恢复，原记录更新为「已恢复」。

### 安装失败排查

| 提示 | 处理 |
|---|---|
| 连接超时/被拒绝 | 检查 IP、SSH 端口、目标防火墙 |
| SSH 认证失败 | 检查密码；`PermitRootLogin yes` 与 `PasswordAuthentication yes` |
| 主机指纹不一致 | 确认 VPS 是否重装；确认安全后可删除节点后重新添加 |
| 不支持的架构/系统 | 仅支持 Linux x86_64 / ARM64 + systemd |
| 未收到首次心跳 | 目标 VPS 上 `journalctl -u hbm-probe -n 50`；检查能否访问 `PUBLIC_URL` |
| 控制端缺少探针文件 | 镜像构建未成功编译 Go 探针，查看 `docker compose build` 日志 |

## 故障历史管理

默认**永久保留，不自动清理**。删除节点不会删除其历史（未恢复事件标记为「节点已删除」）。删除选中记录、按时间清理都需要二次确认（清理还需输入「清理历史」），故障中的事件不可删除。请自行关注磁盘空间和备份。

## 备份与恢复

```bash
# 备份（在线一致性备份）
docker compose exec hbm python -c "import sqlite3;s=sqlite3.connect('/data/hbm.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"
docker compose cp hbm:/data/backup.db ./backup-$(date +%F).db
# 恢复
docker compose stop hbm
docker compose cp ./backup-2026-01-01.db hbm:/data/hbm.db
docker compose run --rm --no-deps --entrypoint sh hbm -c "rm -f /data/hbm.db-wal /data/hbm.db-shm"
docker compose start hbm
```

## 探针卸载

推荐：节点详情 → 输入 root 密码 →「卸载探针」。手动：

```bash
systemctl disable --now hbm-probe.service
rm -f /etc/systemd/system/hbm-probe.service /usr/local/bin/hbm-probe /etc/hbm-probe.env && systemctl daemon-reload
```

## 升级 / 卸载控制端

```bash
git pull && docker compose up -d --build     # 数据卷保留；升级前先备份
docker compose down -v                       # 彻底卸载（-v 会删除数据卷！）
```

## 安全注意事项

- 该系统能用 root 密码登录你的 VPS，务必使用强管理员密码，仅经 HTTPS 访问，并限制管理页面来源 IP（反代层）。
- SSH 密码只在本次任务内存中使用：不入库、不入日志；校验错误响应不回显输入；错误信息只记录固定文案。
- 安装只执行预定义命令，网页不提供任意 Shell。目标仅允许公网 IP 字面量，阻止内网/回环/链路本地/组播/云元数据地址和 `SELF_IPS`（请填写控制端公网 IP）。
- 安装并发默认 5、连接超时 10 秒、队列上限 50。CORS 默认关闭（同源）。
- 登录限速按来源 IP 计（经反代时依赖 `X-Forwarded-For`，因此容器端口只绑定 127.0.0.1）。

## 已知限制

- 单管理员、单实例（SQLite）；不做高可用。
- 探针 Token 以明文存放在目标 VPS（权限 600）；VPS 被入侵则该 Token 可被滥用，仅影响该节点的心跳。
- 主机指纹为首次信任（TOFU），首次连接无法抵御中间人。
- 前端仅适配桌面浏览器，未提供 `package-lock.json`（首次 `npm install` 后建议提交）。

## 开发与测试

```bash
cd backend && pip install -r requirements.txt -r requirements-dev.txt && python -m pytest -v
cd frontend && npm install && npm run build
cd probe && go test ./... 
```

## 许可证

MIT：宽松、便于个人和商业使用。依赖许可证：FastAPI(MIT)、uvicorn(BSD-3)、pydantic(MIT)、paramiko(**LGPL-2.1**，作为未修改的独立依赖动态使用，与 MIT 项目兼容；分发镜像时需保留其许可证声明)、Vue/Vite(MIT)、Go 标准库(BSD-3，探针仅用标准库)。
