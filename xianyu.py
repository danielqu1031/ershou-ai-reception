"""Experimental platform adapter. Never imports a browser's login/session files.

Wire format researched from shaxiu/XianyuAutoAgent (GPL-3.0); see NOTICE.md.
Unknown packets are ignored; authentication/ack failures close reception.
"""
import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse
import aiohttp
import msgpack
from engine import Message
from login_session import APP_KEY, LoginSession, LoginRequired

RENEW_SECONDS = 1800
HEARTBEAT_SECONDS = 15
REQUEST_TIMEOUT = 12

@dataclass(frozen=True)
class OrderNotice:
    item: str
    chat: str
    state: str = ""


def event_identity(event):
    body = event.get("1", {})
    if not isinstance(body, dict):
        return "", ""
    details = body.get("10", {})
    url = details.get("reminderUrl", "") if isinstance(details, dict) else ""
    item = parse_qs(urlparse(url).query).get("itemId", [""])[0] if isinstance(url, str) else ""
    return item, str(body.get("2", "")).split("@")[0]


def parse_event(event):
    if not isinstance(event, dict):
        return None
    meta = event.get("3", {})
    if isinstance(meta, dict) and meta.get("redReminder"):
        return OrderNotice(*event_identity(event), str(meta['redReminder']))
    body = event.get("1")
    if not isinstance(body, dict) or not isinstance(body.get("10"), dict):
        return None
    if isinstance(meta, dict) and str(meta.get("needPush")).lower() == "false":
        return None
    details = body["10"]
    try:
        item = parse_qs(urlparse(details["reminderUrl"]).query).get("itemId", [""])[0]
        text = details["reminderContent"]
        if not isinstance(text, str) or text.startswith("["):
            return None
        return Message(item, str(body["2"]).split("@")[0], str(details["senderUserId"]), text, int(body["5"])/1000)
    except (KeyError, TypeError, ValueError):
        return None


def _wire_map(pairs):
    result = {}
    for key, value in pairs:
        key = str(key)
        if key in result:
            raise ValueError("ambiguous message fields")
        result[key] = value
    return result


def decode_payload(encoded):
    if not isinstance(encoded, str) or len(encoded) > 200000:
        return None
    raw = base64.b64decode(encoded, validate=True)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        return msgpack.unpackb(raw, raw=False, strict_map_key=False, object_pairs_hook=_wire_map, max_str_len=200000, max_bin_len=200000, max_array_len=2000, max_map_len=2000)


