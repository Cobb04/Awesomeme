import AppKit
import Carbon

// 与固定版本 KeyboardShortcuts 同模块编译，直接复用可失败的注册接口。
@objc(AwesomemeHotkey)
@MainActor
public final class AwesomemeHotkey: NSObject {
    private var hotKey: HotKey?
    @objc public private(set) var registered = false

    @objc(registerKeyCode:modifiers:)
    public func register(keyCode: Int, modifiers: Int) -> Bool {
        unregister()
        hotKey = HotKey(carbonKeyCode: keyCode, carbonModifiers: modifiers,
                        onKeyDown: {}, onKeyUp: { [weak self] in
            guard let self, self.registered else { return }
            NotificationCenter.default.post(name: .init("AwesomemeHotkeyPressed"), object: self)
        })
        registered = hotKey != nil
        hotKey?.onRegistrationFailed = { [weak self] in
            self?.registered = false
            NotificationCenter.default.post(name: .init("AwesomemeHotkeyFailed"), object: self)
        }
        return registered
    }

    // 释放上游 HotKey 会注销系统注册。
    @objc public func unregister() {
        hotKey = nil
        registered = false
    }

    // 按当前布局的 Command 映射寻找 V；失败时 Python 退回复制。
    @objc public static func pasteKeyCode() -> Int {
        guard let source = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
              let pointer = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData)
        else { return -1 }
        let data = Unmanaged<CFData>.fromOpaque(pointer).takeUnretainedValue()
        guard let bytes = CFDataGetBytePtr(data) else { return -1 }
        let layout = UnsafeRawPointer(bytes).assumingMemoryBound(to: UCKeyboardLayout.self)
        for code in UInt16(0)...UInt16(127) {
            var deadKeys: UInt32 = 0
            var count = 0
            var chars = [UniChar](repeating: 0, count: 4)
            let status = UCKeyTranslate(layout, code, UInt16(kUCKeyActionDown),
                                        UInt32(cmdKey >> 8), UInt32(LMGetKbdType()),
                                        OptionBits(kUCKeyTranslateNoDeadKeysMask),
                                        &deadKeys, chars.count, &count, &chars)
            if status == noErr && count == 1 && chars[0] == 118 {
                return Int(code)
            }
        }
        return -1
    }
}
