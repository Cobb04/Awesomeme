"""P1 合成夹具：不操作用户的剪贴板或聊天窗口。"""

import sys
import types
from pathlib import Path

import pytest
from PIL import Image

from src import clipboard_util
from src.macos_picker import PickerController


# 以独立剪贴板替身走准备与提交两个实际函数。
def _test_copy(path):
    import AppKit

    generation = AppKit.NSPasteboard.generalPasteboard().changeCount()
    try:
        prepared = clipboard_util._prepare_macos_image(path)
        return clipboard_util._commit_macos_image(prepared, generation)
    except Exception:
        return False


@pytest.fixture
def board(monkeypatch, tmp_path):
    # 注入独立剪贴板替身；保留真实图像字节解析和发送缓存逻辑。
    class Item:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            self.data = {}
            return self

        def setData_forType_(self, data, kind):
            self.data[kind] = data
            return True

        def setString_forType_(self, value, kind):
            self.data[kind] = value
            return True

    class Board:
        generation = 10
        contents = ["original clipboard"]
        successful = True

        def changeCount(self):
            return self.generation

        def clearContents(self):
            self.generation += 1
            self.contents = []

        def writeObjects_(self, values):
            if self.successful:
                self.contents = values
            return self.successful

    value = Board()
    appkit = types.SimpleNamespace(
        NSPasteboard=types.SimpleNamespace(generalPasteboard=lambda: value),
        NSPasteboardItem=Item,
        NSPasteboardTypeFileURL="public.file-url",
    )
    foundation = types.SimpleNamespace(
        NSData=types.SimpleNamespace(
            dataWithBytes_length_=lambda payload, length: payload
        )
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setenv("AWESOMEME_TEST_ROOT", str(tmp_path))
    return value


# GIF 必须保留全部字节和帧，文件 URL 指向同样的稳定内容。
def test_gif_copy_preserves_all_frames_and_one_item(board, tmp_path):
    source = tmp_path / "animated.gif"
    Image.new("RGB", (17, 23), "red").save(
        source,
        save_all=True,
        append_images=[Image.new("RGB", (17, 23), "blue")],
        duration=200,
        loop=0,
    )
    payload = source.read_bytes()
    assert _test_copy(source)
    assert len(board.contents) == 1
    data = board.contents[0].data
    assert data["com.compuserve.gif"] == payload
    from urllib.parse import unquote, urlparse

    copied = Path(unquote(urlparse(data["public.file-url"]).path))
    assert copied.read_bytes() == payload
    with Image.open(copied) as image:
        assert image.n_frames == 2 and image.size == (17, 23)


# 写入失败必须返回失败，不能以路径文本冒充图片。
def test_failed_write_is_not_success(board, tmp_path):
    source = tmp_path / "image.png"
    Image.new("RGB", (10, 10)).save(source)
    board.successful = False
    assert not _test_copy(source)


# 准备过程中外部复制必须保留，不能被迟到的图片覆盖。
def test_changed_clipboard_preserved(board, tmp_path, monkeypatch):
    source = tmp_path / "image.png"
    Image.new("RGB", (10, 10)).save(source)
    original = clipboard_util.PILImage.open

    def open_and_change(*args, **kwargs):
        board.generation += 1
        board.contents = ["new user content"]
        return original(*args, **kwargs)

    monkeypatch.setattr(clipboard_util.PILImage, "open", open_and_change)
    assert not _test_copy(source)
    assert board.contents == ["new user content"]


# 不能解码的输入必须在清空剪贴板之前拒绝。
def test_invalid_image_preserves_clipboard(board, tmp_path):
    source = tmp_path / "invalid.png"
    source.write_bytes(b"not an image")
    assert not _test_copy(source)
    assert board.contents == ["original clipboard"]


# 旧会话取消和提交时的程序化失焦都不得关闭新的选择会话。
def test_cancel_respects_generation_and_commit():
    controller = PickerController.__new__(PickerController)
    calls = []
    controller.window = types.SimpleNamespace(hide=lambda: calls.append("hide"))
    controller.session = "new"
    controller.committing = False
    controller._closing_for_commit = False
    controller._target = object()
    controller.cancel("old")
    assert controller.session == "new" and not calls
    controller.committing = True
    controller._closing_for_commit = True
    controller.cancel("new", reason="resign")
    assert controller.session == "new" and not calls
    controller.committing = False
    controller.cancel("new")
    assert controller.session is None and calls == ["hide"]


# 即使用户取消发生在复制之后，排队的粘贴也不能继续。
def test_user_cancel_invalidates_pending_paste(monkeypatch):
    from src import macos_insertion

    controller = PickerController.__new__(PickerController)
    controller.window = types.SimpleNamespace(hide=lambda: None)
    controller.session = "active"
    controller.committing = True
    controller._closing_for_commit = False
    controller._target = {}
    posted = []
    monkeypatch.setattr(
        macos_insertion, "paste_once", lambda *args: posted.append(args)
    )
    controller.cancel("active")
    controller._finish("active", 12)
    assert not posted


# 取消后到达的图片准备结果不得写入剪贴板，也不能关闭新面板。
def test_cancel_during_prepare_rejects_late_commit(board, monkeypatch):
    controller = PickerController.__new__(PickerController)
    hidden = []
    controller.window = types.SimpleNamespace(hide=lambda: hidden.append(True))
    controller.session = "old"
    controller.committing = True
    controller._closing_for_commit = False
    controller._target = {}
    writes = []
    monkeypatch.setattr(
        clipboard_util, "_commit_macos_image", lambda *args: writes.append(args)
    )
    controller.cancel("old")
    controller.session = "new"
    controller.committing = True
    controller._commit("old", 10, object())
    assert not writes
    assert board.contents == ["original clipboard"]
    assert controller.session == "new" and hidden == [True]


# 即使有权限且选区为空，未验证的组合态仍禁止真实入口发出事件。
def test_capture_unknown_composition_never_posts(monkeypatch):
    from src import macos_insertion as insertion

    running = types.SimpleNamespace(
        bundleIdentifier=lambda: "com.tencent.WeWorkMac", processIdentifier=lambda: 7
    )
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        types.SimpleNamespace(
            NSWorkspace=types.SimpleNamespace(
                sharedWorkspace=lambda: types.SimpleNamespace(
                    frontmostApplication=lambda: running
                )
            )
        ),
    )
    posted = []
    monkeypatch.setitem(
        sys.modules,
        "ApplicationServices",
        types.SimpleNamespace(
            AXIsProcessTrusted=lambda: True,
            AXUIElementCreateApplication=lambda pid: "application",
            AXUIElementSetMessagingTimeout=lambda *args: 0,
            kAXErrorSuccess=0,
            CGEventPost=lambda *args: posted.append(args),
        ),
    )
    attributes = {
        ("application", "AXFocusedWindow"): "window",
        ("application", "AXFocusedUIElement"): "field",
        ("field", "AXRole"): "AXTextArea",
    }
    monkeypatch.setattr(
        insertion, "_attribute", lambda item, key: attributes.get((item, key))
    )
    monkeypatch.setattr(insertion, "_empty_selection", lambda field: True)
    target = insertion.capture_target()
    assert insertion.paste_once(target, 10) == {
        "status": "copied_only",
        "reason": "composition_state_unverified",
    }
    assert not posted


