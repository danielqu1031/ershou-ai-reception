"""Serialize user choices with startup recovery; never replay old messages."""
import asyncio
from dataclasses import replace
from saved_session import SessionError, validate_cookie
from keychain_vault import APPROVED_SCOPE
from startup_control import disable_startup, inhibit_recovery


class RecoveryControl:
    def __init__(self, saved, engine, transport, disable=disable_startup):
        self.saved, self.engine, self.transport = saved, engine, transport
        self.disable = disable
        self.scope = APPROVED_SCOPE
        self.credential = None
        self.restore_auto = False
        self.lock = asyncio.Lock()
        self.task = None
        self.notice = ''
        self.inhibited = False
        self.inhibit_path = saved.choices.path.parent / 'recovery.disabled'
        transport.on_cookie = self.cookie_changed
        transport.on_connected = self.connected
        transport.on_safety = self.safety_stop

    def start(self):
        self.task = asyncio.create_task(self.restore())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def snapshot(self):
        try:
            choice = self.saved.choices.get(self.scope)
            return {'state':choice.state if choice else 'not_saved',
                    'auto_restore':bool(choice and choice.auto),
                    'notice':self.notice}
        except Exception:
            return {'state':'unavailable', 'auto_restore':False,
                    'notice':'恢复设置暂不可读，自动恢复未确认'}

    async def fail_closed(self):
        self.engine.pause()
        self.restore_auto = False
        self.inhibited = True
        recorded = False
        try:
            inhibit_recovery(self.inhibit_path)
            recorded = True
        except OSError:
            pass
        try:
            disabled = await self.disable()
        except Exception:
            disabled = False
        self.notice = ('恢复设置保存失败，已禁止自动恢复' if recorded or disabled else
                       '恢复设置保存失败，已暂停；登录自启未能禁用，需要处理')
        raise SessionError(self.notice)

    async def durable_pause(self):
        self.engine.pause()
        self.restore_auto = False
        try:
            await self.saved.pause(self.scope)
        except Exception:
            await self.fail_closed()

    async def pause(self, disconnect=False):
        # Invalidate immediately, even when a vault operation owns the lock.
        self.engine.pause()
        self.restore_auto = False
        async with self.lock:
            await self.durable_pause()
            if disconnect:
                await self.transport.stop()
        self.notice = '已暂停；下次登录或程序重启不会自动回复'

    async def restore(self):
        # Locked/unavailable Keychain stays quiet and in preview, with backoff.
        while True:
            if self.inhibited or self.inhibit_path.exists():
                self.notice = '自动恢复已停用，请检查设置后手动开启'
                return
            try:
                async with self.lock:
                    choice = self.saved.choices.get(self.scope)
                    if choice and choice.state.startswith('revoking'):
                        await self.saved.revoke(self.scope)
                        return
                    credential = await self.saved.restore_candidate(self.scope)
                    if credential is None:
                        return
                    self.credential = credential
                    self.restore_auto = True
                    self.notice = '正在恢复已授权的接待'
                    await self.transport.connect(credential.cookie, self.scope.item)
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                self.engine.pause()
                self.notice = '登录钥匙串暂不可用，保持暂停，稍后重试'
                await asyncio.sleep(30)

    async def connect(self, cookie, item, remember=False):
        if item != self.scope.item:
            raise ValueError('此接待台仅用于已授权的这件商品')
        validate_cookie(self.scope, cookie)
        async with self.lock:
            await self.durable_pause()
            await self.transport.stop()
            self.credential = None
            if remember:
                if item != self.scope.item:
                    raise ValueError('本机保存仅授权给当前这件商品')
                await self.saved.save(self.scope, cookie, explicit_consent=True)
                choice = self.saved.choices.get(self.scope)
                from saved_session import Credential
                self.credential = Credential(self.scope, choice.generation, cookie)
                self.notice = '登录已保存到本机钥匙串；开启自动接待后可自动恢复'
            else:
                self.notice = '本次连接仅使用内存，重启后不恢复'
            await self.transport.connect(cookie, item)

    def can_enable(self):
        return (self.engine.connected and self.transport.live_verified
                and not self.engine.trading_blocked()
                and not self.engine.db.execute("SELECT 1 FROM holds LIMIT 1").fetchone())

    async def enable(self):
        async with self.lock:
            if not self.can_enable():
                raise ValueError('需已连接、真实收发已验证，且没有未处理的异常会话')
            revision = self.engine.revision
            if self.credential:
                await self.saved.enable(self.scope, verified=True, online=True)
            if revision != self.engine.revision or not self.can_enable():
                await self.durable_pause()
                raise ValueError('连接或商品状态改变，未开启自动接待')
            self.inhibit_path.unlink(missing_ok=True)
            self.inhibited = False
            self.engine.mode = 'auto'
            self.notice = '自动接待已开启' + ('，登录或程序重启后自动恢复' if self.credential else '')

    async def connected(self):
        async with self.lock:
            if self.inhibited or self.inhibit_path.exists() or not self.restore_auto or not self.credential:
                return
            choice = self.saved.choices.get(self.scope)
            valid = (choice and choice.auto and choice.state == 'ready'
                     and choice.generation == self.credential.generation
                     and self.engine.seller == self.scope.seller
                     and self.engine.item == self.scope.item)
            if valid and self.can_enable():
                self.engine.mode = 'auto'
                self.notice = '已自动恢复接待；停机前旧消息不会自动重发'
            else:
                await self.durable_pause()
                self.notice = '存在未处理的异常或商品状态变化，已保持暂停'
            self.restore_auto = False

    async def cookie_changed(self, cookie):
        # A connection is stopped before replacing this generation.
        credential = self.credential
        if credential:
            try:
                await self.saved.refresh(replace(credential, cookie=cookie))
            except Exception:
                self.notice = '本次连接可用，但登录续期未保存；重启恢复可能需要重新连接'

    async def safety_stop(self, reason):
        self.engine.pause()
        self.restore_auto = False
        # Do not acquire controller.lock here: connect/stop joins transport tasks.
        try:
            if reason == 'sold':
                await self.saved.revoke(self.scope, sold=True)
            else:
                await self.saved.pause(self.scope)
        except Exception:
            await self.fail_closed()

    async def revoke(self):
        self.engine.pause()
        self.restore_auto = False
        async with self.lock:
            await self.transport.stop()
            try:
                await self.saved.revoke(self.scope)
            except Exception:
                await self.fail_closed()
            self.credential = None
            if not await self.disable():
                raise SessionError('登录信息已移除，但登录自启未能禁用')
            self.notice = '本机登录信息已移除，登录自启已禁用'
