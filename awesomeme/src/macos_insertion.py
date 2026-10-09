"""只向本次选择前的原窗口请求一次粘贴。"""

_TARGETS = {"com.tencent.xinWeChat", "com.tencent.WeWorkMac", "com.electron.lark"}
_OPAQUE_ROLES = {None, "AXWindow", "AXApplication", "AXGroup", "AXUnknown", "AXWebArea"}


# 仅读取焦点对象和几何信息，不读取输入文字、聊天内容或窗口标题。
def _attribute(element, name):
    import ApplicationServices as AX

    error, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if error == AX.kAXErrorSuccess else None


# 必须知道选区为空，才能避免把已有选中文字替换成图片。
def _empty_selection(element):
    return _selection_state(element) == "empty"


# 区分选中文字与客户端未公开选区；微信兼容路线只放行后者。
def _selection_state(element):
    import ApplicationServices as AX

    value = _attribute(element, "AXSelectedTextRange")
    if value is None:
        return "unknown"
    success, selected = AX.AXValueGetValue(value, AX.kAXValueCFRangeType, None)
    if not success:
        return "unknown"
    location, length = selected
    if not 0 <= location < 2**63 - 1 or length < 0:
        return "unknown"
    return "selected" if length else "empty"


# 区分无组词、正在组词、未公开属性和读取失败；不读取输入文字。
def _composition_state(element):
    import ApplicationServices as AX

    name = "AXTextInputMarkedRange"
    try:
        error, names = AX.AXUIElementCopyAttributeNames(element, None)
        if error != AX.kAXErrorSuccess or names is None:
            return "unknown"
        if name not in names:
            return "unsupported"
        error, value = AX.AXUIElementCopyAttributeValue(element, name, None)
        if error == AX.kAXErrorNoValue:
            return "clear"
        if error != AX.kAXErrorSuccess or value is None:
            return "unknown"
        success, marked = AX.AXValueGetValue(value, AX.kAXValueCFRangeType, None)
        if not success:
            return "unknown"
        _, length = marked
        if length < 0:
            return "unknown"
        return "marked" if length else "clear"
    except Exception:
        return "unknown"


# 统一捕获时和提交前的输入状态判定。
def _input_state_reason(field, window_compat=False, composition_optional=False):
    role = _attribute(field, "AXRole") if field is not None else None
    if window_compat and role in _OPAQUE_ROLES:
        return ""
    if role not in ("AXTextArea", "AXTextField"):
        return "input_not_identified"
    if _attribute(field, "AXSubrole") in ("AXSecureTextField", "AXSearchField"):
        return "input_not_identified"
    if not _empty_selection(field) and not (
        window_compat and _selection_state(field) == "unknown"
    ):
        return "selection_not_empty_or_unknown"
    state = _composition_state(field)
    if state == "marked":
        return "composition_active"
    if state == "clear" or window_compat:
        return ""
    if composition_optional and state == "unsupported":
        return ""
    return "composition_state_unverified"


# 捕获对象引用仅留在内存；不通过 JS 接口暴露 PID 或 AX 对象。
def capture_target():
    import AppKit
    import ApplicationServices as AX

    running = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if running is None or running.bundleIdentifier() not in _TARGETS:
        return {"reason": "unsupported_target"}
    identity = {"bundle_id": running.bundleIdentifier()}
    if not AX.AXIsProcessTrusted():
        return {**identity, "reason": "accessibility_required"}
    application = AX.AXUIElementCreateApplication(running.processIdentifier())
    if AX.AXUIElementSetMessagingTimeout(application, 0.25) != AX.kAXErrorSuccess:
        return {**identity, "reason": "application_unavailable"}
    window = _attribute(application, "AXFocusedWindow")
    field = _attribute(application, "AXFocusedUIElement")
    if window is None:
        return {**identity, "reason": "window_not_identified"}
    if field is not None and (
        AX.AXUIElementSetMessagingTimeout(field, 0.25) != AX.kAXErrorSuccess
    ):
        return {**identity, "reason": "field_unavailable"}
    window_compat = running.bundleIdentifier() == "com.tencent.xinWeChat"
    composition_optional = running.bundleIdentifier() == "com.electron.lark"
    return {
        **identity,
        "reason": _input_state_reason(field, window_compat, composition_optional),
        "mode": "window" if window_compat else "field",
        "composition_optional": composition_optional,
        "running": running,
        "application": application,
        "window": window,
        "field": field,
        "field_role": _attribute(field, "AXRole") if field is not None else None,
        "posted": False,
    }