# 真实插入入口只发一次 Cmd+V，不包含 Enter；其他前置失败都不发事件。
@pytest.mark.parametrize(
    "failure", ["", "target_changed", "clipboard", "modifier", "permission", "layout"]
)
def test_paste_once_or_safe_fallback(monkeypatch, failure):
    from src import macos_insertion as insertion

    monkeypatch.setattr(
        insertion, "_paste_keycode", lambda: None if failure == "layout" else 9
    )

    posted = []
    quartz = types.SimpleNamespace(
        kCGEventSourceStatePrivate=1,
        kCGEventSourceStateCombinedSessionState=2,
        kCGEventFlagMaskCommand=1,
        kCGEventFlagMaskControl=2,
        kCGEventFlagMaskAlternate=4,
        kCGEventFlagMaskShift=8,
        kCGHIDEventTap=0,
        CGEventSourceCreate=lambda _: object(),
        CGEventCreateKeyboardEvent=lambda source, key, down: {"key": key, "down": down},
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
        CGEventSourceFlagsState=lambda _: 1 if failure == "modifier" else 0,
        CGEventPost=lambda tap, event: posted.append(event),
        CGPreflightPostEventAccess=lambda: failure != "permission",
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    clipboard = types.SimpleNamespace(
        changeCount=lambda: 13 if failure == "clipboard" else 12
    )
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        types.SimpleNamespace(
            NSPasteboard=types.SimpleNamespace(generalPasteboard=lambda: clipboard)
        ),
    )
    monkeypatch.setattr(
        insertion,
        "validate_target",
        lambda target: (
            "already_requested"
            if target.get("posted")
            else ("target_changed" if failure == "target_changed" else "")
        ),
    )
    target = {"posted": False}
    result = insertion.paste_once(target, 12)
    insertion.paste_once(target, 12)
    if failure:
        assert posted == []
        assert result["status"] in ("copied_only", "clipboard_changed")
    else:
        assert result["status"] == "paste_requested"
        assert posted == [
            {"key": 9, "down": True, "flags": 1},
            {"key": 9, "down": False, "flags": 1},
        ]


