"""FastAPI 入口。启动：uvicorn --factory app.main:create_app"""
import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from . import core
from .installer import InstallManager, friendly
from .terminal import ParamikoShell

log = logging.getLogger("hbm")
COOKIE = "hbm_session"
MAX_TERMINALS = 10
TERMINAL_IDLE = 1800   # 秒，空闲超时自动断开


def load_config(env=os.environ):
    return dict(
        db_path=env.get("DB_PATH", "/data/hbm.db"),
        public_url=env.get("PUBLIC_URL", "").rstrip("/"),
        secret_key=env.get("SECRET_KEY", ""),
        cookie_secure=env.get("COOKIE_SECURE", "1") != "0",
        admin_user=env.get("ADMIN_USERNAME", ""),
        admin_password=env.get("ADMIN_PASSWORD", ""),
        probe_dir=env.get("PROBE_DIR", "/app/probes"),
        allow_private=env.get("ALLOW_PRIVATE_TARGETS", "0") == "1",
        allow_insecure=env.get("ALLOW_INSECURE_HTTP", "0") == "1",
        self_ips=[x for x in env.get("SELF_IPS", "").split(",") if x.strip()],
        concurrency=int(env.get("INSTALL_CONCURRENCY", "5")),
        wait_seconds=int(env.get("INSTALL_WAIT_SECONDS", "60")),
        cors=[x for x in env.get("CORS_ORIGINS", "").split(",") if x.strip()],
        static_dir=env.get("STATIC_DIR", "/app/static"),
    )


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: SecretStr


class HB(BaseModel):
    node_id: int


class NodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    ip: str = Field(max_length=64)
    ssh_port: int = Field(22, ge=1, le=65535)
    password: SecretStr
    save_password: bool = True
    vendor: str = Field("", max_length=32)
    note: str = Field("", max_length=200)


class NodePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    note: Optional[str] = Field(None, max_length=200)
    vendor: Optional[str] = Field(None, max_length=32)
    password: Optional[SecretStr] = None      # None=不改；空字符串=清除


class PwOpt(BaseModel):
    password: Optional[SecretStr] = None      # 不填则使用已保存的密码


class EventsDelete(BaseModel):
    ids: List[int] = Field(max_length=1000)
    confirm: bool = False


