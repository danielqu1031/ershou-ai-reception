"""In-memory login renewal; never exports credentials or follows redirects."""
import hashlib
import json
import time

APP_KEY = "444e9908a51d1cb236a27862abc769c9"
TOKEN_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"


class LoginRequired(Exception):
    pass


class LoginSession:
    def __init__(self, cookies, device, on_cookie=None):
        self.on_cookie = on_cookie
        self.cookies = dict(cookies)
        self.seller = self.cookies['unb']
        self.device = device

    def headers(self):
        return {'Cookie': '; '.join(f'{k}={v}' for k, v in self.cookies.items()),
                'Origin': 'https://www.goofish.com', 'Referer': 'https://www.goofish.com/',
                'User-Agent': 'Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36'}

    def update(self, cookies):
        updates = {k: v.value for k, v in cookies.items()}
        if 'unb' in updates and updates['unb'] != self.seller:
            raise LoginRequired()
        self.cookies.update(updates)

    async def token(self, session):
        result = await self.api(session, 'mtop.taobao.idlemessage.pc.login.token', {'appKey': APP_KEY, 'deviceId': self.device})
        token = result.get('accessToken')
        if not isinstance(token, str) or not token:
            raise LoginRequired()
        return token

    async def available(self, session, item):
        data = await self.api(session, 'mtop.taobao.idle.pc.detail', {'itemId': item})
        detail = data.get('itemDO', {})
        # Only the exact online state observed on the seller's item is accepted.
        # Unknown, removed or sold states never imply availability.
        return (str(detail.get('itemId')) == item
                and str(detail.get('trackParams', {}).get('sellerId')) == self.seller
                and detail.get('itemStatus') == 0
                and detail.get('itemStatusStr') == '在线'
                and detail.get('soldCnt') == 0)

    async def api(self, session, api, payload):
        data = json.dumps(payload, separators=(',', ':'))
        for attempt in range(2):
            stamp = str(int(time.time()*1000))
            old = self.cookies.get('_m_h5_tk', '')
            sign = hashlib.md5(f"{old.split('_')[0]}&{stamp}&34839810&{data}".encode()).hexdigest()
            async with session.post(f'https://h5api.m.goofish.com/h5/{api}/1.0/', headers=self.headers(), params={
                'jsv':'2.7.2', 'appKey':'34839810', 't':stamp, 'sign':sign, 'v':'1.0',
                'type':'originaljson', 'accountSite':'xianyu', 'dataType':'json',
                'timeout':'20000', 'api':api,
                'sessionOption':'AutoLoginOnly'}, data={'data': data}, allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise LoginRequired()
                response.raise_for_status()
                self.update(response.cookies)
                if response.cookies and self.on_cookie:
                    await self.on_cookie(self.headers()["Cookie"])
                result = await response.json(content_type=None)
            codes = [str(r).split('::')[0] for r in result.get('ret', [])]
            if any(c.startswith('SUCCESS') for c in codes) and isinstance(result.get('data'), dict):
                return result['data']
            token_error = any('TOKEN' in c for c in codes)
            if token_error and not attempt and self.cookies.get('_m_h5_tk') != old:
                continue
            if token_error or any(any(word in c for word in ('SESSION', 'VALIDATE', 'LOGIN', 'RGV', 'SECURITY')) for c in codes):
                raise LoginRequired()
            raise RuntimeError('闲鱼接口暂不可用')