# 空组词范围才为 clear；未公开属性为 unsupported，读取或解码失败为 unknown。
@pytest.mark.parametrize(
    "advertised,error,length,decoded,expected",
    [
        (True, 0, 0, True, "clear"),
        (True, 0, 2, True, "marked"),
        (True, 0, -1, True, "unknown"),
        (True, 0, 0, False, "unknown"),
        (True, 1, 0, True, "clear"),
        (True, 2, 0, True, "unknown"),
        (False, 1, 0, True, "unsupported"),
    ],
)
def test_composition_attribute_states(
    monkeypatch, advertised, error, length, decoded, expected
):
    from src import macos_insertion as insertion

    reads = []

    def read(element, name, out):
        reads.append(name)
        return error, object()

    quartz = types.SimpleNamespace(
        kAXErrorSuccess=0,
        kAXErrorNoValue=1,
        kAXValueCFRangeType=3,
        AXUIElementCopyAttributeNames=lambda *args: (
            0,
            ["AXTextInputMarkedRange"] if advertised else [],
        ),
        AXUIElementCopyAttributeValue=read,
        AXValueGetValue=lambda *args: (decoded, (0, length)),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", quartz)
    assert insertion._composition_state("field") == expected
    assert set(reads) <= {"AXTextInputMarkedRange"}


# 空选区必须有有效光标位置；NotFound 不能证明可以安全粘贴。
@pytest.mark.parametrize(
    "location,length,expected",
    [(0, 0, True), (3, 0, True), (-1, 0, False), (2**63 - 1, 0, False), (0, 2, False)],
)
def test_empty_selection_requires_valid_caret(monkeypatch, location, length, expected):
    from src import macos_insertion as insertion

    monkeypatch.setattr(insertion, "_attribute", lambda *args: object())
    monkeypatch.setitem(
        sys.modules,
        "ApplicationServices",
        types.SimpleNamespace(
            kAXValueCFRangeType=3,
            AXValueGetValue=lambda *args: (
                True,
                (location, length),
            ),
        ),
    )
    assert insertion._empty_selection("field") == expected


# 输入状态在面板收起后必须再次读取；原窗口、控件和进程也必须一致。
@pytest.mark.parametrize(
    "changed", ["none", "composition", "selection", "window", "field", "process"]
)
def test_validate_rechecks_captured_input(monkeypatch, changed):
    from src import macos_insertion as insertion

    running = types.SimpleNamespace(
        bundleIdentifier=lambda: "com.tencent.xinWeChat",
        processIdentifier=lambda: 7,
        launchDate=lambda: 100,
        isTerminated=lambda: False,
    )
    current = [running]
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        types.SimpleNamespace(
            NSWorkspace=types.SimpleNamespace(
                sharedWorkspace=lambda: types.SimpleNamespace(
                    frontmostApplication=lambda: current[0]
                )
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "ApplicationServices",
        types.SimpleNamespace(
            AXIsProcessTrusted=lambda: True,
            AXUIElementCreateApplication=lambda pid: "application",
            AXUIElementSetMessagingTimeout=lambda *args: 0,
            kAXErrorSuccess=0,
        ),
    )
    attributes = {
        ("application", "AXFocusedWindow"): "window",
        ("application", "AXFocusedUIElement"): "field",
        ("field", "AXRole"): "AXTextArea",
    }
    monkeypatch.setattr(
        insertion, "_attribute", lambda item, key: attributes.get((item, key))
    )
    monkeypatch.setattr(insertion, "_empty_selection", lambda field: True)
    monkeypatch.setattr(insertion, "_composition_state", lambda field: "clear")
    target = insertion.capture_target()
    assert target["reason"] == ""
    if changed == "composition":
        monkeypatch.setattr(insertion, "_composition_state", lambda field: "marked")
    elif changed == "selection":
        monkeypatch.setattr(insertion, "_empty_selection", lambda field: False)
        monkeypatch.setattr(insertion, "_selection_state", lambda field: "selected")
    elif changed == "window":
        attributes[("application", "AXFocusedWindow")] = "other"
    elif changed == "field":
        attributes[("application", "AXFocusedUIElement")] = "other"
    elif changed == "process":
        current[0] = types.SimpleNamespace(processIdentifier=lambda: 8)
    expected = {
        "none": "",
        "composition": "composition_active",
        "selection": "selection_not_empty_or_unknown",
    }.get(changed, "target_changed")
    assert insertion.validate_target(target) == expected
    if changed in ("window", "field", "process"):
        assert (
            target["change_detail"]
            == {
                "window": "window_changed",
                "field": "field_changed",
                "process": "other_app_active",
            }[changed]
        )


@pytest.fixture
def library(tmp_path):
    from src.database import MemeDB
    from src.macos_picker import PickerLibrary

    db = MemeDB(tmp_path / "library.sqlite")
    cache = tmp_path / "images"
    cache.mkdir()
    host = types.SimpleNamespace(
        _cfg=types.SimpleNamespace(cache_dir=cache),
        _api=types.SimpleNamespace(
            _db=db,
            _find_meme_file=lambda name: cache / name,
            _get_collection_ids_recursive=lambda cid: [cid],
        ),
    )
    return PickerLibrary(host)


# 实际数据库分页保留排序，混排图片并复用标签、分组和最近使用。
def test_library_pages_search_groups_and_recent(library):
    db = library.db
    ids = [
        db.add_meme(
            f"{n}.gif" if n % 2 else f"{n}.png",
            file_hash=f"hash{n}",
            tags=["猫"] if n == 1 else [],
        )
        for n in range(51)
    ]
    group = db.create_collection("微信")
    db.add_to_collection(ids[1], group)
    rows = db.search(limit=51)
    first, second = library.page("", None, 0), library.page("", None, 48)
    assert first["total"] == second["total"] == 51
    assert [r["id"] for r in first["items"] + second["items"]] == [
        r["id"] for r in rows
    ]
    assert len(first["items"]) == 48 and len(second["items"]) == 3
    assert {r["preview"].split("/")[2] for r in first["items"]} == {"thumb", "original"}
    assert library.page("猫", None, 0)["items"][0]["id"] == ids[1]
    assert library.page("", group, 0)["total"] == 1
    assert library.page("", group + 1, 0)["total"] == 0
    db.record_use(ids[4])
    assert library.page("", -3, 0)["items"][0]["id"] == ids[4]
    assert library.groups() == [{"id": group, "name": "微信"}]
    for value in (-1, 0, True, "1"):
        with pytest.raises(ValueError):
            library.page("", value, 0)


# 文件被删除、替换或指向图库之外时拒绝提交。
def test_library_preparation_revalidates_file(library, tmp_path, monkeypatch):
    import hashlib

    path = library.host._cfg.cache_dir / "x.png"
    Image.new("RGB", (9, 7), "red").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    meme_id = library.db.add_meme(path.name, file_hash=digest)
    monkeypatch.setattr(
        clipboard_util,
        "_prepare_macos_image",
        lambda p: {"payload": Path(p).read_bytes()},
    )
    assert library.prepare(meme_id, digest)["payload"] == path.read_bytes()
    path.write_bytes(b"replaced")
    with pytest.raises(ValueError, match="image_changed"):
        library.prepare(meme_id, digest)
    path.unlink()
    with pytest.raises(ValueError, match="image_missing"):
        library.prepare(meme_id, digest)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="image_missing"):
        library.prepare(meme_id, digest)


# 重复点击只排队一次；新查询使旧页失效，旧会话不能提交。
def test_picker_api_gates_queries_and_commit(library, monkeypatch):
    import threading
    from src.macos_picker import PickerApi

    meme_id = library.db.add_meme("x.gif", file_hash="hash")
    queued = []
    monkeypatch.setitem(
        sys.modules,
        "PyObjCTools",
        types.SimpleNamespace(
            AppHelper=types.SimpleNamespace(callAfter=lambda *args: queued.append(args))
        ),
    )
    c = types.SimpleNamespace(
        session="new",
        committing=False,
        _request=0,
        _choices={},
        _query_lock=threading.RLock(),
        library=library,
        choose=lambda *args: None,
    )
    api = PickerApi(c)
    assert api.search("old", 1)["status"] == "stale"
    assert api.search("new", 1)["status"] == "ok"
    assert api.search("new", 2, "missing")["items"] == []
    assert api.choose("new", 1, meme_id)["status"] == "invalid_selection"
    assert api.choose("new", 2, meme_id)["status"] == "invalid_selection"
    assert api.search("new", 3)["status"] == "ok"
    assert api.choose("new", 3, meme_id)["status"] == "preparing"
    assert api.choose("new", 3, meme_id)["status"] == "invalid_selection"
    assert len(queued) == 1
    assert api.search("new", 4)["status"] == "stale"


# 关闭面板后返回的慢查询不能激活旧图片。
def test_picker_api_drops_late_query(library):
    import threading
    from src.macos_picker import PickerApi

    c = types.SimpleNamespace(
        session="new",
        committing=False,
        _request=0,
        _choices={},
        _query_lock=threading.RLock(),
        library=library,
    )
    original = library.page

    def cancel_during_read(*args):
        c.session = None
        return original(*args)

    library.page = cancel_during_read
    assert PickerApi(c).search("new", 1)["status"] == "stale"
    assert not c._choices


# 微信只公开窗口时，兼容路线应抵达粘贴入口；模拟 AX，不操作真实窗口。
@pytest.mark.parametrize("focus", [None, "window"])
def test_wechat_opaque_window_can_request_paste(monkeypatch, focus):
    from src import macos_insertion as insertion

    running = types.SimpleNamespace(
        bundleIdentifier=lambda: "com.tencent.xinWeChat",
        processIdentifier=lambda: 7,
        launchDate=lambda: 100,
        isTerminated=lambda: False,
    )
    current = [running]
    attrs = {
        ("application", "AXFocusedWindow"): "window",
        ("application", "AXFocusedUIElement"): focus,
        ("window", "AXRole"): "AXWindow",
    }
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        types.SimpleNamespace(
            NSWorkspace=types.SimpleNamespace(
                sharedWorkspace=lambda: types.SimpleNamespace(
                    frontmostApplication=lambda: current[0]
                )
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "ApplicationServices",
        types.SimpleNamespace(
            AXIsProcessTrusted=lambda: True,
            AXUIElementCreateApplication=lambda pid: "application",
            AXUIElementSetMessagingTimeout=lambda *args: 0,
            kAXErrorSuccess=0,
        ),
    )
    monkeypatch.setattr(insertion, "_attribute", lambda obj, key: attrs.get((obj, key)))
    target = insertion.capture_target()
    assert target["reason"] == ""
    assert target["mode"] == "window"
    assert insertion.input_anchor(target) is None
    assert insertion.validate_target(target) == ""
    posted = []
    quartz = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    for name, value in dict(
        kCGEventSourceStatePrivate=1,
        kCGEventSourceStateCombinedSessionState=2,
        kCGEventFlagMaskCommand=1,
        kCGEventFlagMaskControl=2,
        kCGEventFlagMaskAlternate=4,
        kCGEventFlagMaskShift=8,
        kCGHIDEventTap=0,
        CGPreflightPostEventAccess=lambda: True,
        CGEventSourceCreate=lambda _: object(),
        CGEventCreateKeyboardEvent=lambda source, key, down: {"key": key, "down": down},
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
        CGEventSourceFlagsState=lambda _: 0,
        CGEventPost=lambda tap, event: posted.append(event),
    ).items():
        setattr(quartz, name, value)
    sys.modules["AppKit"].NSPasteboard = types.SimpleNamespace(
        generalPasteboard=lambda: types.SimpleNamespace(changeCount=lambda: 10)
    )
    monkeypatch.setattr(insertion, "_paste_keycode", lambda: 9)
    assert insertion.paste_once(target, 10)["status"] == "paste_requested"
    assert insertion.paste_once(target, 10)["reason"] == "already_requested"
    assert posted == [
        {"key": 9, "down": True, "flags": 1},
        {"key": 9, "down": False, "flags": 1},
    ]
    target["posted"] = False
    attrs[("application", "AXFocusedUIElement")] = "search"
    attrs[("search", "AXRole")] = "AXTextField"
    attrs[("search", "AXSubrole")] = "AXSearchField"
    assert insertion.validate_target(target) == "input_not_identified"
    attrs[("application", "AXFocusedUIElement")] = focus
    attrs[("application", "AXFocusedWindow")] = "other"
    assert insertion.validate_target(target) == "target_changed"
    attrs[("application", "AXFocusedWindow")] = "window"
    current[0] = types.SimpleNamespace(processIdentifier=lambda: 8)
    assert insertion.validate_target(target) == "target_changed"


# 已知搜索/密码框、选中文字、正在组词仍不粘贴；仅未知状态兼容。
@pytest.mark.parametrize(
    "role,subrole,selection,composition,expected",
    [
        (None, None, "unknown", "unknown", ""),
        ("AXTextArea", None, "unknown", "unknown", ""),
        ("AXTextArea", None, "empty", "unknown", ""),
        ("AXTextArea", None, "selected", "unknown", "selection_not_empty_or_unknown"),
        ("AXTextArea", None, "empty", "marked", "composition_active"),
        ("AXTextField", "AXSearchField", "empty", "clear", "input_not_identified"),
        ("AXTextField", "AXSecureTextField", "empty", "clear", "input_not_identified"),
        ("AXButton", None, "unknown", "unknown", "input_not_identified"),
    ],
)
def test_window_compat_known_input_guards(
    monkeypatch, role, subrole, selection, composition, expected
):
    from src import macos_insertion as insertion

    attrs = {"AXRole": role, "AXSubrole": subrole}
    monkeypatch.setattr(insertion, "_attribute", lambda obj, key: attrs.get(key))
    monkeypatch.setattr(insertion, "_empty_selection", lambda obj: selection == "empty")
    monkeypatch.setattr(insertion, "_selection_state", lambda obj: selection)
    monkeypatch.setattr(insertion, "_composition_state", lambda obj: composition)
    assert insertion._input_state_reason("field", window_compat=True) == expected


# 真正的 PyObjC 原生值往返；不创建应用对象，不读用户窗口，不发事件。
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native bridge")
def test_native_ax_range_uses_real_framework(monkeypatch):
    import ApplicationServices as AX
    from src import macos_insertion as insertion

    for name in (
        "AXIsProcessTrusted",
        "AXUIElementCreateApplication",
        "AXUIElementSetMessagingTimeout",
        "AXUIElementCopyAttributeValue",
        "AXUIElementCopyAttributeNames",
        "AXValueGetValue",
    ):
        assert callable(getattr(AX, name))
    value = AX.AXValueCreate(AX.kAXValueCFRangeType, (4, 0))
    monkeypatch.setattr(insertion, "_attribute", lambda *args: value)
    assert insertion._selection_state("synthetic_field") == "empty"
    value = AX.AXValueCreate(AX.kAXValueCFRangeType, (4, 2))
    assert insertion._selection_state("synthetic_field") == "selected"


# 组合态和几何也用真实 AXValue；只替换窗口查询边界，避免触及用户界面。
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native bridge")
def test_native_ax_composition_and_geometry(monkeypatch):
    import ApplicationServices as AX
    from src import macos_insertion as insertion

    marked = AX.AXValueCreate(AX.kAXValueCFRangeType, (0, 2))
    monkeypatch.setattr(
        AX,
        "AXUIElementCopyAttributeNames",
        lambda *args: (0, ["AXTextInputMarkedRange"]),
    )
    monkeypatch.setattr(AX, "AXUIElementCopyAttributeValue", lambda *args: (0, marked))
    assert insertion._composition_state("synthetic") == "marked"
    marked = AX.AXValueCreate(AX.kAXValueCFRangeType, (0, 0))
    assert insertion._composition_state("synthetic") == "clear"
    values = {
        "AXRole": "AXTextArea",
        "AXPosition": AX.AXValueCreate(AX.kAXValueCGPointType, (120, 400)),
        "AXSize": AX.AXValueCreate(AX.kAXValueCGSizeType, (320, 80)),
    }
    monkeypatch.setattr(insertion, "_attribute", lambda field, name: values.get(name))
    frame = types.SimpleNamespace(
        origin=types.SimpleNamespace(y=0), size=types.SimpleNamespace(height=1000)
    )
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        types.SimpleNamespace(
            NSScreen=types.SimpleNamespace(
                screens=lambda: [types.SimpleNamespace(frame=lambda: frame)]
            ),
            NSMakePoint=lambda x, y: (x, y),
        ),
    )
    assert insertion.input_anchor({"field": "synthetic"}) == (120, 600)


# 飞书缺少组词属性时走原输入框；真实 AXValue 解码，查询和按键边界隔离。
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native bridge")
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("unsupported", ""),
        ("timeout", "composition_state_unverified"),
        ("invalid", "composition_state_unverified"),
        ("marked", "composition_active"),
        ("selected", "selection_not_empty_or_unknown"),
        ("unknown_selection", "selection_not_empty_or_unknown"),
        ("opaque", "input_not_identified"),
        ("search", "input_not_identified"),
        ("secure", "input_not_identified"),
        ("wecom", "composition_state_unverified"),
        ("field_changed", "target_changed"),
        ("window_changed", "target_changed"),
        ("late_marked", "composition_active"),
        ("late_timeout", "composition_state_unverified"),
    ],
)
def test_feishu_missing_composition_attribute(monkeypatch, scenario, expected):
    import ApplicationServices as AX
    from src import macos_insertion as insertion

    state = [scenario]
    running = types.SimpleNamespace(
        bundleIdentifier=lambda: (
            "com.tencent.WeWorkMac" if scenario == "wecom" else "com.electron.lark"
        ),
        processIdentifier=lambda: 7,
        launchDate=lambda: 100,
        isTerminated=lambda: False,
    )
    attributes = {
        ("app", "AXFocusedWindow"): "window",
        ("app", "AXFocusedUIElement"): "field",
        ("field", "AXRole"): "AXGroup" if scenario == "opaque" else "AXTextArea",
        ("field", "AXSubrole"): {
            "search": "AXSearchField",
            "secure": "AXSecureTextField",
        }.get(scenario),
        ("field", "AXSelectedTextRange"): AX.AXValueCreate(
            AX.kAXValueCFRangeType, (0, 2 if scenario == "selected" else 0)
        ),
    }
    if scenario == "unknown_selection":
        attributes.pop(("field", "AXSelectedTextRange"))
    marked = AX.AXValueCreate(AX.kAXValueCFRangeType, (0, 2))

    def names(*args):
        if state[0] == "timeout":
            return AX.kAXErrorCannotComplete, None
        return 0, (
            ["AXTextInputMarkedRange"] if state[0] in ("marked", "invalid") else []
        )

    def read(obj, name, out):
        if name == "AXTextInputMarkedRange":
            return (0, marked) if state[0] == "marked" else (0, None)
        value = attributes.get((obj, name))
        return (
            (0, value) if value is not None else (AX.kAXErrorAttributeUnsupported, None)
        )

    monkeypatch.setattr(AX, "AXIsProcessTrusted", lambda: True)
    monkeypatch.setattr(AX, "AXUIElementCreateApplication", lambda pid: "app")
    monkeypatch.setattr(AX, "AXUIElementSetMessagingTimeout", lambda *args: 0)
    monkeypatch.setattr(AX, "AXUIElementCopyAttributeNames", names)
    monkeypatch.setattr(AX, "AXUIElementCopyAttributeValue", read)
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        types.SimpleNamespace(
            NSWorkspace=types.SimpleNamespace(
                sharedWorkspace=lambda: types.SimpleNamespace(
                    frontmostApplication=lambda: running
                )
            ),
            NSPasteboard=types.SimpleNamespace(
                generalPasteboard=lambda: types.SimpleNamespace(changeCount=lambda: 10)
            ),
        ),
    )
    posted = []
    monkeypatch.setitem(
        sys.modules,
        "Quartz",
        types.SimpleNamespace(
            kCGEventSourceStatePrivate=1,
            kCGEventSourceStateCombinedSessionState=2,
            kCGEventFlagMaskCommand=1,
            kCGEventFlagMaskControl=2,
            kCGEventFlagMaskAlternate=4,
            kCGEventFlagMaskShift=8,
            kCGHIDEventTap=0,
            CGPreflightPostEventAccess=lambda: True,
            CGEventSourceCreate=lambda _: object(),
            CGEventCreateKeyboardEvent=lambda source, key, down: {
                "key": key,
                "down": down,
            },
            CGEventSetFlags=lambda event, flags: event.update(flags=flags),
            CGEventSourceFlagsState=lambda _: 0,
            CGEventPost=lambda tap, event: posted.append(event),
        ),
    )
    monkeypatch.setattr(insertion, "_paste_keycode", lambda: 9)
    target = insertion.capture_target()
    assert target["mode"] == "field"
    if scenario in ("field_changed", "window_changed"):
        name = (
            "AXFocusedUIElement" if scenario == "field_changed" else "AXFocusedWindow"
        )
        attributes[("app", name)] = "other"
    elif scenario.startswith("late_"):
        state[0] = scenario.removeprefix("late_")
    result = insertion.paste_once(target, 10)
    assert result["reason"] == expected
    if expected:
        assert result["status"] == "copied_only" and not posted
    else:
        assert result["status"] == "paste_requested"
        assert [(e["key"], e["down"]) for e in posted] == [(9, True), (9, False)]
        assert insertion.paste_once(target, 10)["reason"] == "already_requested"
        assert len(posted) == 2


