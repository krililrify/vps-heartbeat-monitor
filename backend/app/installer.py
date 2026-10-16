"""SSH 自动安装/卸载。只执行预定义操作；密码只用于本次连接，不落库、不入日志。"""
import base64
import hashlib
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("hbm.installer")
ARCH = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
BIN, ENV, UNIT_PATH = "/usr/local/bin/hbm-probe", "/etc/hbm-probe.env", "/etc/systemd/system/hbm-probe.service"
UNIT = """[Unit]
Description=VPS Heartbeat Probe
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/hbm-probe.env
ExecStart=/usr/local/bin/hbm-probe
Restart=always
RestartSec=5
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""
START_CMD = "systemctl daemon-reload && systemctl enable --now hbm-probe.service"
UNINSTALL_CMD = f"systemctl disable --now hbm-probe.service; rm -f {UNIT_PATH} {BIN} {ENV}; systemctl daemon-reload"


class InstallError(Exception):
    """message 仅含固定的、安全的中文说明。"""


def friendly(e):
    if isinstance(e, InstallError):
        return str(e)
    n = type(e).__name__
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "连接超时，请检查 IP、SSH 端口和防火墙"
    if isinstance(e, ConnectionRefusedError):
        return "连接被拒绝，请检查 SSH 端口"
    if n == "AuthenticationException":
        return "SSH 认证失败，请检查 root 密码，并确认允许 root 密码登录"
    if n in ("SSHException", "NoValidConnectionsError", "BadHostKeyException"):
        return "SSH 连接失败（协议或网络错误）"
    if isinstance(e, OSError):
        return "网络错误，无法连接目标 VPS"
    return f"安装出错（{n}）"


class ParamikoSession:
    def __init__(self, host, port, password, known_fp=None, timeout=10):
        self.host, self.port, self._pw, self.known, self.timeout = host, port, password, known_fp, timeout
        self.fp, self.client = None, None

    def connect(self):
        import paramiko
        outer = self

        class Policy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                outer.fp = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
                if outer.known and outer.known != outer.fp:
                    raise InstallError("SSH 主机指纹与首次记录不一致（可能遭遇中间人攻击，或系统已重装）")

        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(Policy())
        self.client = c
        try:
            c.connect(self.host, port=self.port, username="root", password=self._pw, timeout=self.timeout,
                      banner_timeout=self.timeout, auth_timeout=self.timeout, allow_agent=False, look_for_keys=False)
        finally:
            self._pw = None
        return self.fp

    def run(self, cmd, timeout=60):
        _, out, _ = self.client.exec_command(cmd, timeout=timeout)
        text = out.read().decode(errors="replace")
        return out.channel.recv_exit_status(), text

    def write(self, path, data, mode):
        sftp = self.client.open_sftp()
        try:
            tmp = path + ".hbm-new"
            with sftp.open(tmp, "wb") as f:
                f.write(data)
            sftp.chmod(tmp, mode)
            sftp.posix_rename(tmp, path)
        finally:
            sftp.close()

    def close(self):
        self._pw = None
        if self.client:
            self.client.close()


def run_job(store, job_id, node_id, host, port, password, token, cfg, mode="install", factory=None, sleep=time.sleep):
    factory = factory or ParamikoSession
    sess, uploaded = None, False
    try:
        store.set_job(job_id, "ssh")
        node = store.get_node(node_id)
        sess = factory(host, port, password, node and node["host_fingerprint"])
        password = None
        fp = sess.connect()
        store.set_fingerprint(node_id, fp)
        if mode == "uninstall":
            store.set_job(job_id, "installing")
            sess.run(UNINSTALL_CMD)
            store.clear_token(node_id)
            store.set_status(node_id, "pending", "探针已卸载")
            store.set_job(job_id, "done", "done")
            return
        store.set_job(job_id, "installing")
        _, out = sess.run("uname -sm")
        parts = out.split()
        if len(parts) < 2 or parts[0] != "Linux":
            raise InstallError(f"仅支持 Linux 系统（检测到：{out.strip()[:30] or '未知'}）")
        arch = ARCH.get(parts[1])
        if not arch:
            raise InstallError(f"不支持的 CPU 架构：{parts[1][:20]}（仅支持 x86_64 / ARM64）")
        if sess.run("test -d /run/systemd/system")[0] != 0:
            raise InstallError("目标系统未使用 systemd，无法安装服务")
        path = os.path.join(cfg["probe_dir"], f"hbm-probe-linux-{arch}")
        if not os.path.isfile(path):
            raise InstallError(f"控制端缺少 {arch} 架构的探针文件，请先构建探针")
        with open(path, "rb") as f:
            data = f.read()
        uploaded = True
        sess.write(BIN, data, 0o755)
        sess.write(ENV, f"HBM_URL={cfg['public_url']}\nHBM_NODE_ID={node_id}\nHBM_TOKEN={token}\n".encode(), 0o600)
        sess.write(UNIT_PATH, UNIT.encode(), 0o644)
        store.set_job(job_id, "starting")
        if sess.run(START_CMD)[0] != 0:
            raise InstallError("启动探针服务失败（systemctl 返回错误）")
        store.set_job(job_id, "heartbeat")
        deadline = time.monotonic() + cfg.get("wait_seconds", 60)
        while time.monotonic() < deadline:
            n = store.get_node(node_id)
            if n and n["status"] == "online":
                store.set_job(job_id, "done", "done")
                return
            sleep(1)
        raise InstallError("探针已启动，但未在限定时间内收到首次心跳，请检查目标 VPS 能否访问控制端 HTTPS 地址")
    except Exception as e:  # 只记录固定文案，不记录异常内容/堆栈（避免泄漏敏感信息）
        msg = friendly(e)
        log.warning("job %s failed: %s", job_id, msg)
        if mode == "install":
            if uploaded and sess:
                try:
                    sess.run(UNINSTALL_CMD)  # 清理不完整的安装
                except Exception:
                    pass
            store.set_status(node_id, "install_failed", msg)
        store.set_job(job_id, "failed", "failed", msg)
    finally:
        password = token = None
        if sess:
            try:
                sess.close()
            except Exception:
                pass


class InstallManager:
    def __init__(self, store, cfg, workers=5, max_pending=50, factory=None):
        self.store, self.cfg, self.factory = store, cfg, factory
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="install")
        self.max_pending, self.pending, self.lock = max_pending, 0, threading.Lock()

    def submit(self, node_id, host, port, password, token, mode="install"):
        with self.lock:
            if self.pending >= self.max_pending:
                raise RuntimeError("安装队列已满，请稍后再试")
            self.pending += 1
        job = self.store.create_job(node_id)
        box = [password]

        def task():
            try:
                run_job(self.store, job, node_id, host, port, box.pop(), token, self.cfg, mode, self.factory)
            finally:
                with self.lock:
                    self.pending -= 1
        self.pool.submit(task)
        return job
