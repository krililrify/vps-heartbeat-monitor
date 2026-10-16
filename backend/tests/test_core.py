import logging, os, sqlite3, tempfile, threading, time, unittest
from app import core
from app.core import FakeClock, Store, OFFLINE_AFTER


def mk(path=None):
    d = tempfile.mkdtemp()
    c = FakeClock()
    return Store(path or os.path.join(d, "t.db"), c), c, d


def node(s, name="n1", ip="8.8.8.8"):
    nid = s.add_node(name, ip, 22, "")
    return nid, s.rotate_token(nid)


class Heartbeat(unittest.TestCase):
    def test_valid_token_updates(self):
        s, c, _ = mk(); nid, tok = node(s)
        self.assertEqual(s.get_node(nid)["status"], "installing")
        self.assertTrue(s.heartbeat(nid, tok))
        n = s.get_node(nid)
        self.assertEqual((n["status"], n["last_heartbeat"]), ("online", c.now()))

    def test_invalid_token_rejected(self):
        s, c, _ = mk(); nid, tok = node(s)
        self.assertFalse(s.heartbeat(nid, "bad"))
        self.assertFalse(s.heartbeat(9999, tok))
        self.assertFalse(s.heartbeat("x", tok))
        self.assertIsNone(s.get_node(nid)["last_heartbeat"])
        self.assertEqual(s.get_node(nid)["status"], "installing")

    def test_token_stored_hashed(self):
        s, c, _ = mk(); nid, tok = node(s)
        row = s._q("SELECT token_hash FROM nodes")[0][0]
        self.assertNotEqual(row, tok); self.assertEqual(len(row), 64)

    def test_install_failed_not_online(self):
        s, c, _ = mk(); nid, tok = node(s); s.set_status(nid, "install_failed", "x")
        self.assertTrue(s.heartbeat(nid, tok))
        self.assertEqual(s.get_node(nid)["status"], "install_failed")


class Offline(unittest.TestCase):
    def setUp(self):
        self.s, self.c, self.d = mk(); self.nid, self.tok = node(self.s); self.s.heartbeat(self.nid, self.tok)

    def test_threshold_boundary(self):
        self.c.advance(90); self.assertEqual(self.s.sweep(), [])
        self.c.advance(0.1); self.assertEqual(self.s.sweep(), [self.nid])
        self.assertEqual(self.s.get_node(self.nid)["status"], "offline")

    def test_one_event_per_outage_and_recovery_updates_it(self):
        self.c.advance(91); self.s.sweep(); self.c.advance(60); self.s.sweep(); self.s.sweep()
        ev, total = self.s.list_events(); self.assertEqual(total, 1); self.assertEqual(ev[0]["status"], "open")
        self.c.advance(10); self.s.heartbeat(self.nid, self.tok)
        ev, total = self.s.list_events()
        self.assertEqual(total, 1)                       # 恢复不生成新事件
        self.assertEqual(ev[0]["status"], "recovered"); self.assertEqual(ev[0]["duration"], 161); self.assertIsNotNone(ev[0]["recovered_at"])
        self.assertEqual(self.s.get_node(self.nid)["status"], "online")

    def test_second_outage_new_event(self):
        for _ in range(2):
            self.c.advance(100); self.s.sweep(); self.c.advance(5); self.s.heartbeat(self.nid, self.tok)
        self.assertEqual(self.s.list_events()[1], 2)

    def test_restart_recomputes_status(self):
        path = os.path.join(self.d, "t.db"); self.s.close()
        self.c.advance(500)
        s2 = Store(path, self.c)
        self.assertEqual(s2.get_node(self.nid)["status"], "online")   # 库里还是旧值
        s2.sweep()                                                    # 启动时重算
        self.assertEqual(s2.get_node(self.nid)["status"], "offline"); self.assertEqual(s2.list_events()[1], 1)

    def test_pending_never_offline(self):
        nid = self.s.add_node("p", "1.1.1.1", 22); self.s.set_status(nid, "pending")
        self.c.advance(9999); self.s.sweep(); self.assertEqual(self.s.get_node(nid)["status"], "pending")

    def test_history_kept_automatically_and_on_node_delete(self):
        self.c.advance(100); self.s.sweep(); self.c.advance(365 * 86400); self.s.sweep()
        self.assertEqual(self.s.list_events()[1], 1)
        self.s.delete_node(self.nid)
        ev, total = self.s.list_events(); self.assertEqual(total, 1); self.assertEqual(ev[0]["status"], "node_deleted")

    def test_clear_requires_confirmation(self):
        self.c.advance(100); self.s.sweep(); self.s.heartbeat(self.nid, self.tok)
        eid = self.s.list_events()[0][0]["id"]
        with self.assertRaises(PermissionError): self.s.delete_events([eid])
        with self.assertRaises(PermissionError): self.s.clear_events(self.c.now() + 1)
        self.assertEqual(self.s.list_events()[1], 1)
        self.assertEqual(self.s.clear_events(self.c.now() + 1, confirmed=True), 1)

    def test_open_event_not_deletable(self):
        self.c.advance(100); self.s.sweep(); eid = self.s.list_events()[0][0]["id"]
        self.assertEqual(self.s.delete_events([eid], True), 0)