# 从真实图库走到剪贴板提交，只有飞书静态图缩小，源文件始终不变。
@pytest.mark.parametrize(
    "bundle,size,mode,expected",
    [
        ("com.electron.lark", (400, 200), "RGB", (120, 60)),
        ("com.electron.lark", (200, 400), "RGBA", (60, 120)),
        ("com.electron.lark", (60, 80), "RGBA", (60, 80)),
        ("com.tencent.xinWeChat", (400, 200), "RGB", (400, 200)),
        ("com.tencent.WeWorkMac", (400, 200), "RGB", (400, 200)),
        (None, (400, 200), "RGB", (400, 200)),
    ],
)
def test_feishu_static_library_to_clipboard(
    board, library, bundle, size, mode, expected
):
    import hashlib
    import io
    from urllib.parse import unquote, urlparse

    source = library.host._cfg.cache_dir / "sample.png"
    Image.new(mode, size, (240, 100, 20, 80) if mode == "RGBA" else "red").save(source)
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    meme_id = library.db.add_meme(source.name, file_hash=digest)
    prepared = library.prepare(meme_id, digest, bundle)
    assert clipboard_util._commit_macos_image(prepared, board.changeCount())
    assert len(board.contents) == 1
    data = board.contents[0].data
    copied = Path(unquote(urlparse(data["public.file-url"]).path)).read_bytes()
    assert copied == data[prepared["type"]] == prepared["payload"]
    with Image.open(io.BytesIO(copied)) as image:
        assert image.size == expected
        if mode == "RGBA":
            assert image.convert("RGBA").getpixel((0, 0))[3] == 80
    if size == expected:
        assert copied == original
    else:
        assert copied != original
    assert source.read_bytes() == original
    assert library.db.get_by_id(meme_id)["file_hash"] == digest


