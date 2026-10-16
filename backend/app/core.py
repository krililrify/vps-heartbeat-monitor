"""核心逻辑（仅依赖标准库）：节点状态、心跳、故障事件、认证、SSRF 校验。
所有时间判断都使用服务器端时钟（可替换为 FakeClock 以便测试）。"""
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import threading
import time

OFFLINE_AFTER = 90          # 超过 90 秒无有效心跳 → offline
SESSION_TTL = 12 * 3600
STATUSES = ("pending", "installing", "online", "offline", "install_failed")
WEAK = {"password", "admin", "changeme", "change-me", "12345678", "123456789012"}


class SystemClock:
    def now(self):
        return time.time()


class FakeClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def now(self):
        return self.t

    def advance(self, s):
        self.t += s


SCHEMA = """
CREATE TABLE IF NOT EXISTS admin(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, pw_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, ip TEXT NOT NULL,
  ssh_port INTEGER NOT NULL DEFAULT 22, note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending', token_hash TEXT, last_heartbeat REAL,
  status_changed REAL NOT NULL, created_at REAL NOT NULL,
  host_fingerprint TEXT, install_error TEXT);
CREATE INDEX IF NOT EXISTS idx_nodes_status_hb ON nodes(status, last_heartbeat);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_ip ON nodes(ip);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER NOT NULL, node_name TEXT NOT NULL,
  node_ip TEXT NOT NULL, offline_at REAL NOT NULL, last_heartbeat REAL,
  recovered_at REAL, duration REAL, status TEXT NOT NULL DEFAULT 'open');
CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id, offline_at);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(offline_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_event ON events(node_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS install_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER NOT NULL, step TEXT NOT NULL,
  status TEXT NOT NULL, error TEXT, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
"""


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(pw, stored):
    try:
        _, salt, dk = stored.split("$")
        new = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(new.hex(), dk)
    except Exception:
        return False


def check_admin_password(pw):
    if len(pw or "") < 12 or pw.lower() in WEAK:
        raise ValueError("管理员密码至少 12 位且不能是常见弱密码")


def check_target(ip, allow_private=False, self_ips=()):
    """SSRF 防护：只接受 IP 字面量（不解析域名，避免 DNS 重绑定）。"""
    try:
        a = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        raise ValueError("请输入有效的 IP 地址（不支持域名）")
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    s = str(a)
    if s in {str(ipaddress.ip_address(x.strip())) for x in self_ips if x.strip()}:
        raise ValueError("不允许指向控制端自身")
    if s in ("169.254.169.254", "100.100.100.200", "fd00:ec2::254"):
        raise ValueError("不允许访问云元数据地址")
    if a.is_multicast:
        raise ValueError("不允许访问组播地址")
    if not allow_private and not a.is_global:
        raise ValueError("不允许访问内网、回环、链路本地或保留地址")
    return s


class LoginLimiter:
    def __init__(self, max_fail=5, window=300, clock=None):
        self.max_fail, self.window, self.clock = max_fail, window, clock or SystemClock()
        self.fails, self.lock = {}, threading.Lock()

    def _recent(self, key):
        now = self.clock.now()
        v = [t for t in self.fails.get(key, []) if now - t < self.window]
        self.fails[key] = v
        return v

    def blocked(self, key):
        with self.lock:
            return len(self._recent(key)) >= self.max_fail

    def fail(self, key):
        with self.lock:
            self._recent(key).append(self.clock.now())

    def ok(self, key):
        with self.lock:
            self.fails.pop(key, None)


def _make_fernet(secret):
    """用 SECRET_KEY 派生加密密钥，用于加密保存的 root 密码（Fernet：AES-128-CBC + HMAC）。"""
    if not secret:
        return None
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("hbm-v2:" + secret).encode()).digest()))


