"""Offline, allowlisted protobuf codec for Feishu PullStickers (command 103).

No authentication, network, filesystem reads, third-party imports, or code from
the researched projects runs here. Fields are pinned to public CDN schemas:
  8.3c339834.js: improto.Packet, module 32468 (status defaults to 200, NOT 0)
  4899.735af5d9.js: sticker.PullStickersRequest/Response and sticker.Sticker
  1181.aa6c8a55.js: entities.Image (module 95284) / ImageSet (module 89558)
See ../deep/protocol-evidence/ and ../deep/protocol.md for source provenance.

The output contains only allowlisted metadata. Crypto messages and secure keys
are never decoded or returned. Their presence only sets ``encrypted=True``.
Image ``type`` is a derived safety label, not an Image protobuf enum field.
"""

from collections import Counter
from typing import Iterator, Tuple, Union

__all__ = ["build_pull", "decode_pull", "safe_envelope_summary", "StickerWireError"]

COMMAND = 103
MAX_COUNT = 1000
MAX_PACKET_BYTES = 16 * 1024 * 1024
MAX_STICKER_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 64 * 1024
MAX_TEXT_BYTES = 4096
MAX_FIELDS_PER_MESSAGE = 16384

VARIANTS = {
    2: "origin",
    3: "thumbnail",
    5: "middle",
    6: "thumbnailWebp",
    7: "middleWebp",
    8: "middleMp4",
    9: "cover",
    10: "intact",
}


