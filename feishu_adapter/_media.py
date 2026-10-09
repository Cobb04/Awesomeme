"""Offline media container inspection using only the Python standard library.

``complete`` means structurally complete *container*: framing, lengths, terminal
markers, and PNG chunk CRCs passed. This does not decode every pixel, validate
all compressed bitstreams, or prove equality to a user's original upload.
No rendering, network, filesystem reads, or third-party imports occur here.
Recognizes PNG/APNG, GIF87a/89a, WebP and common JPEG SOF0/SOF1/SOF2 files by
magic bytes. Unsupported/malformed input returns complete=False, not success.
"""

import hashlib
import zlib

__all__ = ["inspect_media"]
MAX_BYTES = 64 * 1024 * 1024
MAX_FRAMES = 10000
MAX_CHUNKS = 100000


class _Invalid(Exception):
    pass


def _check(condition):
    if not condition:
        raise _Invalid()


class _Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.pos = 0

    @property
    def left(self):
        return len(self.data) - self.pos

    def take(self, count):
        _check(0 <= count <= self.left)
        result = self.data[self.pos : self.pos + count]
        self.pos += count
        return result

    def integer(self, count, endian="big"):
        return int.from_bytes(self.take(count), endian)


def _png(data, info, allow_trailing=False):
    reader = _Reader(data)
    _check(bytes(reader.take(8)) == b"\x89PNG\r\n\x1a\n")
    ihdr = idat = palette = closed_idat = False
    color = None
    default_bytes = 0
    animation_frames = None
    frame_count = 0
    sequence = 0
    frame_open = frame_data = False
    frame_uses_idat = False
    for _ in range(MAX_CHUNKS):
        length = reader.integer(4)
        kind = bytes(reader.take(4))
        _check(all(65 <= x <= 90 or 97 <= x <= 122 for x in kind))
        body = reader.take(length)
        crc = reader.integer(4)
        _check(zlib.crc32(body, zlib.crc32(kind)) & 0xFFFFFFFF == crc)
        _check(ihdr or kind == b"IHDR")
        if idat and kind != b"IDAT":
            closed_idat = True
        if kind == b"IHDR":
            _check(not ihdr and length == 13)
            width = int.from_bytes(body[:4], "big")
            height = int.from_bytes(body[4:8], "big")
            depth, color, compression, filtering, interlace = body[8:13]
            allowed = {
                0: (1, 2, 4, 8, 16),
                2: (8, 16),
                3: (1, 2, 4, 8),
                4: (8, 16),
                6: (8, 16),
            }
            _check(0 < width < 2**31 and 0 < height < 2**31)
            _check(color in allowed and depth in allowed[color])
            _check(compression == filtering == 0 and interlace in (0, 1))
            info.update(width=width, height=height, animated=False, frame_count=1)
            ihdr = True
        elif kind == b"PLTE":
            _check(not palette and not idat and color not in (0, 4))
            _check(0 < length <= 768 and length % 3 == 0)
            palette = True
        elif kind == b"acTL":
            _check(animation_frames is None and not idat and length == 8)
            animation_frames = int.from_bytes(body[:4], "big")
            _check(0 < animation_frames <= MAX_FRAMES)
            info.update(animated=animation_frames > 1, frame_count=animation_frames)
        elif kind == b"fcTL":
            _check(animation_frames is not None and length == 26)
            _check(not frame_open or frame_data)
            _check(int.from_bytes(body[:4], "big") == sequence)
            sequence += 1
            width, height, x, y = [
                int.from_bytes(body[start : start + 4], "big")
                for start in (4, 8, 12, 16)
            ]
            _check(width > 0 and height > 0)
            _check(x + width <= info["width"] and y + height <= info["height"])
            _check(body[24] <= 2 and body[25] <= 1)
            if not idat:
                _check(frame_count == 0 and x == y == 0)
                _check(width == info["width"] and height == info["height"])
            frame_count += 1
            _check(frame_count <= animation_frames)
            frame_open, frame_data, frame_uses_idat = True, False, not idat
        elif kind == b"IDAT":
            _check(not closed_idat and (color != 3 or palette))
            idat = True
            default_bytes += length
            if frame_open:
                _check(frame_uses_idat)
                frame_data = frame_data or length > 0
        elif kind == b"fdAT":
            _check(
                animation_frames is not None
                and frame_open
                and not frame_uses_idat
                and idat
            )
            _check(length > 4 and int.from_bytes(body[:4], "big") == sequence)
            sequence += 1
            frame_data = True
        elif kind == b"IEND":
            _check(length == 0 and idat and default_bytes > 0)
            _check(reader.left == 0 or (allow_trailing and reader.left <= 4096))
            if reader.left:
                info["trailing_bytes"] = reader.left
            if animation_frames is not None:
                _check(frame_count == animation_frames and frame_open and frame_data)
            return
        else:
            # Unknown ancillary chunks are allowed; unknown critical chunks are
            # not safely interpretable by this restricted inspector.
            _check(kind[0] & 32)
    raise _Invalid()