# 飞书 GIF 等比缩小，其他客户端保持原字节；短帧时长和循环语义不变。
@pytest.mark.parametrize(
    "bundle", ["com.electron.lark", "com.tencent.xinWeChat", "com.tencent.WeWorkMac"]
)
@pytest.mark.parametrize("loop", [None, 0, 3])
def test_feishu_animated_library_preserves_bytes(board, library, bundle, loop):
    import hashlib
    import io

    source = library.host._cfg.cache_dir / "animation.gif"
    Image.new("RGBA", (240, 180), "red").save(
        source,
        save_all=True,
        append_images=[Image.new("RGBA", (240, 180), "blue")],
        duration=[10, 170],
        **({"loop": loop} if loop is not None else {}),
        disposal=2,
    )
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    meme_id = library.db.add_meme(source.name, file_hash=digest)
    prepared = library.prepare(meme_id, digest, bundle)
    assert clipboard_util._commit_macos_image(prepared, board.changeCount())
    payload = board.contents[0].data["com.compuserve.gif"]
    assert source.read_bytes() == original
    expected_size = (120, 90) if bundle == "com.electron.lark" else (240, 180)
    assert (payload == original) == (bundle != "com.electron.lark")
    assert "media_note" not in prepared
    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == expected_size and image.n_frames == 2
        assert image.info.get("loop") == loop
        durations = []
        for index in range(image.n_frames):
            image.seek(index)
            durations.append(image.info["duration"])
        assert durations == [10, 170]


