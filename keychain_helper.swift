import Foundation
import Security

let service = "com.ershoureception.opensource.session"
let account = "__SELLER_ID__:__ITEM_ID__"
func fail(_ status: OSStatus) -> Never {
    FileHandle.standardError.write(Data("keychain-status:\(status)\n".utf8))
    exit(1)
}
guard CommandLine.arguments.count == 2 else { fail(errSecParam) }
let operation = CommandLine.arguments[1]
guard ["get", "put", "delete", "exists"].contains(operation) else { fail(errSecParam) }
var keychain: SecKeychain?
let path = NSHomeDirectory() + "/Library/Keychains/login.keychain-db"
let opened = SecKeychainOpen(path, &keychain)
guard opened == errSecSuccess, let login = keychain else { fail(opened) }
let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: account,
    kSecMatchSearchList as String: [login],
    kSecAttrSynchronizable as String: false
]
switch operation {
case "put":
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard data.count <= 65536,
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          object["seller"] as? String == "__SELLER_ID__",
          object["item"] as? String == "__ITEM_ID__",
          let generation = object["generation"] as? String, generation.count == 32,
          let cookie = object["cookie"] as? String, cookie.count <= 32000,
          !cookie.contains("\n"), !cookie.contains("\r") else { fail(errSecParam) }
    var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
    if status == errSecItemNotFound {
        var access: SecAccess?
        // nil trusts only this calling helper, never all applications.
        let created = SecAccessCreate("Xianyu receptionist local login" as CFString, nil, &access)
        guard created == errSecSuccess, let acl = access else { fail(created) }
        var add = query
        add.removeValue(forKey: kSecMatchSearchList as String)
        add[kSecUseKeychain as String] = login
        add[kSecValueData as String] = data
        add[kSecAttrAccess as String] = acl
        status = SecItemAdd(add as CFDictionary, nil)
    }
    guard status == errSecSuccess else { fail(status) }
case "delete":
    let status = SecItemDelete(query as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else { fail(status) }
case "get", "exists":
    var lookup = query
    lookup[kSecMatchLimit as String] = kSecMatchLimitOne
    if operation == "get" { lookup[kSecReturnData as String] = true }
    var result: CFTypeRef?
    let status = SecItemCopyMatching(lookup as CFDictionary, &result)
    if status == errSecItemNotFound { exit(4) }
    guard status == errSecSuccess else { fail(status) }
    if operation == "get" {
        guard let data = result as? Data else { fail(errSecDecode) }
        FileHandle.standardOutput.write(data)
    } else {
        FileHandle.standardOutput.write(Data("present\n".utf8))
    }
default: fail(errSecParam)
}
