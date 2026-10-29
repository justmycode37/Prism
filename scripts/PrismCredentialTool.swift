import Foundation
import Security

// Reads the secret from stdin so it never appears in command arguments.
guard CommandLine.arguments.count == 2 else { exit(2) }
let service = CommandLine.arguments[1]
guard ["studio.prism.control", "studio.prism.openai"].contains(service) else { exit(2) }
let data = FileHandle.standardInput.readDataToEndOfFile()
guard !data.isEmpty else { exit(2) }
let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: "local"
]
var item = query
item[kSecValueData as String] = data
var status = SecItemAdd(item as CFDictionary, nil)
if status == errSecDuplicateItem {
    status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
}
guard status == errSecSuccess else {
    FileHandle.standardError.write(Data("Keychain write failed (status \(status)).\n".utf8))
    exit(1)
}
print("Credential saved to macOS Keychain.")