# 转换失败不得悄悄提交未缩小原图；源文件被替换仍须先拒绝。
@pytest.mark.parametrize("failure", ["unchanged", "invalid_size", "source_changed"])
def test_feishu_static_conversion_failure(board, library, monkeypatch, failure):
    import hashlib

    source = library.host._cfg.cache_dir / "sample.png"
    Image.new("RGB", (400, 400), "red").save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    meme_id = library.db.add_meme(source.name, file_hash=digest)
    if failure == "source_changed":
        Image.new("RGB", (400, 400), "blue").save(source)
    elif failure == "unchanged":
        monkeypatch.setattr(
            clipboard_util, "convert_image_mode_1", lambda p, *a, **kw: p
        )
    else:
        invalid = source.parent / "wrong.jpg"
        Image.new("RGB", (200, 200)).save(invalid)
        monkeypatch.setattr(
            clipboard_util, "convert_image_mode_1", lambda *a, **kw: str(invalid)
        )
    with pytest.raises(ValueError, match="image_changed|image_conversion_failed"):
        library.prepare(meme_id, digest, "com.electron.lark")
    assert board.contents == ["original clipboard"]


# 转换只使用校验后的稳定副本，不使用随后变动的图库原文件。
def test_feishu_static_uses_validated_snapshot(board, library, monkeypatch):
    import hashlib
    import io

    source = library.host._cfg.cache_dir / "sample.png"
    Image.new("RGB", (400, 400), "red").save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    meme_id = library.db.add_meme(source.name, file_hash=digest)
    original = clipboard_util.convert_image_mode_1

    def change_source(path, *args, **kwargs):
        assert Path(path) != source
        Image.new("RGB", (400, 400), "blue").save(source)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(clipboard_util, "convert_image_mode_1", change_source)
    prepared = library.prepare(meme_id, digest, "com.electron.lark")
    with Image.open(io.BytesIO(prepared["payload"])) as image:
        red, green, blue = image.getpixel((60, 60))
        assert red > 240 and green < 10 and blue < 10