class Auth(unittest.TestCase):
    def test_login_and_sessions(self):
        s, c, _ = mk(); s.set_admin("root1", "Correct-Horse-Battery")
        self.assertTrue(s.verify_login("root1", "Correct-Horse-Battery"))
        self.assertFalse(s.verify_login("root1", "wrong")); self.assertFalse(s.verify_login("x", "Correct-Horse-Battery"))
        self.assertNotIn("Correct", s._q("SELECT pw_hash FROM admin")[0][0])
        t = s.create_session(); self.assertTrue(s.check_session(t)); self.assertFalse(s.check_session("nope"))
        c.advance(core.SESSION_TTL + 1); self.assertFalse(s.check_session(t))

    def test_weak_admin_password_rejected(self):
        s, _, _ = mk()
        for pw in ("short", "password", "changeme"):
            with self.assertRaises(ValueError): s.set_admin("a", pw)

    def test_rate_limit(self):
        c = FakeClock(); l = core.LoginLimiter(clock=c)
        for _ in range(5): l.fail("ip")
        self.assertTrue(l.blocked("ip")); c.advance(301); self.assertFalse(l.blocked("ip"))


class SSRF(unittest.TestCase):
    def test_blocked(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "::ffff:10.0.0.1", "localhost", "evil.com", "fe80::1", "224.0.0.1"):
            with self.assertRaises(ValueError, msg=ip): core.check_target(ip)
        with self.assertRaises(ValueError): core.check_target("8.8.4.4", self_ips=["8.8.4.4"])
        with self.assertRaises(ValueError): core.check_target("169.254.169.254", allow_private=True)

    def test_allowed(self):
        self.assertEqual(core.check_target(" 8.8.8.8 "), "8.8.8.8"); self.assertEqual(core.check_target("1.1.1.1"), "1.1.1.1")


class Load(unittest.TestCase):
    def test_500_nodes_concurrent(self):
        s, c, _ = mk(); ids = [node(s, f"n{i}", f"8.8.{i // 250}.{i % 250 + 1}") for i in range(500)]
        def worker(chunk):
            for _ in range(3):
                for nid, tok in chunk: assert s.heartbeat(nid, tok)
        t0 = time.time(); ts = [threading.Thread(target=worker, args=(ids[i::8],)) for i in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]; el = time.time() - t0
        print(f"\n[load] 1500 heartbeats in {el:.2f}s = {1500 / el:.0f}/s (需求≈17/s)")
        t1 = time.time(); s.sweep(); sw = time.time() - t1
        print(f"[load] sweep over 500 nodes: {sw * 1000:.1f}ms")
        self.assertEqual(s.counts()["online"], 500); self.assertLess(el, 10); self.assertLess(sw, 0.5)
        c.advance(100); self.assertEqual(len(s.sweep()), 500); self.assertEqual(s.list_events()[1], 500)


class V2(unittest.TestCase):
    KEY = "k" * 40

    def mks(self, secret=None):
        d = tempfile.mkdtemp(); c = FakeClock(); path = os.path.join(d, "t.db")
        return Store(path, c, secret or self.KEY), c, path

    def test_password_encrypted_roundtrip(self):
        s, c, path = self.mks(); nid = s.add_node("a", "8.8.8.8", 22, "", "阿里云")
        s.set_password(nid, "My-Root-Pw-123"); self.assertEqual(s.get_password(nid), "My-Root-Pw-123")
        raw = s._q("SELECT root_password FROM nodes")[0][0]; self.assertNotIn("My-Root-Pw", raw)
        s.close(); blob = b"".join(open(path + x, "rb").read() for x in ("", "-wal") if os.path.exists(path + x))
        self.assertNotIn(b"My-Root-Pw-123", blob)

    def test_wrong_key_cannot_decrypt_and_missing_key_cannot_save(self):
        s, c, path = self.mks(); nid = s.add_node("a", "8.8.8.8", 22); s.set_password(nid, "pw-123456789"); s.close()
        s2 = Store(path, c, "z" * 40); self.assertIsNone(s2.get_password(nid)); self.assertEqual(s2.get_node(nid)["has_password"], 1)
        s3 = Store(path, c)
        with self.assertRaises(RuntimeError): s3.set_password(nid, "x")

    def test_clear_password_and_no_leak_in_listing(self):
        s, c, _ = self.mks(); nid = s.add_node("a", "8.8.8.8", 22); s.set_password(nid, "pw-123456789")
        items, _ = s.list_nodes(); self.assertNotIn("root_password", items[0]); self.assertNotIn("pw-123456789", str(items))
        self.assertEqual(s.passwords_for([nid, 999]), {nid: "pw-123456789"})
        s.set_password(nid, ""); self.assertIsNone(s.get_password(nid)); self.assertEqual(s.get_node(nid)["has_password"], 0)

    def test_vendor_history_survives_node_delete_and_orders_by_recent(self):
        s, c, _ = self.mks(); a = s.add_node("a", "8.8.8.8", 22, "", "阿里云"); c.advance(10); s.add_node("b", "1.1.1.1", 22, "", "Linode")
        self.assertEqual(s.list_vendors(), ["Linode", "阿里云"]); s.delete_node(a)
        c.advance(10); s.update_node(a + 1, vendor="阿里云")
        self.assertEqual(s.list_vendors(), ["阿里云", "Linode"])
        self.assertEqual(s.list_nodes(vendor="阿里云")[1], 1); self.assertEqual(s.list_nodes(vendor="腾讯云")[1], 0)

    def test_migration_from_v1_keeps_data(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "old.db"); db = sqlite3.connect(path)
        db.executescript(core.SCHEMA.replace("CREATE TABLE IF NOT EXISTS vendors", "--"))
        db.execute("INSERT INTO nodes(name,ip,ssh_port,note,status,status_changed,created_at,token_hash,last_heartbeat) VALUES('测试1','154.88.64.196',22,'WPC1','online',1,1,'h',5)")
        db.commit(); db.close()
        s = Store(path, FakeClock(), self.KEY); n = s.get_node(1)
        self.assertEqual((n["name"], n["note"], n["vendor"], n["has_password"]), ("测试1", "WPC1", "", 0))
        s.close(); Store(path, FakeClock(), self.KEY).close()      # 重复迁移幂等


if __name__ == "__main__":
    unittest.main()
