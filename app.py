import asyncio
import json
import os
from pathlib import Path
import re
import secrets
from html import escape
from settings import CONFIG, CONFIGURED, PRODUCT, PORT, require_configured, money
from urllib.parse import parse_qs, urlparse
from aiohttp import web, ClientSession, ClientTimeout
from engine import Reception
from policy import MODEL, classify, decide
from xianyu import Xianyu
from saved_session import ChoiceStore, SavedSession, SessionError
from keychain_vault import KeychainVault
from recovery_control import RecoveryControl

ROOT = Path(__file__).resolve().parent
ORIGIN = f"http://127.0.0.1:{PORT}"


def render_index(token):
    page = (ROOT / 'index.html').read_text()
    values = {**PRODUCT, 'price':money(PRODUCT['price']),
              'min_price':money(PRODUCT['min_price']), 'model':CONFIG['model']}
    for name, value in values.items():
        page = page.replace('__PRODUCT_' + name.upper() + '__', escape(str(value), quote=True))
    return page.replace('__LOCAL_TOKEN__', token)


def make_app():
    os.umask(0o077)
    data = ROOT / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(32)

    @web.middleware
    async def local_only(request, handler):
        if request.host != f"127.0.0.1:{PORT}" or request.headers.get("Origin", ORIGIN) != ORIGIN:
            raise web.HTTPForbidden(text="仅允许本机页面访问")
        if request.path.startswith("/api/") and not secrets.compare_digest(request.headers.get("X-Local-Token", ""), token):
            raise web.HTTPForbidden(text="请重新打开本机接待页面")
        try:
            response = await handler(request)
        except SessionError:
            response = web.json_response({"error": "本机恢复设置未完成，请查看接待台恢复状态；登录信息未显示或记录"}, status=503)
        except ValueError as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    app = web.Application(middlewares=[local_only], client_max_size=40000)
    state = {"model": "待测试", "busy": False}

    async def lifecycle(app):
        async with ClientSession() as session:
            async def model(text):
                return await classify(session, text)
            async def send(message, text):
                await transport.send(message, text)
            engine = Reception(data / "reception.sqlite", model, send)
            transport = Xianyu(engine)
            app["engine"], app["transport"], app["session"] = engine, transport, session
            choices = ChoiceStore(data / "session-policy.sqlite")
            recovery = RecoveryControl(SavedSession(choices, KeychainVault()), engine, transport)
            app["recovery"] = recovery
            recovery.start()
            parent_task = None
            if os.environ.get('XIANYU_SUPERVISOR_PIPE'):
                from parent_watch import watch_parent
                parent_task = asyncio.create_task(watch_parent(int(os.environ['XIANYU_SUPERVISOR_PIPE'])))
            yield
            if parent_task:
                parent_task.cancel()
                await asyncio.gather(parent_task, return_exceptions=True)
            await recovery.close()
            await transport.stop()
            choices.close()
            engine.db.close()
    app.cleanup_ctx.append(lifecycle)

    async def index(request):
        return web.Response(text=render_index(token), content_type="text/html")

    async def status(request):
        engine, transport = app["engine"], app["transport"]
        return web.json_response({**engine.snapshot(), "version":"1.0.0", "configured":CONFIGURED, "recovery":app["recovery"].snapshot(), "connection": transport.status, "live_verified": transport.live_verified, "renewal_count": transport.renewal_count, "retry_count":transport.retry_count, "last_confirmed_at":transport.last_confirmed_at, "connection_history":transport.connection_history, "model": state["model"], "model_name": MODEL, "diagnostics": transport.diagnostics, "message_diagnostics": transport.message_diagnostics})

    async def renew(request):
        if not app['engine'].connected:
            raise ValueError("请先连接闲鱼")
        app['transport'].renew_requested.set()
        return web.json_response({"ok": True})

    async def simulate(request):
        payload = await request.json()
        text = payload.get("text", "")
        if not isinstance(text, str) or not 1 <= len(text) <= 1000:
            raise ValueError("请输入1至1000字的测试消息")
        if state["busy"]:
            raise ValueError("上一次本机测试正在进行，请稍等")
        state["busy"] = True
        try:
            intent = await classify(app["session"], text)
            decision = decide(text, intent)
            state["model"] = "本机模型可用"
            return web.json_response({"reply": decision.reply, "handoff": decision.handoff, "reason": decision.reason, "intent": intent})
        except Exception:
            state["model"] = "模型未就绪"
            raise ValueError("本机模型未就绪，请使用启动脚本启动；首次加载可能需要等待") from None
        finally:
            state["busy"] = False

    async def connect(request):
        require_configured()
        payload = await request.json()
        value = str(payload.get("item", "")).strip()
        item = value if value.isdigit() else parse_qs(urlparse(value).query).get("id", [""])[0]
        if not re.fullmatch(r"\d{6,24}", item):
            raise ValueError("请填写已发布商品的商品链接或商品ID，发布编辑页不能用于绑定")
        cookie = payload.get("cookie", "")
        if not isinstance(cookie, str):
            raise ValueError("登录信息格式不正确")
        await app["recovery"].connect(cookie, item, remember=payload.get("remember") is True)
        return web.json_response({"ok": True})

    async def mode(request):
        payload = await request.json()
        if payload.get("mode") == "auto":
            await app['recovery'].enable()
        else:
            await app['recovery'].pause()
        return web.json_response({"ok": True})

    async def preview(request):
        payload = await request.json()
        generation = app["transport"].generation
        await app["engine"].send_preview(str(payload.get("key", "")))
        if generation != app["transport"].generation or not app["engine"].connected:
            raise ValueError("测试时连接发生变化，请重新进行真实消息测试")
        app["transport"].live_verified = True
        app["engine"].remember_verification()
        app["transport"].status = "平台已确认测试发送 · 请到闲鱼核对"
        return web.json_response({"ok": True})

    async def resume(request):
        payload = await request.json()
        chat = str(payload.get("chat", ""))
        app["engine"].db.execute("DELETE FROM holds WHERE chat=?", (chat,))
        app["engine"].db.commit()
        return web.json_response({"ok": True})

    async def recover(request):
        payload = await request.json()
        chat = payload.get('chat')
        if not isinstance(chat, str) or not re.fullmatch(r'\d{1,30}', chat):
            raise ValueError('请选择需要恢复的会话')
        engine = app['engine']
        if engine.mode != 'auto' or not engine.connected or engine.held(chat) or engine.trading_blocked():
            raise ValueError('请先核对并恢复此会话的自动接待状态')
        app['transport'].recover_pending((chat,))
        return web.json_response({"ok": True})

    async def disconnect(request):
        await app["recovery"].pause(disconnect=True)
        return web.json_response({"ok": True})

    async def revoke(request):
        await app['recovery'].revoke()
        return web.json_response({"ok": True})

    app.add_routes([web.post('/api/revoke-login', revoke)])
    app.add_routes([web.get("/", index), web.get("/api/status", status), web.post("/api/simulate", simulate), web.post("/api/connect", connect), web.post("/api/mode", mode), web.post("/api/send-preview", preview), web.post("/api/resume", resume), web.post("/api/disconnect", disconnect)])
    app.add_routes([web.post('/api/renew', renew)])
    app.add_routes([web.post('/api/recover-pending', recover)])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="127.0.0.1", port=PORT, access_log=None)
