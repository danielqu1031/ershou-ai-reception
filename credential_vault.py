import sys

def credential_vault():
    if sys.platform == 'win32':
        from windows_vault import WindowsVault
        return WindowsVault()
    from keychain_vault import KeychainVault
    return KeychainVault()