def _gif_subblocks(reader):
    total = 0
    for _ in range(MAX_CHUNKS):
        length = reader.integer(1)
        if not length:
            return total
        reader.take(length)
        total += length
    raise _Invalid()


def _gif(data, info, allow_trailing=False):
    reader = _Reader(data)
    _check(bytes(reader.take(6)) in (b"GIF87a", b"GIF89a"))
    width, height = reader.integer(2, "little"), reader.integer(2, "little")
    _check(width > 0 and height > 0)
    packed = reader.integer(1)
    reader.take(2)
    global_palette = bool(packed & 128)
    if global_palette:
        reader.take(3 * 2 ** ((packed & 7) + 1))
    frames = 0
    info.update(width=width, height=height, animated=False, frame_count=0)
    for _ in range(MAX_CHUNKS):
        marker = reader.integer(1)
        if marker == 0x3B:
            _check(
                frames > 0
                and (reader.left == 0 or (allow_trailing and reader.left <= 4096))
            )
            if reader.left:
                info["trailing_bytes"] = reader.left
            return
        if marker == 0x21:
            label = reader.integer(1)
            if label == 0xF9:
                _check(reader.integer(1) == 4)
                control = reader.take(4)
                _check(control[0] & 0xE0 == 0 and (control[0] >> 2) & 7 <= 3)
                _check(reader.integer(1) == 0)
            else:
                _gif_subblocks(reader)
        elif marker == 0x2C:
            x, y, fw, fh = [reader.integer(2, "little") for _ in range(4)]
            _check(fw > 0 and fh > 0 and x + fw <= width and y + fh <= height)
            flags = reader.integer(1)
            _check(flags & 0x18 == 0)
            local_palette = bool(flags & 128)
            _check(global_palette or local_palette)
            if local_palette:
                reader.take(3 * 2 ** ((flags & 7) + 1))
            _check(2 <= reader.integer(1) <= 8)
            _check(_gif_subblocks(reader) > 0)
            frames += 1
            _check(frames <= MAX_FRAMES)
            info.update(frame_count=frames, animated=frames > 1)
        else:
            raise _Invalid()
    raise _Invalid()


def _riff_chunks(data):
    reader = _Reader(data)
    for _ in range(MAX_CHUNKS):
        if reader.left == 0:
            return
        kind = bytes(reader.take(4))
        length = reader.integer(4, "little")
        body = reader.take(length)
        if length & 1:
            _check(reader.integer(1) == 0)
        yield kind, body
    raise _Invalid()


def _webp_dimensions(kind, body):
    if kind == b"VP8 ":
        _check(
            len(body) >= 10 and body[0] & 1 == 0 and bytes(body[3:6]) == b"\x9d\x01\x2a"
        )
        width = int.from_bytes(body[6:8], "little") & 0x3FFF
        height = int.from_bytes(body[8:10], "little") & 0x3FFF
        _check(width > 0 and height > 0)
        return width, height
    _check(kind == b"VP8L" and len(body) >= 5 and body[0] == 0x2F)
    bits = int.from_bytes(body[1:5], "little")
    _check(bits >> 29 == 0)
    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1


