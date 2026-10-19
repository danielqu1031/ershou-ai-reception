"""Build a helper scoped to this installation's configured seller/item."""
from pathlib import Path
import subprocess
import sys
import tempfile
from settings import CONFIG, ROOT, require_configured


def main():
    require_configured()
    if sys.platform != 'darwin':
        raise SystemExit('当前钥匙串和登录自启版本仅支持 macOS')
    source = (ROOT/'keychain_helper.swift').read_text()
    source = source.replace('__SELLER_ID__',CONFIG['seller_id']).replace('__ITEM_ID__',CONFIG['item_id'])
    with tempfile.TemporaryDirectory(prefix='xianyu-helper-') as folder:
        file = Path(folder)/'helper.swift'
        file.write_text(source)
        output = Path(folder)/'keychain-helper'
        subprocess.run(['/usr/bin/xcrun','swiftc',str(file),'-o',str(output)],check=True)
        subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(output)],check=True)
        output.replace(ROOT/'keychain-helper')
    print('已生成仅用于配置账号/商品的本机钥匙串工具。')

if __name__ == '__main__':
    main()