class Store:
    def __init__(self, path, clock=None, secret=None):
        self._fernet = _make_fernet(secret)
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self._migrate()

    def close(self):
        with self.lock:
            self.db.close()

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def _q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def _x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # ---- 管理员 / 会话 ----
    def has_admin(self):
        return bool(self._q("SELECT 1 FROM admin"))

    def set_admin(self, username, password):
        check_admin_password(password)
        self._x("INSERT OR REPLACE INTO admin(id,username,pw_hash) VALUES(1,?,?)", (username, hash_password(password)))

    def verify_login(self, username, password):
        r = self._q("SELECT username,pw_hash FROM admin WHERE id=1")
        ok = verify_password(password, r[0]["pw_hash"] if r else "scrypt$00$00")
        return bool(r) and ok and hmac.compare_digest(username, r[0]["username"])

    def create_session(self):
        tok = secrets.token_urlsafe(32)
        self._x("INSERT INTO sessions VALUES(?,?)", (_h(tok), self.clock.now() + SESSION_TTL))
        return tok

    def check_session(self, tok):
        if not tok:
            return False
        r = self._q("SELECT expires FROM sessions WHERE token_hash=?", (_h(tok),))
        return bool(r) and r[0]["expires"] > self.clock.now()

    def delete_session(self, tok):
        if tok:
            self._x("DELETE FROM sessions WHERE token_hash=?", (_h(tok),))

    def audit(self, action, detail=""):
        self._x("INSERT INTO audit(ts,action,detail) VALUES(?,?,?)", (self.clock.now(), action, detail[:200]))

    # ---- 节点 ----
    def _migrate(self):
        """幂等迁移到 v2：新增厂商、加密密码字段和厂商历史表，旧数据保留。"""
        cols = {r["name"] for r in self._q("PRAGMA table_info(nodes)")}
        with self.tx():
            if "vendor" not in cols:
                self.db.execute("ALTER TABLE nodes ADD COLUMN vendor TEXT NOT NULL DEFAULT ''")
            if "root_password" not in cols:
                self.db.execute("ALTER TABLE nodes ADD COLUMN root_password TEXT")
            self.db.execute("CREATE TABLE IF NOT EXISTS vendors(name TEXT PRIMARY KEY, last_used REAL NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO vendors(name,last_used) SELECT vendor, MAX(created_at) FROM nodes WHERE vendor<>'' GROUP BY vendor")
            self.db.execute("PRAGMA user_version=2")

    def add_node(self, name, ip, port, note="", vendor=""):
        now = self.clock.now()
        c = self._x("INSERT INTO nodes(name,ip,ssh_port,note,vendor,status,status_changed,created_at) VALUES(?,?,?,?,?, 'installing',?,?)",
                    (name, ip, port, note, vendor, now, now))
        self.touch_vendor(vendor)
        return c.lastrowid

    # ---- 厂商历史 / 保存的密码 ----
    def touch_vendor(self, vendor):
        if vendor:
            self._x("INSERT INTO vendors(name,last_used) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET last_used=excluded.last_used", (vendor, self.clock.now()))

    def list_vendors(self):
        return [r["name"] for r in self._q("SELECT name FROM vendors ORDER BY last_used DESC, name")]

    def set_password(self, node_id, pw):
        """pw 为空表示清除。密码加密后保存，需配置 SECRET_KEY。"""
        if pw:
            if not self._fernet:
                raise RuntimeError("未配置 SECRET_KEY，无法保存密码")
            val = self._fernet.encrypt(pw.encode()).decode()
        else:
            val = None
        self._x("UPDATE nodes SET root_password=? WHERE id=?", (val, node_id))

    def get_password(self, node_id):
        r = self._q("SELECT root_password FROM nodes WHERE id=?", (node_id,))
        if not r or not r[0][0] or not self._fernet:
            return None
        try:
            return self._fernet.decrypt(r[0][0].encode()).decode()
        except Exception:      # SECRET_KEY 被更换等
            return None

    def passwords_for(self, ids):
        out = {}
        for i in ids:
            p = self.get_password(i)
            if p is not None:
                out[i] = p
        return out

    def rotate_token(self, node_id):
        tok = secrets.token_urlsafe(32)
        self._x("UPDATE nodes SET token_hash=? WHERE id=?", (_h(tok), node_id))
        return tok

    def clear_token(self, node_id):
        self._x("UPDATE nodes SET token_hash=NULL WHERE id=?", (node_id,))

    def get_node(self, node_id):
        r = self._q("SELECT id,name,ip,ssh_port,note,vendor,(root_password IS NOT NULL) AS has_password,status,last_heartbeat,status_changed,created_at,host_fingerprint,install_error FROM nodes WHERE id=?", (node_id,))
        return dict(r[0]) if r else None

    def find_by_ip(self, ip):
        r = self._q("SELECT id FROM nodes WHERE ip=?", (ip,))
        return r[0]["id"] if r else None

    def update_node(self, node_id, name=None, note=None, vendor=None):
        n = self.get_node(node_id)
        if not n:
            return False
        v = vendor if vendor is not None else n["vendor"]
        self._x("UPDATE nodes SET name=?, note=?, vendor=? WHERE id=?",
                (name if name is not None else n["name"], note if note is not None else n["note"], v, node_id))
        self.touch_vendor(v)
        return True

    def set_status(self, node_id, status, error=None):
        now = self.clock.now()
        self._x("UPDATE nodes SET status_changed=CASE WHEN status<>? THEN ? ELSE status_changed END, status=?, install_error=? WHERE id=?",
                (status, now, status, error, node_id))

    def set_fingerprint(self, node_id, fp):
        self._x("UPDATE nodes SET host_fingerprint=? WHERE id=?", (fp, node_id))

    def delete_node(self, node_id):
        """只删除节点；故障历史默认保留（未恢复事件标记为 node_deleted）。"""
        with self.tx():
            self.db.execute("UPDATE events SET status='node_deleted' WHERE node_id=? AND status='open'", (node_id,))
            return self.db.execute("DELETE FROM nodes WHERE id=?", (node_id,)).rowcount > 0

    # ---- 心跳 ----
    def heartbeat(self, node_id, token):
        try:
            node_id = int(node_id)
        except (TypeError, ValueError):
            return False
        now = self.clock.now()
        with self.tx():
            r = self.db.execute("SELECT token_hash,status FROM nodes WHERE id=?", (node_id,)).fetchone()
            expected = (r["token_hash"] if r else None) or "0" * 64
            if not (hmac.compare_digest(_h(token or ""), expected) and r is not None and r["token_hash"]):
                return False
            old = r["status"]
            if old in ("install_failed", "online"):   # 安装失败节点不会因心跳变成在线
                self.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (now, node_id))
            else:
                self.db.execute("UPDATE nodes SET last_heartbeat=?, status='online', status_changed=?, install_error=NULL WHERE id=?", (now, now, node_id))
                ev = self.db.execute("SELECT id,last_heartbeat FROM events WHERE node_id=? AND status='open'", (node_id,)).fetchone()
                if ev:
                    base = ev["last_heartbeat"] if ev["last_heartbeat"] is not None else now
                    self.db.execute("UPDATE events SET recovered_at=?, duration=?, status='recovered' WHERE id=?", (now, now - base, ev["id"]))
            return True

    def sweep(self):
        """统一扫描超时节点；也用于控制端重启后重新计算状态。"""
        now = self.clock.now()
        marked = []
        with self.tx():
            rows = self.db.execute("SELECT id,name,ip,last_heartbeat FROM nodes WHERE status='online' AND last_heartbeat < ?", (now - OFFLINE_AFTER,)).fetchall()
            for r in rows:
                self.db.execute("UPDATE nodes SET status='offline', status_changed=? WHERE id=?", (now, r["id"]))
                self.db.execute("INSERT OR IGNORE INTO events(node_id,node_name,node_ip,offline_at,last_heartbeat,status) VALUES(?,?,?,?,?, 'open')",
                                (r["id"], r["name"], r["ip"], now, r["last_heartbeat"]))
                marked.append(r["id"])
            self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        return marked

    def list_nodes(self, q="", status="", page=1, size=20, vendor=""):
        now, where, args = self.clock.now(), [], []
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(name LIKE ? ESCAPE '\\' OR ip LIKE ? ESCAPE '\\')")
            args += [f"%{esc}%"] * 2
        if status in STATUSES:
            where.append("status=?")
            args.append(status)
        if vendor:
            where.append("vendor=?")
            args.append(vendor)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = self._q(f"SELECT COUNT(*) c FROM nodes {w}", args)[0]["c"]
        rows = self._q(f"""SELECT id,name,ip,ssh_port,note,vendor,(root_password IS NOT NULL) AS has_password,status,last_heartbeat,status_changed,created_at,install_error FROM nodes {w}
            ORDER BY CASE status WHEN 'offline' THEN 0 WHEN 'install_failed' THEN 1 WHEN 'installing' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END, name
            LIMIT ? OFFSET ?""", args + [size, max(page - 1, 0) * size])
        items = []
        for r in rows:
            d = dict(r)
            d["offline_seconds"] = int(now - (d["last_heartbeat"] or d["status_changed"])) if d["status"] == "offline" else None
            items.append(d)
        return items, total

    def counts(self):
        c = {s: 0 for s in STATUSES}
        for r in self._q("SELECT status, COUNT(*) c FROM nodes GROUP BY status"):
            c[r["status"]] = r["c"]
        c["total"] = sum(c[s] for s in STATUSES)
        return c

    # ---- 故障历史 ----
    def list_events(self, node_id=None, start=None, end=None, page=1, size=20):
        now, where, args = self.clock.now(), [], []
        for col, op, v in (("node_id", "=", node_id), ("offline_at", ">=", start), ("offline_at", "<=", end)):
            if v is not None:
                where.append(f"{col}{op}?")
                args.append(v)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = self._q(f"SELECT COUNT(*) c FROM events {w}", args)[0]["c"]
        rows = self._q(f"SELECT * FROM events {w} ORDER BY offline_at DESC, id DESC LIMIT ? OFFSET ?", args + [size, max(page - 1, 0) * size])
        out = []
        for r in rows:
            d = dict(r)
            if d["status"] == "open":
                d["duration"] = now - (d["last_heartbeat"] or d["offline_at"])
            out.append(d)
        return out, total

    def delete_events(self, ids, confirmed=False):
        if not confirmed:
            raise PermissionError("删除历史记录需要二次确认")
        ids = [int(i) for i in ids][:1000]
        if not ids:
            return 0
        with self.tx():
            n = self.db.execute(f"DELETE FROM events WHERE status<>'open' AND id IN ({','.join('?' * len(ids))})", ids).rowcount
        self.audit("events_delete", f"count={n}")
        return n

    def clear_events(self, before, confirmed=False):
        if not confirmed:
            raise PermissionError("清理历史记录需要二次确认")
        n = self._x("DELETE FROM events WHERE status<>'open' AND offline_at < ?", (before,)).rowcount
        self.audit("events_clear", f"count={n}")
        return n

    # ---- 安装任务 ----
    def create_job(self, node_id):
        return self._x("INSERT INTO install_jobs(node_id,step,status,updated_at) VALUES(?, 'waiting','running',?)", (node_id, self.clock.now())).lastrowid

    def set_job(self, job_id, step, status="running", error=None):
        self._x("UPDATE install_jobs SET step=?, status=?, error=?, updated_at=? WHERE id=?", (step, status, error, self.clock.now(), job_id))

    def get_job(self, job_id):
        r = self._q("SELECT id,node_id,step,status,error FROM install_jobs WHERE id=?", (job_id,))
        return dict(r[0]) if r else None
