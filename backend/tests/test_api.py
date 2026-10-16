"""API 测试（需要 fastapi + httpx；沙箱未安装时自动跳过，CI 中会运行）。"""
import json, os, tempfile, time, unittest
try:
    from fastapi.testclient import TestClient
    from app.main import create_app
    HAS = True
except Exception:
    HAS = False
from tests.test_installer import Fake, PW

H = {"X-Requested-With": "hbm"}
ORIGIN = {"origin": "https://hbm.example.com"}


class FakeShell:
    last = None

    def __init__(self, host, port, pw, known=None):
        self.pw, self.out, self.sent, self.closed = pw, [b"hello\r\n"], [], False
        FakeShell.last = self

    def connect(self): return "SHA256:fake"
    def open_shell(self, cols, rows): self.size = (cols, rows)

    def read(self):
        if self.closed: return None
        if self.out: return self.out.pop(0)
        time.sleep(0.02); return b""

    def send(self, d): self.sent.append(d); self.out.append(b"echo:" + d)
    def resize(self, c, r): self.size = (c, r)
    def close(self): self.closed = True
ADMIN = "S3cure-Passw0rd-xyz"


@unittest.skipUnless(HAS, "需要 fastapi/httpx")
class API(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "hbm-probe-linux-amd64"), "wb").close()
        cfg = dict(db_path=os.path.join(d, "t.db"), public_url="https://hbm.example.com", cookie_secure=False, admin_user="admin", admin_password=ADMIN,
                   probe_dir=d, allow_private=False, allow_insecure=False, self_ips=["8.8.4.4"], concurrency=2, wait_seconds=2, cors=[], static_dir="/nonexistent", secret_key="k" * 40)
        self.app = create_app(cfg, ssh_factory=Fake, start_sweeper=False, shell_factory=FakeShell)
        Fake.store, Fake.mode, Fake.uname, Fake.hb = self.app.state.store, "ok", "Linux x86_64", True
        self.c = TestClient(self.app)

    def login(self):
        r = self.c.post("/api/auth/login", json={"username": "admin", "password": ADMIN}, headers=H); self.assertEqual(r.status_code, 200)

    def test_auth(self):
        self.assertEqual(self.c.get("/api/nodes").status_code, 401)
        self.assertEqual(self.c.post("/api/nodes", json={}, headers=H).status_code, 401)
        self.assertEqual(self.c.post("/api/auth/login", json={"username": "admin", "password": "bad"}, headers=H).status_code, 401)
        self.login(); self.assertEqual(self.c.get("/api/nodes").status_code, 200)

    def test_csrf_header_required(self):
        self.login(); self.assertEqual(self.c.post("/api/events/delete", json={"ids": []}).status_code, 403)

    def test_login_rate_limit(self):
        codes = [self.c.post("/api/auth/login", json={"username": "admin", "password": "x"}, headers=H).status_code for _ in range(6)]
        self.assertEqual(codes[-1], 429)

    def test_heartbeat_endpoint(self):
        s = self.app.state.store; nid = s.add_node("a", "8.8.8.8", 22); tok = s.rotate_token(nid)
        self.assertEqual(self.c.post("/api/heartbeat", json={"node_id": nid}, headers={"Authorization": "Bearer " + tok}).status_code, 200)
        r = self.c.post("/api/heartbeat", json={"node_id": nid}, headers={"Authorization": "Bearer wrong"}); self.assertEqual(r.status_code, 401); self.assertNotIn(tok, r.text)
        self.assertEqual(self.c.post("/api/heartbeat", json={"node_id": 999}, headers={"Authorization": "Bearer " + tok}).status_code, 401)

    def test_ssrf_and_validation_do_not_echo_password(self):
        self.login()
        for ip in ("127.0.0.1", "169.254.169.254", "8.8.4.4", "10.1.1.1"):
            self.assertEqual(self.c.post("/api/nodes", json={"name": "x", "ip": ip, "password": PW}, headers=H).status_code, 400, ip)
        r = self.c.post("/api/nodes", json={"name": "x", "ip": "8.8.8.8", "ssh_port": 0, "password": PW}, headers=H)
        self.assertEqual(r.status_code, 422); self.assertNotIn(PW, r.text)

    def test_install_flow(self):
        self.login()
        r = self.c.post("/api/nodes", json={"name": "vps1", "ip": "8.8.8.8", "password": PW}, headers=H).json()
        nid = r["node_id"]
        for _ in range(100):
            j = self.c.get(f"/api/jobs/{r['job_id']}").json()
            if j["status"] != "running": break
            time.sleep(0.05)
        self.assertEqual(j["status"], "done"); self.assertEqual(self.c.get(f"/api/nodes/{nid}").json()["status"], "online")
        self.assertEqual(self.c.delete(f"/api/nodes/{nid}").status_code, 400)           # 需二次确认
        self.assertEqual(self.c.delete(f"/api/nodes/{nid}?confirm=true", headers=H).status_code, 200)

    def test_history_clear_needs_confirmation(self):
        self.login()
        self.assertEqual(self.c.post("/api/events/clear", json={"before": 9e12}, headers=H).status_code, 400)
        self.assertEqual(self.c.post("/api/events/delete", json={"ids": [1]}, headers=H).status_code, 400)
        self.assertEqual(self.c.post("/api/events/clear", json={"before": 9e12, "confirm_text": "清理历史"}, headers=H).status_code, 200)

    def test_weak_admin_refuses_to_start(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            create_app(dict(db_path=os.path.join(d, "x.db"), public_url="https://a.b", cookie_secure=True, admin_user="a", admin_password="admin", probe_dir=d,
                            allow_private=False, allow_insecure=False, self_ips=[], concurrency=1, wait_seconds=1, cors=[], static_dir="/x", secret_key="k" * 40), start_sweeper=False)


    # ---- v2 ----
    def add(self, **kw):
        body = {"name": "vps1", "ip": "8.8.8.8", "password": PW, "vendor": "Linode"}; body.update(kw)
        r = self.c.post("/api/nodes", json=body, headers=H).json()
        for _ in range(100):
            if self.c.get(f"/api/jobs/{r['job_id']}").json()["status"] != "running": break
            time.sleep(0.05)
        return r["node_id"]

    def test_saved_password_vendor_and_copy_endpoints(self):
        self.login(); nid = self.add()
        n = self.c.get("/api/nodes").json()["items"][0]
        self.assertEqual((n["vendor"], n["has_password"]), ("Linode", 1)); self.assertNotIn(PW, json.dumps(n))     # 轮询不带密码
        self.assertEqual(self.c.get(f"/api/nodes/{nid}/password").json()["password"], PW)
        self.assertEqual(self.c.get(f"/api/passwords?ids={nid},999").json(), {str(nid): PW})
        self.assertEqual(self.c.get("/api/vendors").json()["items"], ["Linode"])
        self.assertEqual(self.c.patch(f"/api/nodes/{nid}", json={"vendor": "阿里云", "password": "New-Pw-456789"}, headers=H).status_code, 200)
        self.assertEqual(self.c.get(f"/api/nodes/{nid}/password").json()["password"], "New-Pw-456789")
        self.assertEqual(self.c.get("/api/vendors").json()["items"][0], "阿里云")

    def test_not_saving_password_and_reinstall_uses_saved(self):
        self.login(); nid = self.add(save_password=False)
        self.assertEqual(self.c.get(f"/api/nodes/{nid}/password").status_code, 404)
        self.assertEqual(self.c.post(f"/api/nodes/{nid}/reinstall", json={}, headers=H).status_code, 400)
        self.c.patch(f"/api/nodes/{nid}", json={"password": PW}, headers=H)
        self.assertEqual(self.c.post(f"/api/nodes/{nid}/reinstall", json={}, headers=H).status_code, 200)

    def ws_ready(self, ws):
        msgs = []
        while True:
            m = ws.receive_json(); msgs.append(m["type"])
            if m["type"] in ("ready", "error", "need_password"): return m, msgs

    def test_terminal_flow(self):
        self.login(); nid = self.add()
        with self.c.websocket_connect(f"/api/nodes/{nid}/terminal", headers=ORIGIN) as ws:
            ws.send_text(json.dumps({"type": "init", "cols": 100, "rows": 30}))
            m, _ = self.ws_ready(ws); self.assertEqual(m["type"], "ready")
            self.assertEqual(ws.receive_bytes(), b"hello\r\n")
            ws.send_bytes(b"ls"); self.assertEqual(ws.receive_bytes(), b"echo:ls")
            ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        self.assertEqual(FakeShell.last.pw, PW)

    def test_terminal_asks_password_when_not_saved(self):
        self.login(); nid = self.add(save_password=False)
        with self.c.websocket_connect(f"/api/nodes/{nid}/terminal", headers=ORIGIN) as ws:
            ws.send_text(json.dumps({"type": "init", "cols": 80, "rows": 24}))
            m, _ = self.ws_ready(ws); self.assertEqual(m["type"], "need_password")
            ws.send_text(json.dumps({"type": "password", "password": "typed-pw-1"}))
            m, _ = self.ws_ready(ws); self.assertEqual(m["type"], "ready")
        self.assertEqual(FakeShell.last.pw, "typed-pw-1")

    def test_terminal_rejects_bad_origin_unauth_and_offline(self):
        from starlette.websockets import WebSocketDisconnect
        self.login(); nid = self.add()
        with self.assertRaises(WebSocketDisconnect):
            with self.c.websocket_connect(f"/api/nodes/{nid}/terminal", headers={"origin": "https://evil.example"}): pass
        self.c.cookies.clear()
        with self.assertRaises(WebSocketDisconnect):
            with self.c.websocket_connect(f"/api/nodes/{nid}/terminal", headers=ORIGIN): pass
        self.login(); self.app.state.store.set_status(nid, "offline")
        with self.c.websocket_connect(f"/api/nodes/{nid}/terminal", headers=ORIGIN) as ws:
            self.assertEqual(ws.receive_json()["type"], "error")
