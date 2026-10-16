import logging, os, tempfile, time, unittest, io
from app import core, installer
from app.core import FakeClock, Store

PW = "Sup3r-Secret-Pw-XYZ"


class AuthenticationException(Exception):   # 名称与 paramiko 一致，用于测试错误映射
    pass


class Fake:
    mode, uname, store, hb = "ok", "Linux x86_64", None, True

    def __init__(self, host, port, password, known=None):
        self.known, self.files, self.cmds = known, {}, []

    def connect(self):
        if Fake.mode == "auth": raise AuthenticationException("bad auth")
        if Fake.mode == "timeout": raise TimeoutError()
        if Fake.mode == "fp" and self.known: raise installer.InstallError("SSH 主机指纹与首次记录不一致")
        return "SHA256:fakefp"

    def run(self, cmd, timeout=60):
        self.cmds.append(cmd)
        if cmd == "uname -sm": return 0, Fake.uname + "\n"
        if cmd.startswith("test -d"): return 0, ""
        if cmd == installer.START_CMD and Fake.hb:
            tok = [l for l in self.files[installer.ENV].decode().split("\n") if l.startswith("HBM_TOKEN=")][0][10:]
            Fake.store.heartbeat(1, tok)
        return 0, ""

    def write(self, path, data, mode): self.files[path] = data
    def close(self): pass


class T(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(); self.path = os.path.join(d, "t.db")
        self.s = Store(self.path, FakeClock()); Fake.store, Fake.mode, Fake.uname, Fake.hb = self.s, "ok", "Linux x86_64", True
        for a in ("amd64", "arm64"): open(os.path.join(d, f"hbm-probe-linux-{a}"), "wb").write(b"ELF")
        self.cfg = {"public_url": "https://hbm.example.com", "probe_dir": d, "wait_seconds": 0.3}
        self.buf = io.StringIO(); self.h = logging.StreamHandler(self.buf); logging.getLogger().addHandler(self.h); logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self): logging.getLogger().removeHandler(self.h)

    def run_install(self, mode="install"):
        nid = self.s.add_node("n", "8.8.8.8", 22); tok = self.s.rotate_token(nid); job = self.s.create_job(nid)
        installer.run_job(self.s, job, nid, "8.8.8.8", 22, PW, tok, self.cfg, mode, Fake, sleep=lambda x: time.sleep(0.02))
        return nid, self.s.get_job(job)

    def test_success(self):
        nid, j = self.run_install(); self.assertEqual(j["status"], "done"); self.assertEqual(self.s.get_node(nid)["status"], "online")
        self.assertEqual(self.s.get_node(nid)["host_fingerprint"], "SHA256:fakefp")

    def test_auth_failure_message(self):
        Fake.mode = "auth"; nid, j = self.run_install()
        self.assertEqual(j["status"], "failed"); self.assertIn("认证失败", j["error"]); self.assertEqual(self.s.get_node(nid)["status"], "install_failed")

    def test_timeout_message(self):
        Fake.mode = "timeout"; _, j = self.run_install(); self.assertIn("超时", j["error"])

    def test_unsupported_arch_and_os(self):
        Fake.uname = "Linux armv7l"; nid, j = self.run_install(); self.assertIn("不支持的 CPU 架构", j["error"]); self.assertEqual(self.s.get_node(nid)["status"], "install_failed")
        Fake.uname = "FreeBSD amd64"; _, j = self.run_install(); self.assertIn("仅支持 Linux", j["error"])

    def test_no_first_heartbeat_is_failure(self):
        Fake.hb = False; nid, j = self.run_install(); self.assertIn("未在限定时间内收到首次心跳", j["error"]); self.assertEqual(self.s.get_node(nid)["status"], "install_failed")

    def test_fingerprint_mismatch(self):
        Fake.mode = "fp"; nid = self.s.add_node("n", "8.8.8.8", 22); self.s.set_fingerprint(nid, "SHA256:old"); job = self.s.create_job(nid)
        installer.run_job(self.s, job, nid, "8.8.8.8", 22, PW, "t", self.cfg, "install", Fake)
        self.assertIn("指纹", self.s.get_job(job)["error"])

    def test_password_never_stored_or_logged(self):
        self.run_install(); Fake.mode = "auth"; self.run_install(); Fake.mode = "timeout"; self.run_install()
        self.s.audit("x"); self.s.close()
        blob = b"".join(open(self.path + sfx, "rb").read() for sfx in ("", "-wal", "-shm") if os.path.exists(self.path + sfx))
        self.assertNotIn(PW.encode(), blob); self.assertNotIn(PW, self.buf.getvalue())

    def test_uninstall(self):
        nid, j = self.run_install(); self.assertEqual(j["status"], "done")
        job = self.s.create_job(nid); installer.run_job(self.s, job, nid, "8.8.8.8", 22, PW, "", self.cfg, "uninstall", Fake)
        self.assertEqual(self.s.get_node(nid)["status"], "pending")
        # 卸载后 token 已清空，旧探针心跳被拒
        self.assertFalse(self.s.heartbeat(nid, "anything"))


if __name__ == "__main__":
    unittest.main()
