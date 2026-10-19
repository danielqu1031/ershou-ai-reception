import json
import pytest
import policy
from settings import CONFIG, read_config, require_configured


def test_example_cannot_connect_real_account():
    with pytest.raises(ValueError, match='示例模式不能连接'):
        require_configured()


def test_custom_product_prices_facts_and_unknown_reply(monkeypatch):
    product = {**CONFIG['product'], 'title':'示例台灯', 'price':300, 'min_price':260,
               'shipping':'运费另计，以页面为准', 'condition':'八成新',
               'function':'可调节亮度', 'scope':'仅一盏台灯，不含灯泡。',
               'capacity':'台灯无水杯容量信息。'}
    monkeypatch.setattr(policy,'PRODUCT',product)
    assert '不考虑' in policy.decide('250最多了','unknown').reply
    assert '270元' in policy.decide('270可以吗','unknown').reply
    assert '300元' in policy.decide('多少钱','price').reply
    assert '运费另计' in policy.decide('包邮吗','shipping').reply
    assert '八成新' in policy.decide('有摔过吗 几成新','unknown').reply
    assert policy.decide('有什么功能','thermal').reply == '可调节亮度'
    reply = policy.decide('这个细节你了解吗','unknown').reply
    assert '300元' in reply and '杯子' not in reply
    assert '260' not in reply


@pytest.mark.parametrize('field,value',[('price',float('nan')),('price',True),('min_price',101),('min_price',0)])
def test_invalid_prices_rejected(tmp_path,field,value):
    config = json.loads(json.dumps(CONFIG))
    config['product'][field] = value
    path = tmp_path/'config.json';path.write_text(json.dumps(config))
    with pytest.raises(ValueError):read_config(path)


def test_product_html_is_escaped(monkeypatch):
    import app
    monkeypatch.setattr(app,'PRODUCT',{**CONFIG['product'],'title':'<script>alert(1)</script>'})
    html = app.render_index('TEST_ONLY')
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '<script>alert(1)</script>' not in html


def test_public_connect_requires_own_config(tmp_path,monkeypatch):
    import app
    import asyncio
    from aiohttp.test_utils import TestServer, TestClient
    monkeypatch.setattr(app,'ROOT',tmp_path)
    (tmp_path/'index.html').write_text('token=__LOCAL_TOKEN__')
    async def run():
        async with TestClient(TestServer(app.make_app())) as client:
            headers={'Host':f'127.0.0.1:{app.PORT}'}
            page=await client.get('/',headers=headers)
            headers['X-Local-Token']=(await page.text()).split('=')[1]
            response=await client.post('/api/connect',headers=headers,json={'item':'222222222222','cookie':'FAKE'})
            assert response.status==400
            assert '示例模式不能连接' in (await response.json())['error']
    asyncio.run(run())


def test_large_currency_keeps_cents_and_accepts_supported_range(monkeypatch):
    monkeypatch.setattr(policy,'PRODUCT',{**CONFIG['product'],'price':10000.01,'min_price':10000.01})
    assert '10000.01元' in policy.decide('多少钱','price').reply
    assert '不考虑' in policy.decide('10000可以吗','unknown').reply
    monkeypatch.setattr(policy,'PRODUCT',{**CONFIG['product'],'price':20000,'min_price':15000})
    assert '16000元' in policy.decide('16000可以吗','unknown').reply


@pytest.mark.skipif(__import__('sys').platform == 'win32', reason='macOS launchctl adapter')
def test_stop_with_invalid_configuration_still_disables_recovery(tmp_path):
    import runpy
    import sys
    from unittest.mock import patch
    source = (__import__('pathlib').Path(__file__).parent/'manage.py').read_text()
    target = tmp_path/'manage.py';target.write_text(source)
    class Process:
        returncode=0
        stdout=''
    calls=[]
    def call(*args,**kwargs):
        calls.append(args[0]);return Process()
    # A failed settings import emulates malformed config before stop() runs.
    with patch.dict(sys.modules,{'settings':None}), patch.object(sys,'argv',['manage.py','stop']):
        # ImportError is intentionally not a config error, inject real parse error.
        import builtins
        original=builtins.__import__
        def importing(name,*args,**kwargs):
            if name=='settings':raise ValueError('invalid config')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=importing), patch('subprocess.run',side_effect=call):
            runpy.run_path(str(target),run_name='__main__')
    assert (tmp_path/'data/recovery.disabled').exists()
    assert any(cmd[1]=='disable' for cmd in calls)


@pytest.mark.parametrize('root',[[],None,'text',42])
def test_malformed_config_root_is_reported_as_configuration_error(tmp_path,root):
    path=tmp_path/'config.json';path.write_text(json.dumps(root))
    with pytest.raises(ValueError,match='JSON 对象'):read_config(path)
