"""Offline tests using generated solid-color images, never user media.

Small base64 GIF/WebP/JPEG fixtures were generated once with bundled Pillow.
Pillow is not imported or required by the module or these tests.
"""

import base64
import hashlib
import struct
import unittest
import zlib

from feishu_adapter._media import inspect_media

GIF = "R0lGODdhAgADAIEAAAcTFwAAAAAAAAAAACwAAAAAAgADAAAIBgABCBwYEAA7"
WEBP = "UklGRiwAAABXRUJQVlA4ICAAAABwAQCdASoCAAMAAUAmJZQCdAGIQAD+/GmpI4wmkjsAAA=="
ANIM_WEBP = "UklGRoQAAABXRUJQVlA4WAoAAAACAAAAAQAAAgAAQU5JTQYAAAAAAAAAAABBTk1GKAAAAAAAAAAAAAEAAAIAADIAAAJWUDhMDwAAAC8BgAAABxD9j/4HIqL/AQBBTk1GKAAAAAAAAAAAAAEAAAIAADIAAABWUDhMDwAAAC8BgAAABxDR//4HIqL/AQA="
JPEG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAADAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDw+iiitDI//9k="


def chunk(kind, body):
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def png(animated=False):
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 3, 8, 6, 0, 0, 0))
    compressed = zlib.compress((b"\0" + b"\xff\0\0\xff" * 2) * 3)
    controls = b""
    tail = b""
    if animated:
        controls = chunk(b"acTL", struct.pack(">II", 2, 0))
        controls += chunk(
            b"fcTL", struct.pack(">IIIIIHHBB", 0, 2, 3, 0, 0, 1, 10, 0, 0)
        )
        tail = chunk(b"fcTL", struct.pack(">IIIIIHHBB", 1, 2, 3, 0, 0, 1, 10, 0, 0))
        tail += chunk(b"fdAT", struct.pack(">I", 2) + compressed)
    return (
        b"\x89PNG\r\n\x1a\n"
        + header
        + controls
        + chunk(b"IDAT", compressed)
        + tail
        + chunk(b"IEND", b"")
    )


class MediaInspectTests(unittest.TestCase):
    def test_generated_still_images(self):
        fixtures = [
            (png(), "image/png"),
            (base64.b64decode(GIF), "image/gif"),
            (base64.b64decode(WEBP), "image/webp"),
            (base64.b64decode(JPEG), "image/jpeg"),
        ]
        for data, mime in fixtures:
            with self.subTest(mime=mime):
                info = inspect_media(data)
                self.assertTrue(info["complete"])
                self.assertEqual((info["width"], info["height"]), (2, 3))
                self.assertEqual(info["mime"], mime)
                self.assertEqual(info["frame_count"], 1)
                self.assertFalse(info["animated"])
                self.assertEqual(info["sha256"], hashlib.sha256(data).hexdigest())

    def test_generated_animation(self):
        gif = base64.b64decode(GIF)
        # Repeating the synthetic image descriptor/data before the trailer
        # creates a second GIF frame; the global color table is shared.
        second_image = gif[gif.index(b"\x2c") : -1]
        animated_gif = gif[:-1] + second_image + b"\x3b"
        for data in [png(True), base64.b64decode(ANIM_WEBP), animated_gif]:
            info = inspect_media(data)
            self.assertTrue(info["complete"])
            self.assertTrue(info["animated"])
            self.assertEqual(info["frame_count"], 2)

    def test_truncation_of_every_container(self):
        fixtures = [
            png(),
            png(True),
            *[base64.b64decode(x) for x in (GIF, WEBP, ANIM_WEBP, JPEG)],
        ]
        for data in fixtures:
            for cut in [1, 3, 10, len(data) // 2]:
                with self.subTest(length=len(data), cut=cut):
                    self.assertFalse(inspect_media(data[:-cut])["complete"])

    def test_forged_suffix_signature_and_crc(self):
        self.assertFalse(inspect_media(b"<html>error.png</html>")["complete"])
        self.assertEqual(
            inspect_media(b"<html>error.gif</html>")["mime"], "application/octet-stream"
        )
        self.assertFalse(inspect_media(b"\xff\xd8\xff\xd9")["complete"])
        corrupt = bytearray(png())
        corrupt[20] ^= 1
        self.assertFalse(inspect_media(bytes(corrupt))["complete"])
        # Filename-like appended bytes are not accepted as part of an image.
        self.assertFalse(inspect_media(png() + b"fake.gif")["complete"])

    def test_bad_declared_webp_size_and_png_frame_count(self):
        webp = bytearray(base64.b64decode(WEBP))
        webp[4:8] = struct.pack("<I", len(webp) + 200)
        self.assertFalse(inspect_media(bytes(webp))["complete"])
        data = png(True)
        good = chunk(b"acTL", struct.pack(">II", 2, 0))
        bad = chunk(b"acTL", struct.pack(">II", 3, 0))
        self.assertFalse(inspect_media(data.replace(good, bad))["complete"])


if __name__ == "__main__":
    unittest.main()