class StickerWireError(ValueError):
    """Fixed error code; never embeds server messages, bytes, or identifiers."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str):
    raise StickerWireError(code)


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _uint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _blob(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def build_pull(count: int, cid: str) -> bytes:
    """Build ONLY cmd 103, position 0, count 1..1000, protobuf payload.

    ``cid`` is a local request ID. It is never logged. No other command or
    arbitrary protobuf payload can be supplied through this interface.
    """
    if type(count) is not int or not 1 <= count <= MAX_COUNT:
        _fail("invalid_count")
    if not isinstance(cid, str) or not 1 <= len(cid) <= 128:
        _fail("invalid_cid")
    # Restrict local IDs to printable ASCII without spaces/control characters.
    if any(ord(c) < 33 or ord(c) > 126 for c in cid):
        _fail("invalid_cid")
    request = _uint(1, 0) + _uint(2, count)
    return (
        _uint(2, 1)
        + _uint(3, COMMAND)
        + _blob(5, request)
        + _blob(6, cid.encode("ascii"))
    )


def _read_varint(data: memoryview, offset: int) -> Tuple[int, int]:
    value = 0
    for index in range(10):
        if offset >= len(data):
            _fail("truncated_varint")
        byte = data[offset]
        offset += 1
        if index == 9 and byte > 1:
            _fail("varint_overflow")
        value |= (byte & 127) << (index * 7)
        if not byte & 128:
            return value, offset
    _fail("varint_overflow")


def _fields(data: memoryview) -> Iterator[Tuple[int, int, Union[int, memoryview]]]:
    offset = 0
    field_count = 0
    while offset < len(data):
        field_count += 1
        if field_count > MAX_FIELDS_PER_MESSAGE:
            _fail("field_limit")
        tag, offset = _read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0 or number > (1 << 29) - 1:
            _fail("invalid_field_number")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, offset = _read_varint(data, offset)
            else:
                length = 8 if wire == 1 else 4
            if length > len(data) - offset:
                _fail("truncated_field")
            value = data[offset : offset + length]
            offset += length
        else:
            # Groups are not used in the pinned schema. Fail closed.
            _fail("unsupported_wire_type")
        yield number, wire, value


def _expect_wire(actual: int, expected: int):
    if actual != expected:
        _fail("wrong_wire_type")


def _once(seen: set, field: int):
    if field in seen:
        # protobuf permits last-one-wins/merge; a restricted probe instead
        # rejects ambiguous duplicate singular fields, including crypto flags.
        _fail("duplicate_singular_field")
    seen.add(field)


def _text(value: memoryview) -> str:
    if len(value) > MAX_TEXT_BYTES:
        _fail("text_limit")
    try:
        return value.tobytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise StickerWireError("invalid_utf8") from None


def _signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def safe_envelope_summary(packet: bytes) -> dict:
    """Inspect envelope and first-level payload framing; never decode text.

    Returns field numbers, wire types, value lengths, and ONLY raw varint values
    of field 2 (payloadType), 3 (cmd), and 4 (status). Lists preserve duplicates;
    empty lists mean absent, with no invented/default value. In particular,
    sid/cid, unknown varints, payload bytes and wrapper contents are not exposed.
    ``payload_shapes`` contains only number/wire/length for field 5 contents;
    it never returns a payload value, including field 2 which could mean either
    LarkError.code OR PullStickersResponse.updateTime. ACK (cmd 1) is not enough
    evidence to identify a payload as LarkError.
    This diagnostic does not validate a response as successful.
    """
    if not isinstance(packet, bytes):
        _fail("invalid_packet_type")
    summary = {
        "packet_bytes": len(packet),
        "fields": [],
        "payload_shapes": [],
        "payloadType": [],
        "cmd": [],
        "status": [],
        "parse_error": None,
    }
    if len(packet) > MAX_PACKET_BYTES:
        summary["parse_error"] = "packet_size_limit"
        return summary
    data = memoryview(packet)
    offset = 0
    names = {2: "payloadType", 3: "cmd", 4: "status"}
    try:
        while offset < len(data):
            if len(summary["fields"]) >= 128:
                _fail("envelope_diagnostic_field_limit")
            tag, offset = _read_varint(data, offset)
            number, wire = tag >> 3, tag & 7
            if number == 0 or number > (1 << 29) - 1:
                _fail("invalid_field_number")
            if wire == 0:
                start = offset
                value, offset = _read_varint(data, offset)
                length = offset - start
                if number in names:
                    summary[names[number]].append(value)
            elif wire in (1, 2, 5):
                if wire == 2:
                    length, offset = _read_varint(data, offset)
                else:
                    length = 8 if wire == 1 else 4
                if length > len(data) - offset:
                    _fail("truncated_field")
                if number == 5 and wire == 2:
                    summary["payload_shapes"].append(
                        _safe_payload_shape(data[offset : offset + length])
                    )
                # Other byte fields (including sid/cid) are never sliced.
                offset += length
            else:
                _fail("unsupported_wire_type")
            summary["fields"].append({"number": number, "wire": wire, "length": length})
    except StickerWireError as exc:
        summary["parse_error"] = exc.code
    return summary


def _safe_payload_shape(data: memoryview) -> dict:
    """No interpretation, strings, nested traversal, or numeric values."""
    result = {"fields": [], "parse_error": None}
    offset = 0
    try:
        while offset < len(data):
            if len(result["fields"]) >= 128:
                _fail("payload_diagnostic_field_limit")
            tag, offset = _read_varint(data, offset)
            number, wire = tag >> 3, tag & 7
            if number == 0 or number > (1 << 29) - 1:
                _fail("invalid_field_number")
            if wire == 0:
                start = offset
                _, offset = _read_varint(data, offset)
                length = offset - start
            elif wire in (1, 2, 5):
                if wire == 2:
                    length, offset = _read_varint(data, offset)
                else:
                    length = 8 if wire == 1 else 4
                if length > len(data) - offset:
                    _fail("truncated_field")
                offset += length
            else:
                _fail("unsupported_wire_type")
            result["fields"].append({"number": number, "wire": wire, "length": length})
    except StickerWireError as exc:
        result["parse_error"] = exc.code
    return result


def _decode_image(data: memoryview) -> dict:
    if len(data) > MAX_IMAGE_BYTES:
        _fail("image_limit")
    result = {"encrypted": False, "type": "NORMAL"}
    seen = set()
    text_fields = {1: "key", 11: "fsUnit"}
    numeric_fields = {3: "width", 4: "height", 10: "size"}
    for field, wire, value in _fields(data):
        if field in text_fields:
            _once(seen, field)
            _expect_wire(wire, 2)
            result[text_fields[field]] = _text(value)
        elif field in numeric_fields:
            _once(seen, field)
            _expect_wire(wire, 0)
            result[numeric_fields[field]] = _signed(value, 32)
        elif field in (5, 6, 9):
            # secureKey/secureUrls/Crypto: inspect presence only, not contents.
            _expect_wire(wire, 2)
            result["encrypted"] = True
        elif field in (7, 8):
            _expect_wire(wire, 0)
            result["encrypted"] = True
        # urls, params, etag and unknown fields never enter output.
    if result["encrypted"]:
        result["type"] = "ENCRYPTED"
    return result


def _decode_image_set(data: memoryview) -> dict:
    result = {}
    seen = set()
    for field, wire, value in _fields(data):
        if field in VARIANTS:
            _once(seen, field)
            _expect_wire(wire, 2)
            result[VARIANTS[field]] = _decode_image(value)
        # Deprecated set-wide key/imageKey fields are not fallback originals.
    return result


def _decode_sticker(data: memoryview) -> dict:
    if len(data) > MAX_STICKER_BYTES:
        _fail("sticker_limit")
    result = {"stickerId": "", "position": 0, "image": {}, "encrypted": False}
    text_fields = {1: "key", 3: "fsUnit", 8: "stickerSetId", 9: "stickerId"}
    numeric_fields = {2: "position", 6: "mode"}
    seen = set()
    for field, wire, value in _fields(data):
        if field in text_fields:
            _once(seen, field)
            _expect_wire(wire, 2)
            result[text_fields[field]] = _text(value)
        elif field in numeric_fields:
            _once(seen, field)
            _expect_wire(wire, 0)
            result[numeric_fields[field]] = _signed(value, 32)
        elif field == 4:
            _once(seen, field)
            _expect_wire(wire, 2)
            result["image"] = _decode_image_set(value)
        # Summary, payment details and unknown fields are deliberately omitted.
    result["encrypted"] = any(image["encrypted"] for image in result["image"].values())
    return result


def decode_pull(packet: bytes) -> dict:
    """Decode the HTTP 200 response to this probe's fixed cmd 103 request.

    CALLER CONTRACT: use only the response body received for an HTTP 200 POST
    built by build_pull. This is not a generic ACK/unsolicited-packet decoder.
    Official HTTP transports select the response schema from the request, not
    the packet's cmd: ACK=1 and the echoed PULL_STICKERS=103 are accepted here.
    Both reviewed transports decode non-OK HTTP bodies directly as LarkError,
    outside the Packet envelope; the caller must not pass those bodies here.
    Evidence: public 1181.aa6c8a55.js module 91420; 93.76299292.js Vm interceptor;
    8.3c339834.js module 72583 defines ACK=1.

    improto.Packet.status defaults to 200. Any explicit status other than 200
    fails before its opaque payload is decoded. Empty payload is a valid empty
    protobuf response only when the payload field is actually present.
    Successful decoding is NOT evidence that the collection is complete.
    """
    if not isinstance(packet, bytes):
        _fail("invalid_packet_type")
    if not packet or len(packet) > MAX_PACKET_BYTES:
        _fail("packet_size_limit")
    command = None
    payload_type = 1
    status = 200
    payload = None
    seen = set()
    for field, wire, value in _fields(memoryview(packet)):
        if field in (2, 3, 4, 11):
            _once(seen, field)
            _expect_wire(wire, 0)
            if field == 2:
                payload_type = value
            elif field == 3:
                command = _signed(value, 32)
            elif field == 4:
                status = value
            elif value not in (0, COMMAND):
                _fail("unexpected_command_override")
        elif field == 5:
            _once(seen, field)
            _expect_wire(wire, 2)
            payload = value
        elif field in (7, 8, 9):
            # No piped/versioned multi-payload response is expected here.
            _fail("unsupported_packet_wrapper")
        # sid/cid/cursor/retry fields and unknown fields never enter output.
    if status != 200:
        _fail("server_status_error")
    if command not in (1, COMMAND):
        _fail("unexpected_command")
    if payload_type != 1:
        _fail("unexpected_payload_type")
    if payload is None:
        _fail("missing_payload")

    stickers = []
    update_time = None
    seen = set()
    for field, wire, value in _fields(payload):
        if field == 1:
            _expect_wire(wire, 2)
            if len(stickers) >= MAX_COUNT:
                _fail("sticker_count_limit")
            stickers.append(_decode_sticker(value))
        elif field == 2:
            _once(seen, field)
            _expect_wire(wire, 0)
            update_time = _signed(value, 64)
    ids = [item["stickerId"] for item in stickers if item["stickerId"]]
    variants = Counter(name for item in stickers for name in item["image"])
    return {
        "response_command": command,
        "envelope_status": status,
        "stickers": stickers,
        "count": len(stickers),
        "unique_sticker_ids": len(set(ids)),
        "missing_sticker_ids": len(stickers) - len(ids),
        "duplicate_sticker_ids": len(ids) - len(set(ids)),
        "origin_missing": sum(
            not item["image"].get("origin", {}).get("key") for item in stickers
        ),
        "encrypted_count": sum(item["encrypted"] for item in stickers),
        "variant_counts": dict(sorted(variants.items())),
        "updateTime": update_time,
        "at_client_limit": len(stickers) >= MAX_COUNT,
        "completeness": "unverified",
    }
