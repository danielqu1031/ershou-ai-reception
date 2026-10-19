import asyncio
import os
from pathlib import Path
import sys
import pytest
from file_lock import lock


def test_cross_process_lock_is_exclusive(tmp_path):
    import subprocess
    path = tmp_path/'lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    code = "import os,sys; from file_lock import lock; f=os.open(sys.argv[1],os.O_RDWR); lock(f)"
    try:
        lock(fd)
        result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
        assert result.returncode != 0
        assert b'BlockingIOError' in result.stderr
    finally:
        os.close(fd)
    assert subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True).returncode == 0


@pytest.mark.skipif(os.name != 'nt', reason='real Windows DPAPI')
def test_dpapi_roundtrip_encrypted_at_rest_and_delete(tmp_path):
    from windows_vault import WindowsVault, crypt
    from keychain_vault import APPROVED_SCOPE
    from saved_session import Credential, SERVICE, SessionError
    async def run():
        vault = WindowsVault(tmp_path)
        value = Credential(APPROVED_SCOPE, 'a'*32, 'FAKE_SECRET_TEST_ONLY')
        assert await vault.get(SERVICE, APPROVED_SCOPE.account) is None
        await vault.put(SERVICE, APPROVED_SCOPE.account, value)
        path, entropy = vault.location(SERVICE, APPROVED_SCOPE.account)
        encrypted = path.read_bytes()
        assert b'FAKE_SECRET_TEST_ONLY' not in encrypted
        assert await vault.get(SERVICE, APPROVED_SCOPE.account) == value
        with pytest.raises(SessionError):
            crypt(encrypted, b'wrong-scope', decrypt=True)
        with pytest.raises(SessionError):
            await vault.get(SERVICE, 'other')
        path.write_bytes(b'corrupt')
        with pytest.raises(SessionError):
            await vault.get(SERVICE, APPROVED_SCOPE.account)
        await vault.delete(SERVICE, APPROVED_SCOPE.account)
        assert await vault.get(SERVICE, APPROVED_SCOPE.account) is None
        assert not list(tmp_path.iterdir())
    asyncio.run(run())


@pytest.mark.skipif(os.name != 'nt', reason='real Windows registry')
def test_windows_startup_owned_value_only(tmp_path, monkeypatch):
    import windows_control as control
    import settings
    import winreg
    monkeypatch.setattr(control, 'ROOT', tmp_path)
    monkeypatch.setattr(control, 'DATA', tmp_path/'data')
    monkeypatch.setattr(control, 'STOP', tmp_path/'data/stop.request')
    monkeypatch.setattr(control, 'RUN_KEY', 'Software\\ErshouTests\\' + tmp_path.name)
    monkeypatch.setattr(settings, 'require_configured', lambda: None)
    executable = tmp_path/'.venv/Scripts/pythonw.exe'
    executable.parent.mkdir(parents=True)
    executable.touch()
    try:
        control.install()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, control.RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, control.LABEL)
        assert str(tmp_path/'manage.py') in value
        assert 'cookie' not in value.lower()
        assert control.disable()
        assert control.STOP.exists()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, control.RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, control.LABEL, 0, winreg.REG_SZ, 'unrelated installation')
        assert not control.disable()
        with pytest.raises(RuntimeError):
            control.install()
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, control.RUN_KEY)


def test_windows_stop_persists_inhibition_without_http(tmp_path, monkeypatch):
    import windows_control as control
    import manage
    monkeypatch.setattr(control, 'DATA', tmp_path)
    monkeypatch.setattr(control, 'STOP', tmp_path/'stop.request')
    monkeypatch.setattr(control, 'disable', lambda: control.STOP.touch() or True)
    monkeypatch.setattr(manage, 'reachable', lambda _: False)
    control.stop()
    assert (tmp_path/'recovery.disabled').exists()
    assert control.STOP.exists()