def _webp(data, info):
    reader = _Reader(data)
    _check(bytes(reader.take(4)) == b"RIFF")
    _check(reader.integer(4, "little") + 8 == len(data))
    _check(bytes(reader.take(4)) == b"WEBP")
    extended = animation_header = False
    animation_flag = False
    still_dimensions = None
    frames = 0
    for index, (kind, body) in enumerate(_riff_chunks(reader.take(reader.left))):
        if kind == b"VP8X":
            _check(index == 0 and len(body) == 10)
            _check(body[0] & 0xC1 == 0 and bytes(body[1:4]) == b"\x00\x00\x00")
            width = int.from_bytes(body[4:7], "little") + 1
            height = int.from_bytes(body[7:10], "little") + 1
            extended = True
            animation_flag = bool(body[0] & 2)
            info.update(
                width=width, height=height, animated=animation_flag, frame_count=0
            )
        elif kind == b"ANIM":
            _check(
                extended
                and animation_flag
                and not animation_header
                and frames == 0
                and len(body) == 6
            )
            animation_header = True
        elif kind == b"ANMF":
            _check(animation_header and len(body) >= 16)
            x = 2 * int.from_bytes(body[0:3], "little")
            y = 2 * int.from_bytes(body[3:6], "little")
            width = int.from_bytes(body[6:9], "little") + 1
            height = int.from_bytes(body[9:12], "little") + 1
            _check(body[15] & 0xFC == 0)
            _check(x + width <= info["width"] and y + height <= info["height"])
            dimensions = None
            alpha = False
            for subkind, subbody in _riff_chunks(body[16:]):
                if subkind == b"ALPH":
                    _check(not alpha and dimensions is None and len(subbody) > 0)
                    alpha = True
                else:
                    _check(subkind in (b"VP8 ", b"VP8L") and dimensions is None)
                    dimensions = _webp_dimensions(subkind, subbody)
            _check(dimensions == (width, height))
            frames += 1
            _check(frames <= MAX_FRAMES)
            info.update(frame_count=frames, animated=frames > 1)
        elif kind in (b"VP8 ", b"VP8L"):
            _check(not animation_flag and still_dimensions is None)
            still_dimensions = _webp_dimensions(kind, body)
        elif kind == b"ALPH":
            _check(
                extended
                and not animation_flag
                and still_dimensions is None
                and len(body) > 0
            )
        # Unknown RIFF chunks are allowed by the extensible WebP container.
    if animation_flag:
        _check(animation_header and frames > 0 and still_dimensions is None)
    else:
        _check(still_dimensions is not None)
        if extended:
            _check(still_dimensions == (info["width"], info["height"]))
        info.update(
            width=still_dimensions[0],
            height=still_dimensions[1],
            animated=False,
            frame_count=1,
        )