# 后台准备捕获本次目标，不能因用户随后切换会话而套用另一客户端策略。
def test_choose_snapshots_target_bundle(board, monkeypatch):
    import threading

    pending, prepared_calls = [], []
    monkeypatch.setattr(
        threading,
        "Thread",
        lambda target, **kw: types.SimpleNamespace(
            start=lambda: pending.append(target)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "PyObjCTools",
        types.SimpleNamespace(
            AppHelper=types.SimpleNamespace(callAfter=lambda *args: None)
        ),
    )
    controller = PickerController.__new__(PickerController)
    controller.session = "active"
    controller.committing = True
    controller._target = {"bundle_id": "com.electron.lark"}
    controller.library = types.SimpleNamespace(
        prepare=lambda *args: prepared_calls.append(args)
    )
    controller.choose("active", 5, "digest")
    controller._target = {"bundle_id": "com.tencent.xinWeChat"}
    pending[0]()
    assert prepared_calls == [(5, "digest", "com.electron.lark")]


# 缩小透明 GIF 后，每帧清空后的区域不能留下上一帧的色块。
def test_feishu_gif_transparency_and_disposal(board, library):
    import hashlib
    import io
    from PIL import ImageDraw

    source = library.host._cfg.cache_dir / "transparent.gif"
    frames = []
    for x in (20, 150, 70):
        frame = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rectangle((x, 40, x + 40, 100), fill="red")
        frames.append(frame)
    frames[0].save(
        source,
        save_all=True,
        append_images=frames[1:],
        duration=[0, 10, 170],
        disposal=2,
        loop=2,
    )
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    meme_id = library.db.add_meme(source.name, file_hash=digest)
    prepared = library.prepare(meme_id, digest, "com.electron.lark")
    assert "media_note" not in prepared
    with Image.open(io.BytesIO(prepared["payload"])) as image:
        assert image.size == (120, 120) and image.n_frames == 3
        assert image.info["loop"] == 2
        for index, x in enumerate((20, 150, 70)):
            image.seek(index)
            rgba = image.convert("RGBA")
            assert rgba.getpixel((0, 0))[3] == 0
            assert rgba.getpixel((x // 2 + 10, 35))[3] == 255
            assert image.info["duration"] == [0, 10, 170][index]
        image.seek(1)
        assert image.convert("RGBA").getpixel((20, 35))[3] == 0
    assert source.read_bytes() == original


# 缺失时长、动画格式尚未验证、小图均保留原始数据。
@pytest.mark.parametrize("scenario", ["small", "webp", "missing_duration"])
def test_feishu_animation_preserved_cases(board, tmp_path, scenario):
    size = (60, 80) if scenario == "small" else (240, 240)
    path = tmp_path / ("input.webp" if scenario == "webp" else "input.gif")
    options = (
        {} if scenario == "missing_duration" else {"duration": [40, 80], "loop": 0}
    )
    Image.new("RGB", size, "red").save(
        path, save_all=True, append_images=[Image.new("RGB", size, "blue")], **options
    )
    original = clipboard_util._prepare_macos_image(path)
    prepared = clipboard_util._prepare_feishu_image(original)
    assert prepared["payload"] == original["payload"]
    assert (prepared.get("media_note") == "animation_original") == (scenario != "small")


# 转换异常、静态化、循环/时序变化都必须回退完整原图，并附带原尺寸提示。
@pytest.mark.parametrize(
    "failure", ["exception", "static", "timing", "loop", "transparent"]
)
def test_feishu_animation_validation_fallback(board, tmp_path, monkeypatch, failure):
    source = tmp_path / "source.gif"
    Image.new("RGB", (240, 240), "red").save(
        source,
        save_all=True,
        append_images=[Image.new("RGB", (240, 240), "blue")],
        duration=[40, 80],
        loop=2,
    )
    candidate = tmp_path / "wrong.gif"
    frames = [
        Image.new(
            "RGBA", (120, 120), (255, 0, 0, 0) if failure == "transparent" else "red"
        ),
        Image.new("RGBA", (120, 120), "blue"),
    ]
    frames[0].save(
        candidate,
        save_all=True,
        append_images=[] if failure == "static" else frames[1:],
        duration=[100, 100] if failure == "timing" else [40, 80],
        loop=0 if failure == "loop" else 2,
        disposal=2,
    )

    def convert(*args, **kwargs):
        if failure == "exception":
            raise OSError("synthetic failure")
        return str(candidate)

    monkeypatch.setattr(clipboard_util, "_animated_webp_to_gif", convert)
    original = clipboard_util._prepare_macos_image(source)
    prepared = clipboard_util._prepare_feishu_image(original)
    assert prepared["payload"] == original["payload"]
    assert prepared["media_note"] == "animation_original"
    assert clipboard_util._commit_macos_image(prepared, board.changeCount())
    assert board.contents[0].data["com.compuserve.gif"] == source.read_bytes()