# AX 采用主屏左上坐标；AppKit 采用主屏左下坐标，保留负数多屏坐标。
def input_anchor(target):

    import AppKit
    import ApplicationServices as AX

    if target.get("field") is None:
        return None
    if _attribute(target["field"], "AXRole") not in ("AXTextArea", "AXTextField"):
        return None
    position = _attribute(target["field"], "AXPosition")
    size = _attribute(target["field"], "AXSize")
    if position is None or size is None:
        return None
    p_ok, p = AX.AXValueGetValue(position, AX.kAXValueCGPointType, None)
    s_ok, s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
    if not p_ok or not s_ok:
        return None
    x, y = p
    width, height = s
    if width <= 0 or height <= 0:
        return None
    primary = AppKit.NSScreen.screens()[0].frame()
    return AppKit.NSMakePoint(x, primary.origin.y + primary.size.height - y)


# 收起面板后重新核对前台进程、窗口、输入控件和选区，不主动切换到已失焦的应用。
def validate_target(target):
    import os

    import AppKit
    import ApplicationServices as AX

    if target.get("reason"):
        return target["reason"]
    if target.get("posted"):
        return "already_requested"
    if not AX.AXIsProcessTrusted():
        return "accessibility_required"

    # 仅记录失败环节，不保存进程号、窗口标题或输入内容。
    def changed(detail):
        target["change_detail"] = detail
        return "target_changed"

    running = target["running"]
    current = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if running.isTerminated() or current is None:
        return changed("process_unavailable")
    if (
        current.processIdentifier() != running.processIdentifier()
        or current.launchDate() != running.launchDate()
    ):
        own_pid = os.getpid()
        return changed(
            "picker_became_active"
            if current.processIdentifier() == own_pid
            else "other_app_active"
        )
    window = _attribute(target["application"], "AXFocusedWindow")
    if window != target["window"]:
        return changed("window_missing" if window is None else "window_changed")
    field = _attribute(target["application"], "AXFocusedUIElement")
    window_compat = target.get("mode") == "window"
    original_role = target.get("field_role")
    if (not window_compat or original_role not in _OPAQUE_ROLES) and field != target[
        "field"
    ]:
        return changed("field_missing" if field is None else "field_changed")
    return _input_state_reason(
        field, window_compat, target.get("composition_optional", False)
    )


# 复用已加载的原生桥；布局无法翻译时不把固定物理键当成 V。
def _paste_keycode():
    try:
        import objc

        value = objc.lookUpClass("AwesomemeHotkey").pasteKeyCode()
        return value if 0 <= value <= 127 else None
    except Exception:
        return None


# CGEventPost 不是接收回执；成功仅表示请求发出，不宣称图片已经进入输入框。
def paste_once(target, clipboard_generation):
    import AppKit
    import Quartz

    reason = validate_target(target)
    if reason:
        result = {"status": "copied_only", "reason": reason}
        if reason == "target_changed" and target.get("change_detail"):
            result["detail"] = target["change_detail"]
        return result
    if not Quartz.CGPreflightPostEventAccess():
        return {"status": "copied_only", "reason": "event_permission_required"}
    board = AppKit.NSPasteboard.generalPasteboard()
    if board.changeCount() != clipboard_generation:
        return {"status": "clipboard_changed", "reason": "clipboard_changed"}
    keycode = _paste_keycode()
    if keycode is None:
        return {"status": "copied_only", "reason": "keyboard_layout_unavailable"}
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
    if source is None:
        return {"status": "copied_only", "reason": "event_unavailable"}
    down = Quartz.CGEventCreateKeyboardEvent(source, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(source, keycode, False)
    if down is None or up is None:
        return {"status": "copied_only", "reason": "event_unavailable"}
    # 使用私有事件源，拒绝用户仍按住其他修饰键时投递。
    modifiers = Quartz.CGEventSourceFlagsState(
        Quartz.kCGEventSourceStateCombinedSessionState
    )
    if modifiers & (
        Quartz.kCGEventFlagMaskCommand
        | Quartz.kCGEventFlagMaskControl
        | Quartz.kCGEventFlagMaskAlternate
        | Quartz.kCGEventFlagMaskShift
    ):
        return {"status": "copied_only", "reason": "modifiers_pressed"}
    for event in (down, up):
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
    if board.changeCount() != clipboard_generation:
        return {"status": "clipboard_changed", "reason": "clipboard_changed"}
    # 准备事件期间也可能切换窗口；投递前再核对一次。
    reason = validate_target(target)
    if reason:
        result = {"status": "copied_only", "reason": reason}
        if reason == "target_changed" and target.get("change_detail"):
            result["detail"] = target["change_detail"]
        return result
    # 一次性标记先于投递；异常也不能自动重试。绝不发 Return/Enter。
    target["posted"] = True
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
    return {"status": "paste_requested", "reason": ""}
