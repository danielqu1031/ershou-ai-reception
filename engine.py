"""Single-item reception with durable deduplication and explicit human ownership."""
import asyncio
import hashlib
import sqlite3
import time
from dataclasses import dataclass, replace
from policy import decide, Decision

from policy import unknown_reply
FALLBACK_REPLY = unknown_reply()

@dataclass(frozen=True)
class Message:
    item: str
    chat: str
    sender: str
    text: str
    timestamp: float

    @property
    def key(self):
        return hashlib.sha256(repr((self.item, self.chat, self.sender, self.text, self.timestamp)).encode()).hexdigest()


class Reception:
    def __init__(self, database, classifier, sender):
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, time REAL, chat TEXT,
          text TEXT, reply TEXT DEFAULT '', status TEXT, reason TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS holds (chat TEXT PRIMARY KEY, reason TEXT);
        CREATE TABLE IF NOT EXISTS verified_accounts (fingerprint TEXT, item TEXT,
          verified_at REAL, PRIMARY KEY(fingerprint,item));
        CREATE TABLE IF NOT EXISTS trading_states (seller TEXT, item TEXT, state TEXT,
          PRIMARY KEY(seller,item));
        CREATE TABLE IF NOT EXISTS queued_messages (key TEXT PRIMARY KEY, seller TEXT,
          item TEXT, chat TEXT, sender TEXT, text TEXT, timestamp REAL, mode TEXT);
        """)
        self.db.execute("INSERT OR REPLACE INTO holds SELECT chat, '上次发送未确认，请到闲鱼核对' FROM events WHERE status='sending'")
        self.db.execute("UPDATE events SET status='uncertain',reason='程序中断前发送未确认，不重试' WHERE status='sending'")
        self.db.execute("UPDATE events SET status='recoverable',reason='重启前尚未发送，等待同账号恢复自动接待' WHERE status='pending' AND key IN (SELECT key FROM queued_messages WHERE mode='auto')")
        self.db.execute("UPDATE events SET status='cancelled',reason='程序重启，未自动继续旧任务' WHERE status='pending'")
        self.db.execute("DELETE FROM queued_messages WHERE key NOT IN (SELECT key FROM events WHERE status='recoverable')")
        self.db.commit()
        self.classify, self.send = classifier, sender
        self.mode, self.item, self.seller = "preview", "", ""
        self.connected = False
        self.started = time.time()
        self.revision = 0
        self.last_sent = 0
        self.lock = asyncio.Lock()
        self.previews = {}
        self.echoes = {}

    def pause(self, preserve_recovery=False):
        self.mode = "preview"
        self._invalidate()
        if not preserve_recovery:
            self.db.execute("UPDATE events SET status='cancelled',reason='已主动暂停，不恢复未发任务' WHERE status='recoverable'")
            self.db.execute("DELETE FROM queued_messages WHERE key IN (SELECT key FROM events WHERE status='cancelled')")
            self.db.commit()

    def suspend(self):
        """Unsent work waits for transport; uncertain sends still pause()."""
        self.connected = False

    async def wait_connected(self, message, revision):
        while not self.connected and revision == self.revision and time.time()-message.timestamp <= 120:
            await asyncio.sleep(.25)

    def _invalidate(self):
        self.revision += 1
        self.previews.clear()
        self.db.execute("UPDATE events SET status='cancelled',reason='接待状态改变，旧任务和预览已取消' WHERE status IN ('pending','preview')")
        self.db.commit()

    def hold(self, chat, reason):
        self.db.execute("INSERT OR REPLACE INTO holds VALUES (?, ?)", (chat, reason))
        self.db.commit()

    def remember_verification(self):
        fingerprint = hashlib.sha256(self.seller.encode()).hexdigest()
        self.db.execute("INSERT OR REPLACE INTO verified_accounts VALUES (?,?,?)", (fingerprint, self.item, time.time()))
        self.db.commit()

    def previously_verified(self):
        fingerprint = hashlib.sha256(self.seller.encode()).hexdigest()
        return self.db.execute("SELECT 1 FROM verified_accounts WHERE fingerprint=? AND item=?", (fingerprint, self.item)).fetchone() is not None

    def stop_trading(self, state):
        self.db.execute('INSERT OR REPLACE INTO trading_states VALUES (?,?,?)', (self.seller, self.item, state))
        self.db.commit()
        self.pause()

    def trading_blocked(self):
        return self.db.execute('SELECT 1 FROM trading_states WHERE seller=? AND item=?', (self.seller, self.item)).fetchone() is not None

    def held(self, chat):
        return self.db.execute("SELECT 1 FROM holds WHERE chat=?", (chat,)).fetchone() is not None

    def finish(self, key, status, reply="", reason=""):
        self.db.execute("UPDATE events SET status=?,reply=?,reason=? WHERE key=?", (status, reply, reason, key))
        if status not in ('pending', 'recoverable'):
            self.db.execute("DELETE FROM queued_messages WHERE key=?", (key,))
        self.db.commit()

    async def recover_pending(self, confirmed_chats=()):
        """Restore unsent work only after checking post-crash chat history.

        Offline seller replies cannot be inferred from a fresh live connection.
        Until history reconciliation is implemented, require explicit per-chat
        confirmation. Automatic startup alone never constitutes that check.
        """
        if not self.connected or self.mode != 'auto' or not self.previously_verified() or self.trading_blocked():
            return
        rows = self.db.execute("SELECT q.* FROM queued_messages q JOIN events e ON e.key=q.key WHERE e.status='recoverable' AND q.seller=? AND q.item=? ORDER BY q.timestamp", (self.seller, self.item)).fetchall()
        for row in rows:
            if row['chat'] not in confirmed_chats:
                continue
            if not self.connected or self.mode != 'auto' or self.trading_blocked():
                return
            message = Message(row['item'], row['chat'], row['sender'], row['text'], row['timestamp'])
            if message.key != row['key'] or not -5 <= time.time()-message.timestamp <= 120:
                self.finish(row['key'], 'cancelled', reason='未发送任务已过期或记录不完整')
                continue
            if self.held(message.chat):
                self.finish(message.key, 'cancelled', reason='会话暂停，不恢复发送')
                continue
            # Claim atomically before yielding, so concurrent recovery cannot
            # duplicate a reply. Never recover a sending/uncertain event.
            claim = self.db.execute("UPDATE events SET status='pending',reason='' WHERE key=? AND status='recoverable'", (message.key,))
            self.db.commit()
            if claim.rowcount:
                await self._process(message, self.revision, self.mode)

    async def receive(self, message):
        if not self.connected or not self.item or message.item != self.item or self.trading_blocked():
            return
        now = time.time()
        if message.timestamp < self.started or not -5 <= now - message.timestamp <= 120:
            return
        if message.sender == self.seller:
            echo = (message.chat, message.text)
            if self.echoes.get(echo, 0) > time.time():
                return
            self.hold(message.chat, "检测到卖家消息，等待人工确认继续")
            return
        if not message.sender or not message.chat or not message.text or len(message.text) > 2000:
            return
        try:
            self.db.execute("INSERT INTO events (key,time,chat,text,status) VALUES (?,?,?,?,?)", (message.key, now, message.chat, message.text, "pending"))
            self.db.execute("INSERT INTO queued_messages VALUES (?,?,?,?,?,?,?,?)", (message.key, self.seller, message.item, message.chat, message.sender, message.text, message.timestamp, self.mode))
            self.db.commit()
        except sqlite3.IntegrityError:
            return
        await self._process(message, self.revision, self.mode)

    async def _process(self, message, revision, mode):
        async with self.lock:
            if self.held(message.chat):
                self.finish(message.key, "human", reason="该会话由人工接待")
                return
            try:
                result = decide(message.text, await self.classify(message.text))
            except Exception:
                result = decide(message.text, 'unknown')
            if not result.reply:
                result = replace(result, reply=FALLBACK_REPLY, handoff=False)
            await self.wait_connected(message, revision)
            if mode != "auto":
                if revision != self.revision or not self.connected or self.held(message.chat):
                    self.finish(message.key, "cancelled", result.reply, "连接或接待状态改变，预览失效")
                    return
                self.finish(message.key, "preview", result.reply, result.reason)
                if result.reply:
                    self.previews[message.key] = (message, result, revision)
                    self.previews = {k:v for k,v in self.previews.items() if time.time()-v[0].timestamp <= 120}
                return
            if revision != self.revision or not self.connected or self.mode != "auto" or self.held(message.chat) or time.time()-message.timestamp > 120:
                self.finish(message.key, "cancelled", result.reply, "状态改变或消息过期，未发送")
                return
            if not result.reply:
                self.hold(message.chat, result.reason)
                self.finish(message.key, "human", reason=result.reason)
                return
            # No retries after an uncertain send. Persist intent before network I/O.
            await asyncio.sleep(max(0, 5-(time.time()-self.last_sent)))
            await self.wait_connected(message, revision)
            if revision != self.revision or not self.connected or self.mode != "auto" or self.held(message.chat) or time.time()-message.timestamp > 120:
                self.finish(message.key, "cancelled", result.reply, "已暂停，未发送")
                return
            self.finish(message.key, "sending", result.reply)
            self.echoes = {k:v for k,v in self.echoes.items() if v > time.time()}
            self.echoes[(message.chat, result.reply)] = time.time()+120
            try:
                await self.send(message, result.reply)
            except (Exception, asyncio.CancelledError) as exc:
                self.hold(message.chat, "发送结果不确定，请在闲鱼核对")
                self.pause()
                self.finish(message.key, "uncertain", result.reply, "未确认发送结果，停止自动回复，不重试")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
            self.last_sent = time.time()
            self.finish(message.key, "sent", result.reply, result.reason)
            if result.handoff:
                self.hold(message.chat, result.reason)

    async def send_preview(self, key):
        async with self.lock:
            pair = self.previews.pop(key, None)
            if not pair:
                raise ValueError("没有可发送的真实预览；请让测试买家发一条新消息")
            message, result, revision = pair
            if revision != self.revision or message.item != self.item or not self.connected or self.held(message.chat) or time.time()-message.timestamp > 120:
                raise ValueError("消息已过期、连接断开或已由人工接管；请重新测试")
            self.finish(key, "sending", result.reply)
            self.echoes[(message.chat, result.reply)] = time.time()+120
            try:
                await self.send(message, result.reply)
            except (Exception, asyncio.CancelledError) as exc:
                self.hold(message.chat, "发送结果不确定，请在闲鱼核对")
                self.pause()
                self.finish(key, "uncertain", result.reply, "结果不确定，不重试")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ValueError("平台没有确认发送结果；已停止，请到闲鱼核对") from None
            self.finish(key, "sent", result.reply, result.reason)
            if result.handoff:
                self.hold(message.chat, result.reason)

    def snapshot(self):
        return {"mode": self.mode, "connected": self.connected, "item": self.item,
                "events": [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY time DESC LIMIT 80")],
                "holds": [dict(r) for r in self.db.execute("SELECT * FROM holds")]}