class Xianyu:
    def __init__(self, reception):
        self.engine = reception
        self.on_cookie = self.on_connected = self.on_safety = None
        self.status = "未连接"
        self.ws = None
        self.task = None
        self.pending = {}
        self.jobs = set()
        self.live_verified = False
        self.lifecycle_lock = asyncio.Lock()
        self.generation = 0
        self.diagnostics = []
        self.message_diagnostics = []
        self._mid_counter = 0
        self.renewal_count = 0
        self.renew_requested = asyncio.Event()
        self.renewal_pending = False
        self.phase = 'idle'
        self.retry_count = 0
        self.connected_at = 0
        self.last_confirmed_at = 0
        self.connection_history = []

    def record_connection(self, event, **details):
        self.connection_history.append({'at':time.time(), 'event':event, **details})
        self.connection_history = self.connection_history[-30:]

    def _new_mid(self):
        # Decimal IDs plus channel suffix are required by the IM protocol.
        # Advance within the same millisecond and across clock corrections.
        self._mid_counter = max(self._mid_counter + 1, int(time.time()*1000)*1000)
        return f"{self._mid_counter} 0"

    async def stop(self):
        async with self.lifecycle_lock:
            await self._stop()

    async def _stop(self, preserve_recovery=False):
        self.generation += 1
        self.engine.pause(preserve_recovery=preserve_recovery)
        self.engine.connected = False
        self.live_verified = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("连接已关闭"))
        self.status = "未连接"

    async def connect(self, cookie, item):
        async with self.lifecycle_lock:
            await self._connect(cookie, item)

    async def _connect(self, cookie, item):
        await self._stop(preserve_recovery=True)
        self.diagnostics = []
        self.message_diagnostics = []
        cookies = {}
        for pair in cookie.split(";"):
            key, separator, value = pair.strip().partition("=")
            if separator:
                cookies[key] = value
        if not cookies.get("unb", "").isdigit() or not cookies.get("_m_h5_tk"):
            raise ValueError("登录信息缺少必要字段，请在本机重新填写。")
        if "\r" in cookie or "\n" in cookie or len(cookie) > 32000:
            raise ValueError("登录信息格式不正确。")
        self.engine.item, self.engine.seller = item, cookies["unb"]
        self.engine.started = time.time()
        self.renewal_count = 0
        self.renew_requested.clear()
        self.renewal_pending = False
        self.live_verified = self.engine.previously_verified()
        self.status = "连接中"
        self.task = asyncio.create_task(self._run(cookie, cookies))

    async def _request(self, lwp, body=None, headers=None):
        mid = self._new_mid()
        future = asyncio.get_running_loop().create_future()
        self.pending[mid] = future
        packet = {"lwp": lwp, "headers": {"mid": mid, **(headers or {})}}
        if body is not None:
            packet["body"] = body
        try:
            await self.ws.send_json(packet)
            reply = await asyncio.wait_for(future, REQUEST_TIMEOUT)
            if lwp == '/reg' and str(reply.get('code')) in ('401', '403'):
                raise LoginRequired()
            if str(reply.get("code")) != "200":
                raise RuntimeError("平台未确认请求成功")
            if lwp in ('/reg', '/!'):
                self.last_confirmed_at = time.time()
            return reply
        finally:
            self.pending.pop(mid, None)

    def recover_pending(self, confirmed_chats):
        task = asyncio.create_task(self.engine.recover_pending(confirmed_chats=confirmed_chats))
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

    async def _run(self, cookie, cookies):
        login = LoginSession(cookies, str(uuid.uuid4()) + "-" + cookies["unb"], self.on_cookie)
        cookie = None  # Retain only the renewable credential map, in memory.
        failures = 0
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), cookie_jar=aiohttp.DummyCookieJar()) as session:
                while True:
                    try:
                        self.phase = 'login'
                        token = await login.token(session)
                        await self._connection(session, login, token)
                        failures = 0
                        self.status = "正在自动续期"
                    except LoginRequired:
                        self.engine.pause()
                        self.live_verified = False
                        self.status = "闲鱼要求重新登录或验证，请完成后重连"
                        self.record_connection('login_required', phase=self.phase)
                        if self.on_safety:
                            await self.on_safety('login_required')
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if self.connected_at and time.monotonic()-self.connected_at >= 60:
                            failures = 0
                        self.connected_at = 0
                        failures += 1
                        self.retry_count += 1
                        delay = min(60, 2 ** min(failures, 6))
                        self.record_connection('retry', phase=self.phase, error_type=type(exc).__name__, delay=delay)
                        self.status = f"连接暂时中断（{self.phase}/{type(exc).__name__}），{delay}秒后自动重连"
                    finally:
                        self.engine.suspend()
                        self.ws = None
                        for future in list(self.pending.values()):
                            if not future.done():
                                future.set_exception(ConnectionError("连接中断"))
                    if failures:
                        await asyncio.sleep(delay)
        finally:
            self.engine.connected = False
            self.engine.pause()
            self.live_verified = False
            for job in list(self.jobs):
                job.cancel()
            if self.jobs:
                await asyncio.gather(*self.jobs, return_exceptions=True)

    async def _connection(self, session, login, token):
        # Reconcile authoritative item state before allowing resumed replies,
        # including sales made during outages longer than the replay window.
        self.phase = 'item-status'
        if not await login.available(session, self.engine.item):
            self.engine.stop_trading('unknown')
            if self.on_safety:
                await self.on_safety('unknown')
        self.phase = 'websocket'
        async with session.ws_connect("wss://wss-goofish.dingtalk.com/", headers=login.headers(), max_msg_size=1000000) as ws:
            self.ws = ws
            self.generation += 1
            receiver = asyncio.create_task(self._read())
            try:
                self.phase = 'register'
                await self._request("/reg", headers={"cache-header":"app-key token ua wv", "app-key":APP_KEY, "token":token, "ua":"DingTalk(2.1.5) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5", "dt":"j", "wv":"im:3,au:3,sy:6", "sync":"0,0;0;0;", "did":login.device})
                # Re-request recent messages; durable keys suppress duplicates.
                now = int(max(self.engine.started, time.time()-120)*1000)
                self.engine.connected = True
                self.connected_at = time.monotonic()
                self.record_connection('connected')
                if self.on_connected:
                    await self.on_connected()
                if self.renewal_pending:
                    self.renewal_count += 1
                    self.renewal_pending = False
                await ws.send_json({"lwp":"/r/SyncStatus/ackDiff", "headers":{"mid":self._new_mid()}, "body":[{"pipeline":"sync", "tooLong2Tag":"PNM,1", "channel":"sync", "topic":"sync", "highPts":0, "pts":now*1000, "seq":0, "timestamp":now}]})
                self.status = "已连接 · 自动续期与重连已启用" if not self.engine.trading_blocked() else "已连接 · 商品非在售或已有订单，已停止出售回复"
                deadline = time.monotonic() + RENEW_SECONDS
                while True:
                    done, _ = await asyncio.wait({receiver}, timeout=HEARTBEAT_SECONDS)
                    if done:
                        receiver.result()
                        raise ConnectionError("消息通道关闭")
                    self.phase = 'heartbeat'
                    await self._request("/!")
                    if time.monotonic() >= deadline or self.renew_requested.is_set():
                        self.renew_requested.clear()
                        # Platform did not acknowledge re-registering on the
                        # same socket. Drain active sends before replacing it.
                        async with self.engine.lock:
                            self.engine.suspend()
                            self.renewal_pending = True
                        return
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)

    async def _read(self):
        async for packet in self.ws:
            if len(self.diagnostics) < 12:
                self.diagnostics.append({"frame_type": packet.type.name})
            if packet.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                message = json.loads(packet.data)
                headers = message.get("headers", {})
                mid = headers.get("mid")
                if len(self.diagnostics) < 12:
                    self.diagnostics.append({"code": message.get("code") if isinstance(message.get("code"), (int, float)) else None,
                        "has_headers": isinstance(headers, dict), "has_mid": bool(mid), "matches_pending": mid in self.pending,
                        "has_body": "body" in message, "has_lwp": "lwp" in message})
                if "code" in message:
                    future = self.pending.get(mid)
                    if future and not future.done():
                        future.set_result(message)
                    continue
                if "lwp" in message and mid:
                    await self.ws.send_json({"code":200, "headers":{key:headers[key] for key in ("mid","sid","app-key","ua","dt") if key in headers}})
                packages = message.get("body", {}).get("syncPushPackage", {}).get("data", [])
                for payload in packages:
                    decoded = decode_payload(payload.get("data", ""))
                    event = parse_event(decoded)
                    observation = {"parsed": type(event).__name__, "matched_item": bool(event and event.item == self.engine.item)}
                    if isinstance(event, Message):
                        observation.update(age_seconds=int(time.time()-event.timestamp), before_connection=event.timestamp < self.engine.started)
                    self.message_diagnostics.append(observation)
                    self.message_diagnostics = self.message_diagnostics[-20:]
                    if isinstance(event, OrderNotice):
                        if event.item == self.engine.item and event.item:
                            if '等待买家付款' in event.state:
                                self.status = "已有待付款订单 · 继续接待"
                            else:
                                sold = any(s in event.state for s in ('等待卖家发货', '等待买家收货', '交易成功'))
                                self.engine.stop_trading('sold' if sold else 'unknown')
                                if self.on_safety:
                                    await self.on_safety('sold' if sold else 'unknown')
                                self.status = "商品已有付款订单，已停止继续出售回复" if sold else "交易状态变化，停止出售回复，需核对商品状态"
                        elif not event.item:
                            self.engine.stop_trading('unknown')
                            if self.on_safety:
                                await self.on_safety('unknown')
                            self.status = "交易通知无法确认商品归属；预防误售，已暂停，请在闲鱼核对"
                    elif isinstance(event, Message):
                        if len(self.jobs) >= 30:
                            self.engine.pause()
                            if self.on_safety:
                                await self.on_safety("backlog")
                            self.status = "咨询积压，已暂停"
                            continue
                        task = asyncio.create_task(self.engine.receive(event))
                        self.jobs.add(task)
                        task.add_done_callback(self.jobs.discard)
            except (ValueError, TypeError, KeyError, AttributeError, msgpack.UnpackException) as exc:
                if len(self.diagnostics) < 12:
                    self.diagnostics.append({"decode_error": type(exc).__name__})
                continue
        self.engine.connected = False
        self.status = "消息通道已断开，正在恢复连接"

    async def send(self, message, text):
        if not self.engine.connected or message.item != self.engine.item:
            raise RuntimeError("未连接或商品不匹配")
        content = base64.b64encode(json.dumps({"contentType":1, "text":{"text":text}}, ensure_ascii=False).encode()).decode()
        await self._request("/r/MessageSend/sendByReceiverScope", body=[{
            "uuid": "-"+str(int(time.time()*1000000))+"1", "cid":message.chat+"@goofish", "conversationType":1,
            "content":{"contentType":101, "custom":{"type":1, "data":content}},
            "redPointPolicy":0, "extension":{"extJson":"{}"}, "ctx":{"appVersion":"1.0","platform":"web"}, "mtags":{}, "msgReadStatusSetting":1
        }, {"actualReceivers":[message.sender+"@goofish", self.engine.seller+"@goofish"]}])
