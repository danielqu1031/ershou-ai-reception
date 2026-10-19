"""Validated local configuration; no credentials belong in this file."""
import json
import math
from decimal import Decimal
from runtime_constants import LABEL, SERVICE
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / 'config.local.json'


def read_config(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('配置文件最外层必须是 JSON 对象')
    for name in ('seller_id', 'item_id'):
        if not isinstance(value.get(name), str) or not re.fullmatch(r'\d{6,24}', value[name]):
            raise ValueError(f'{name} 必须是6至24位数字字符串')
    if type(value.get('port')) is not int or not 1024 <= value['port'] <= 65535:
        raise ValueError('port 必须是1024至65535之间的整数')
    if not isinstance(value.get('model'), str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', value['model']):
        raise ValueError('请填写有效的本机 Ollama 模型名')
    product = value.get('product')
    if not isinstance(product, dict):
        raise ValueError('缺少 product 配置')
    for key in ('price', 'min_price'):
        amount = product.get(key)
        if type(amount) not in (int, float) or not math.isfinite(amount) or not 0 < amount <= 999999:
            raise ValueError(f'{key} 必须是有效正数')
    for key in ('price', 'min_price'):
        amount = Decimal(str(product[key]))
        if amount != amount.quantize(Decimal('0.01')):
            raise ValueError('价格最多保留两位小数')
    if product['min_price'] > product['price']:
        raise ValueError('底价不能超过标价')
    for key in ('title','shipping','condition','scope','function','thermal_duration','capacity','material','dispatch','damage'):
        if not isinstance(product.get(key), str) or not 1 <= len(product[key].strip()) <= 500:
            raise ValueError(f'商品字段 {key} 需填写1至500字；未知信息请明确写未确认')
    return value


CONFIGURED = CONFIG_PATH.is_file()
CONFIG = read_config(CONFIG_PATH if CONFIGURED else ROOT/'config.example.json')
PRODUCT = CONFIG['product']
PORT = CONFIG['port']
MODEL = CONFIG['model']


def require_configured():
    if not CONFIGURED or CONFIG['seller_id'] == '111111111' or CONFIG['item_id'] == '222222222222':
        raise ValueError('请先复制 config.example.json 为 config.local.json，并填写自己的账号、商品及真实信息；示例模式不能连接闲鱼')


def money(value):
    return format(Decimal(str(value)), ".2f").rstrip("0").rstrip(".")
