// App-specific credentials only. JSON stdin/stdout are private pipes, never logs.
import Foundation
import Security

func output(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value) {
        FileHandle.standardOutput.write(data)
    }
}

let input = FileHandle.standardInput.readDataToEndOfFile()
guard input.count <= 16384,
      let request = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any],
      let op = request["op"] as? String,
      let ref = request["ref"] as? String,
      ref.range(of: "^[a-z0-9-]{10,100}$", options: .regularExpression) != nil
else { output(["ok": false, "status": -50]); exit(1) }

let service = "org.local-memory.workbench.models"
let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                           kSecAttrService as String: service,
                           kSecAttrAccount as String: ref]
var status: OSStatus = errSecParam
if op == "set", let secret = request["secret"] as? String,
   !secret.isEmpty, secret.utf8.count <= 8192 {
    let data = Data(secret.utf8)
    status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
    if status == errSecItemNotFound {
        var add = query
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        status = SecItemAdd(add as CFDictionary, nil)
    }
    output(["ok": status == errSecSuccess, "status": status])
} else if op == "get" || op == "has" {
    var lookup = query
    lookup[kSecReturnData as String] = op == "get"
    lookup[kSecMatchLimit as String] = kSecMatchLimitOne
    // Never block unattended work on an OS credential dialog.
    lookup[kSecUseAuthenticationUI as String] = kSecUseAuthenticationUIFail
    var result: CFTypeRef?
    status = SecItemCopyMatching(lookup as CFDictionary, &result)
    if op == "get", status == errSecSuccess, let data = result as? Data,
       let secret = String(data: data, encoding: .utf8) {
        output(["ok": true, "secret": secret])
    } else {
        output(["ok": status == errSecSuccess, "status": status])
    }
} else if op == "delete-test", ref.hasPrefix("test-") {
    status = SecItemDelete(query as CFDictionary)
    output(["ok": status == errSecSuccess || status == errSecItemNotFound, "status": status])
} else {
    output(["ok": false, "status": status])
}
