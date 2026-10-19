"""Current-Windows-user DPAPI storage. Plaintext never goes to disk."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
from saved_session import Credential, Scope, SessionError, SERVICE
from keychain_vault import APPROVED_SCOPE

class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

def crypt(raw, entropy, decrypt=False):
    if os.name != 'nt':
        raise SessionError('WINDOWS_REQUIRED')
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buf, extra = ctypes.create_string_buffer(raw), ctypes.create_string_buffer(entropy)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    salt = Blob(len(entropy), ctypes.cast(extra, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    fn = api.CryptUnprotectData if decrypt else api.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, ctypes.byref(salt), None, None, 1, ctypes.byref(out)):
        raise SessionError('CREDENTIAL_STORE_UNAVAILABLE')
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        kernel.LocalFree(out.data)

class WindowsVault:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path(os.environ['LOCALAPPDATA'])/'ErshouAIReception'/'credentials'

    def location(self, service, account):
        if service != SERVICE or account != APPROVED_SCOPE.account:
            raise SessionError('UNAPPROVED_CREDENTIAL_SCOPE')
        entropy = (service+'\0'+account).encode()
        return self.directory/(hashlib.sha256(entropy).hexdigest()+'.bin'), entropy

    async def get(self, service, account):
        path, entropy = self.location(service, account)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            data = json.loads(crypt(raw, entropy, decrypt=True))
            value = Credential(Scope(data['seller'], data['item']), data['generation'], data['cookie'])
            if value.scope != APPROVED_SCOPE:
                raise ValueError()
            return value
        except Exception:
            raise SessionError('CREDENTIAL_RECORD_INVALID') from None

    async def put(self, service, account, value):
        path, entropy = self.location(service, account)
        if value.scope != APPROVED_SCOPE:
            raise SessionError('UNAPPROVED_CREDENTIAL_SCOPE')
        raw = json.dumps(dict(seller=value.scope.seller, item=value.scope.item, generation=value.generation, cookie=value.cookie)).encode()
        encrypted = crypt(raw, entropy)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)

    async def delete(self, service, account):
        path, _ = self.location(service, account)
        path.unlink(missing_ok=True)