class EventsClear(BaseModel):
    before: float
    confirm_text: str = ""


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def create_app(cfg=None, clock=None, ssh_factory=None, start_sweeper=True, shell_factory=None):
    cfg = cfg or load_config()
    if not cfg["public_url"]:
        raise RuntimeError("必须设置 PUBLIC_URL（探针回连的控制端地址，应为 https://...）")
    if not cfg["public_url"].startswith("https://") and not cfg["allow_insecure"]:
        raise RuntimeError("PUBLIC_URL 必须使用 HTTPS（仅测试可设 ALLOW_INSECURE_HTTP=1）")
    if len(cfg.get("secret_key") or "") < 32:
        raise RuntimeError("必须设置 SECRET_KEY（至少 32 位随机字符，用于加密保存的 root 密码；例如 openssl rand -hex 32）")
    store = core.Store(cfg["db_path"], clock, cfg["secret_key"])
    if not store.has_admin():
        if not cfg["admin_user"] or not cfg["admin_password"]:
            raise RuntimeError("首次启动必须设置 ADMIN_USERNAME 和 ADMIN_PASSWORD")
        store.set_admin(cfg["admin_user"], cfg["admin_password"])
        store.audit("admin_init")
    store.sweep()                                                    # 重启后从数据库重新计算状态
    mgr = InstallManager(store, cfg, cfg["concurrency"], factory=ssh_factory)
    limiter = core.LoginLimiter(clock=clock)
    shell_factory = shell_factory or ParamikoShell
    term_lock, term_count = threading.Lock(), [0]

    @asynccontextmanager
    async def lifespan(app):
        task = None
        if start_sweeper:
            async def loop():
                while True:
                    try:
                        await run_in_threadpool(store.sweep)
                    except Exception:
                        log.exception("sweep failed")
                    await asyncio.sleep(5)
            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="VPS Heartbeat Monitor", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.mgr, app.state.cfg = store, mgr, cfg
    if cfg["cors"]:
        app.add_middleware(CORSMiddleware, allow_origins=cfg["cors"], allow_credentials=True,
                           allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type", "X-Requested-With"])

    @app.middleware("http")
    async def sec_headers(request, call_next):
        r = await call_next(request)
        r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                          "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:"})
        return r

    @app.exception_handler(RequestValidationError)
    async def bad_request(request, exc):   # 不回显 input，避免密码出现在错误响应里
        return JSONResponse({"detail": "参数不合法：" + "; ".join(".".join(str(x) for x in e["loc"][1:]) + " " + e["msg"] for e in exc.errors())}, status_code=422)

    def admin(request: Request):
        if not store.check_session(request.cookies.get(COOKIE)):
            raise HTTPException(401, "未登录")
        if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "hbm":
            raise HTTPException(403, "缺少 CSRF 头")

    @app.get("/healthz")
    def healthz():
        store._q("SELECT 1")
        return {"ok": True}

    # ---- 心跳（探针调用，无需登录，凭 节点ID + 独立 Token）----
    @app.post("/api/heartbeat")
    def heartbeat(body: HB, authorization: str = Header(default="")):
        tok = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not store.heartbeat(body.node_id, tok):
            raise HTTPException(401, "invalid credentials")
        return {"ok": True}

    # ---- 认证 ----
    @app.post("/api/auth/login")
    def login(body: Login, request: Request, response: Response):
        key = request.client.host if request.client else "?"
        if limiter.blocked(key):
            raise HTTPException(429, "登录失败次数过多，请稍后再试")
        if not store.verify_login(body.username, body.password.get_secret_value()):
            limiter.fail(key)
            store.audit("login_failed")
            raise HTTPException(401, "用户名或密码错误")
        limiter.ok(key)
        response.set_cookie(COOKIE, store.create_session(), httponly=True, secure=cfg["cookie_secure"], samesite="strict", max_age=core.SESSION_TTL, path="/")
        store.audit("login")
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        store.delete_session(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me", dependencies=[Depends(admin)])
    def me():
        return {"ok": True}

    # ---- 节点 ----
    @app.get("/api/nodes", dependencies=[Depends(admin)])
    def nodes(q: str = "", status: str = "", vendor: str = "", page: int = 1, size: int = 20):
        items, total = store.list_nodes(q[:64], status, max(page, 1), min(max(size, 1), 100), vendor[:32])
        return {"items": items, "total": total, "counts": store.counts()}

    @app.get("/api/vendors", dependencies=[Depends(admin)])
    def vendors():
        return {"items": store.list_vendors()}

    @app.get("/api/passwords", dependencies=[Depends(admin)])
    def passwords(ids: str = ""):
        """批量获取已保存的 root 密码（界面按需请求，不放在每 5 秒的轮询里）。"""
        want = [int(x) for x in ids.split(",") if x.strip().isdigit()][:100]
        return {str(k): v for k, v in store.passwords_for(want).items()}

    @app.get("/api/nodes/{nid}/password", dependencies=[Depends(admin)])
    def node_password(nid: int):
        n = store.get_node(nid)
        if not n:
            raise HTTPException(404, "节点不存在")
        pw = store.get_password(nid)
        if pw is None:
            raise HTTPException(409 if n["has_password"] else 404, "密码无法解密（SECRET_KEY 是否被更换？）" if n["has_password"] else "该节点未保存密码")
        return {"password": pw}

    def start_install(node_id, ip, port, pw, mode="install"):
        tok = store.rotate_token(node_id) if mode == "install" else ""
        try:
            return mgr.submit(node_id, ip, port, pw, tok, mode)
        except RuntimeError as e:
            raise HTTPException(429, str(e))

    @app.post("/api/nodes", dependencies=[Depends(admin)])
    def add_node(body: NodeIn):
        try:
            ip = core.check_target(body.ip, cfg["allow_private"], cfg["self_ips"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        if store.find_by_ip(ip):
            raise HTTPException(409, "该 IP 已存在")
        pw = body.password.get_secret_value()
        nid = store.add_node(body.name.strip(), ip, body.ssh_port, body.note, body.vendor.strip())
        if body.save_password:
            store.set_password(nid, pw)
        store.audit("node_add", f"id={nid}")
        return {"node_id": nid, "job_id": start_install(nid, ip, body.ssh_port, pw)}

    @app.get("/api/nodes/{nid}", dependencies=[Depends(admin)])
    def node(nid: int):
        n = store.get_node(nid)
        if not n:
            raise HTTPException(404, "节点不存在")
        n["events"], _ = store.list_events(node_id=nid, size=10)
        return n

    @app.patch("/api/nodes/{nid}", dependencies=[Depends(admin)])
    def patch(nid: int, body: NodePatch):
        if not store.update_node(nid, body.name, body.note, body.vendor.strip() if body.vendor is not None else None):
            raise HTTPException(404, "节点不存在")
        if body.password is not None:
            store.set_password(nid, body.password.get_secret_value())
        return {"ok": True}

    @app.delete("/api/nodes/{nid}", dependencies=[Depends(admin)])
    def delete(nid: int, confirm: bool = False):
        if not confirm:
            raise HTTPException(400, "删除节点需要二次确认")
        if not store.delete_node(nid):
            raise HTTPException(404, "节点不存在")
        store.audit("node_delete", f"id={nid}")
        return {"ok": True}

    def node_job(nid, body, mode):
        n = store.get_node(nid)
        if not n:
            raise HTTPException(404, "节点不存在")
        pw = (body.password.get_secret_value() if body.password else "") or store.get_password(nid)
        if not pw:
            raise HTTPException(400, "该节点未保存密码，请输入 root 密码")
        if mode == "install":
            store.set_status(nid, "installing")
        store.audit(f"node_{mode}", f"id={nid}")
        return {"job_id": start_install(nid, n["ip"], n["ssh_port"], pw, mode)}

    @app.post("/api/nodes/{nid}/reinstall", dependencies=[Depends(admin)])
    def reinstall(nid: int, body: PwOpt):
        return node_job(nid, body, "install")

    @app.post("/api/nodes/{nid}/uninstall", dependencies=[Depends(admin)])
    def uninstall(nid: int, body: PwOpt):
        return node_job(nid, body, "uninstall")

    @app.get("/api/jobs/{jid}", dependencies=[Depends(admin)])
    def job(jid: int):
        j = store.get_job(jid)
        if not j:
            raise HTTPException(404, "任务不存在")
        return j

    # ---- 故障历史 ----
    @app.get("/api/events", dependencies=[Depends(admin)])
    def events(node_id: Optional[int] = None, start: Optional[float] = None, end: Optional[float] = None, page: int = 1, size: int = 20):
        items, total = store.list_events(node_id, start, end, max(page, 1), min(max(size, 1), 100))
        return {"items": items, "total": total}

    @app.post("/api/events/delete", dependencies=[Depends(admin)])
    def events_delete(body: EventsDelete):
        try:
            return {"deleted": store.delete_events(body.ids, body.confirm)}
        except PermissionError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/events/clear", dependencies=[Depends(admin)])
    def events_clear(body: EventsClear):
        try:
            return {"deleted": store.clear_events(body.before, body.confirm_text == "清理历史")}
        except PermissionError as e:
            raise HTTPException(400, str(e))

    # ---- 网页 SSH 终端 ----
    # 协议：文本帧=JSON 控制消息；客户端→服务端的二进制帧=终端输入；服务端→客户端的二进制帧=终端输出
    @app.websocket("/api/nodes/{nid}/terminal")
    async def terminal(ws: WebSocket, nid: int):
        if (ws.headers.get("origin") or "").rstrip("/") != cfg["public_url"]:   # 防跨站 WebSocket 劫持
            await ws.close(code=1008)
            return
        if not store.check_session(ws.cookies.get(COOKIE)):
            await ws.close(code=1008)
            return
        await ws.accept()

        async def say(t, **kw):
            await ws.send_text(json.dumps({"type": t, **kw}, ensure_ascii=False))

        n = store.get_node(nid)
        if not n or n["status"] != "online":
            await say("error", msg="节点不存在或当前不在线，无法连接终端")
            await ws.close()
            return
        with term_lock:
            if term_count[0] >= MAX_TERMINALS:
                await say("error", msg=f"同时打开的终端已达上限（{MAX_TERMINALS}）")
                await ws.close()
                return
            term_count[0] += 1
        sh, opened = None, False
        try:
            init = json.loads(await asyncio.wait_for(ws.receive_text(), 30))
            cols, rows = _clamp(init.get("cols"), 20, 500, 80), _clamp(init.get("rows"), 5, 200, 24)
            pw = store.get_password(nid)
            if pw is None:
                if n["has_password"]:
                    await say("error", msg="已保存的密码无法解密（SECRET_KEY 是否被更换？）")
                    return
                await say("need_password")
                pw = (json.loads(await asyncio.wait_for(ws.receive_text(), 180)).get("password") or "")
                if not pw:
                    await say("error", msg="未提供密码")
                    return
            await say("status", msg="正在连接…")
            sh = shell_factory(n["ip"], n["ssh_port"], pw, n["host_fingerprint"])
            pw = None
            fp = await run_in_threadpool(sh.connect)
            if not n["host_fingerprint"]:
                store.set_fingerprint(nid, fp)
            await run_in_threadpool(sh.open_shell, cols, rows)
            opened = True
            store.audit("terminal_open", f"id={nid}")
            await say("ready")

            loop, q, last = asyncio.get_running_loop(), asyncio.Queue(), [time.monotonic()]

            def reader():
                try:
                    while True:
                        d = sh.read()
                        if d is None:
                            break
                        if d:
                            loop.call_soon_threadsafe(q.put_nowait, d)
                except Exception:
                    pass
                finally:
                    loop.call_soon_threadsafe(q.put_nowait, None)
            threading.Thread(target=reader, daemon=True).start()

            async def pump_out():
                while True:
                    d = await q.get()
                    if d is None:
                        return
                    await ws.send_bytes(d)

            async def pump_in():
                while True:
                    try:
                        m = await asyncio.wait_for(ws.receive(), 60)
                    except asyncio.TimeoutError:
                        if time.monotonic() - last[0] > TERMINAL_IDLE:
                            await say("error", msg="空闲超时，连接已断开")
                            return
                        continue
                    if m["type"] == "websocket.disconnect":
                        return
                    last[0] = time.monotonic()
                    if m.get("bytes") is not None:
                        await run_in_threadpool(sh.send, m["bytes"])
                    elif m.get("text"):
                        c = json.loads(m["text"])
                        if c.get("type") == "resize":
                            await run_in_threadpool(sh.resize, _clamp(c.get("cols"), 20, 500, 80), _clamp(c.get("rows"), 5, 200, 24))

            tasks = {asyncio.create_task(pump_out()), asyncio.create_task(pump_in())}
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        except WebSocketDisconnect:
            pass
        except Exception as e:   # 只回传固定文案，不回传异常内容
            log.warning("terminal node=%s failed: %s", nid, friendly(e))
            try:
                await say("error", msg=friendly(e))
            except Exception:
                pass
        finally:
            if sh:
                try:
                    await run_in_threadpool(sh.close)
                except Exception:
                    pass
            with term_lock:
                term_count[0] -= 1
            if opened:
                store.audit("terminal_close", f"id={nid}")
            try:
                await ws.close()
            except Exception:
                pass

    if os.path.isdir(cfg["static_dir"]):
        app.mount("/", StaticFiles(directory=cfg["static_dir"], html=True), name="static")
    return app