def _jpeg(data, info, allow_trailing=False):
    reader = _Reader(data)
    _check(bytes(reader.take(2)) == b"\xff\xd8")
    frame = False
    scans = 0
    quantization = huffman = False
    pending_marker = None
    components = set()
    for _ in range(MAX_CHUNKS):
        if pending_marker is None:
            _check(reader.integer(1) == 255)
            marker = reader.integer(1)
            while marker == 255:
                marker = reader.integer(1)
        else:
            marker, pending_marker = pending_marker, None
        if marker == 0xD9:
            _check(frame and scans > 0 and quantization and huffman)
            _check(reader.left == 0 or (allow_trailing and reader.left <= 4096))
            if reader.left:
                info["trailing_bytes"] = reader.left
            return
        _check(marker not in (0, 1, 0xD8) and not 0xD0 <= marker <= 0xD7)
        length = reader.integer(2)
        _check(length >= 2)
        body = reader.take(length - 2)
        if marker in (0xC0, 0xC1, 0xC2):
            _check(not frame and len(body) >= 6)
            precision = body[0]
            height, width = int.from_bytes(body[1:3], "big"), int.from_bytes(
                body[3:5], "big"
            )
            count = body[5]
            _check(
                precision in (8, 12) and width > 0 and height > 0 and 1 <= count <= 4
            )
            _check(len(body) == 6 + 3 * count)
            components = {body[6 + 3 * i] for i in range(count)}
            _check(len(components) == count)
            for i in range(count):
                sampling, table = body[7 + 3 * i], body[8 + 3 * i]
                _check(
                    1 <= sampling >> 4 <= 4 and 1 <= sampling & 15 <= 4 and table <= 3
                )
            frame = True
            info.update(width=width, height=height, animated=False, frame_count=1)
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            # Lossless/arithmetic/differential modes are outside this probe.
            raise _Invalid()
        elif marker == 0xDB:
            table_reader = _Reader(body)
            _check(table_reader.left > 0)
            while table_reader.left:
                spec = table_reader.integer(1)
                _check(spec >> 4 <= 1 and spec & 15 <= 3)
                table_reader.take(64 * (1 + (spec >> 4)))
            quantization = True
        elif marker == 0xC4:
            table_reader = _Reader(body)
            _check(table_reader.left > 0)
            while table_reader.left:
                spec = table_reader.integer(1)
                _check(spec >> 4 <= 1 and spec & 15 <= 3)
                count = sum(table_reader.take(16))
                _check(0 < count <= 256)
                table_reader.take(count)
            huffman = True
        elif marker == 0xDA:
            _check(frame and len(body) >= 4)
            count = body[0]
            _check(1 <= count <= len(components) and len(body) == 1 + 2 * count + 3)
            scan_components = {body[1 + 2 * i] for i in range(count)}
            _check(len(scan_components) == count and scan_components <= components)
            scans += 1
            entropy_bytes = 0
            while reader.left:
                byte = reader.integer(1)
                if byte != 255:
                    entropy_bytes += 1
                    continue
                next_byte = reader.integer(1)
                while next_byte == 255:
                    next_byte = reader.integer(1)
                if next_byte == 0:
                    entropy_bytes += 1
                elif 0xD0 <= next_byte <= 0xD7:
                    continue
                else:
                    pending_marker = next_byte
                    break
            _check(entropy_bytes > 0 and pending_marker is not None)
    raise _Invalid()


def inspect_media(
    data: bytes, *, allow_gif_trailing=False, allow_image_trailing=False
) -> dict:
    """Return magic-derived metadata; complete means container structure only.

    Unknown formats use application/octet-stream and null metadata. Recognized
    but truncated/unsupported files retain safely parsed metadata and report
    complete=False. No extension, remote Content-Type or filename is trusted.
    allow_gif_trailing accepts up to 4 KiB after a structurally verified GIF
    trailer and reports trailing_bytes; size and sha256 still cover ALL bytes.
    """
    if not isinstance(data, bytes):
        raise TypeError("media data must be bytes")
    info = {
        "mime": "application/octet-stream",
        "width": None,
        "height": None,
        "animated": None,
        "frame_count": None,
        "complete": False,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    parser = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        info["mime"], parser = "image/png", _png
    elif data.startswith((b"GIF87a", b"GIF89a")):
        info["mime"], parser = "image/gif", _gif
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        info["mime"], parser = "image/webp", _webp
    elif data.startswith(b"\xff\xd8"):
        info["mime"], parser = "image/jpeg", _jpeg
    if parser is not None and 0 < len(data) <= MAX_BYTES:
        try:
            if parser in (_png, _jpeg):
                parser(data, info, allow_trailing=allow_image_trailing)
            elif parser is _gif:
                parser(data, info, allow_trailing=allow_gif_trailing)
            else:
                parser(data, info)
            info["complete"] = True
        except _Invalid:
            pass
    return info
