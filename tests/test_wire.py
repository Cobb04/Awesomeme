"""Synthetic offline tests. No user data, login, file fixtures or network."""

import json
import unittest

from feishu_adapter._wire import (
    StickerWireError,
    build_pull,
    decode_pull,
    safe_envelope_summary,
)


def uv(value):
    result = []
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + [value])


def number(field, value):
    return uv(field * 8) + uv(value)


def blob(field, value):
    if isinstance(value, str):
        value = value.encode()
    return uv(field * 8 + 2) + uv(len(value)) + value


def envelope(payload, status=None, command=103, payload_type=1):
    result = number(2, payload_type) + number(3, command)
    if status is not None:
        result += number(4, status)
    return result + blob(5, payload)


class StickerWireTests(unittest.TestCase):
    def test_safe_envelope_summary_never_decodes_sensitive_fields(self):
        secret = "SYNTHETIC_SID_CID_PAYLOAD_SECRET"
        data = blob(1, secret) + number(2, 1) + number(3, 103) + number(4, 200)
        data += (
            blob(5, secret) + blob(6, secret) + blob(8, secret) + number(12, 123456789)
        )
        result = safe_envelope_summary(data)
        self.assertEqual(result["cmd"], [103])
        self.assertEqual(result["status"], [200])
        self.assertIsNone(result["parse_error"])
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn("123456789", json.dumps(result))
        self.assertEqual(
            result["fields"][0], {"number": 1, "wire": 2, "length": len(secret)}
        )

    def test_safe_summary_preserves_absence_duplicates_and_errors(self):
        result = safe_envelope_summary(number(3, 103) + number(3, 0) + blob(5, b""))
        self.assertEqual(result["cmd"], [103, 0])
        self.assertEqual(result["status"], [])
        self.assertEqual(
            safe_envelope_summary(b"\x2a\x05\x00")["parse_error"], "truncated_field"
        )
        self.assertEqual(
            safe_envelope_summary(number(30, 1) * 129)["parse_error"],
            "envelope_diagnostic_field_limit",
        )

    def test_payload_shape_does_not_guess_error_or_return_values(self):
        secret = "SYNTHETIC_ERROR_MESSAGE_SECRET"
        payload = blob(1, secret) + number(2, 123456789) + number(3, 2)
        result = safe_envelope_summary(envelope(payload, command=1))
        self.assertEqual(
            result["payload_shapes"],
            [
                {
                    "fields": [
                        {"number": 1, "wire": 2, "length": len(secret)},
                        {"number": 2, "wire": 0, "length": 4},
                        {"number": 3, "wire": 0, "length": 1},
                    ],
                    "parse_error": None,
                }
            ],
        )
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn("123456789", json.dumps(result))
        malformed = safe_envelope_summary(envelope(b"\x12\x05\x00", command=1))
        self.assertIsNone(malformed["parse_error"])
        self.assertEqual(
            malformed["payload_shapes"][0]["parse_error"], "truncated_field"
        )

    def test_http_ack_is_request_correlated_and_still_checked(self):
        # Artificial nanosecond-like int64; not derived from a user's packet.
        result = decode_pull(envelope(number(2, 1700000000000000000), command=1))
        self.assertEqual(result["response_command"], 1)
        self.assertEqual(result["envelope_status"], 200)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["updateTime"], 1700000000000000000)
        self.assertEqual(result["completeness"], "unverified")
        for data in [
            number(2, 1) + number(3, 1),
            envelope(b"", command=1, status=401),
            envelope(b"", command=1, payload_type=2),
            envelope(blob(2, b"wrong-wire"), command=1),
        ]:
            with self.assertRaises(StickerWireError):
                decode_pull(data)

    def test_exact_allowlisted_request(self):
        self.assertEqual(
            build_pull(1000, "synthetic-id"),
            bytes.fromhex("100118672a05080010e807320c73796e7468657469632d6964"),
        )
        for count in [0, 1001, -1, True, "100"]:
            with self.assertRaises(StickerWireError):
                build_pull(count, "x")
        for cid in ["", "new\nline", "中", "x" * 129]:
            with self.assertRaises(StickerWireError):
                build_pull(1, cid)

    def test_normal_metadata_counts_and_default_status(self):
        image = (
            blob(1, "ordinary-key")
            + number(3, 320)
            + number(4, 240)
            + blob(11, "unit-sticker")
        )
        sticker = blob(9, "synthetic-sticker") + number(2, 7) + blob(4, blob(2, image))
        decoded = decode_pull(envelope(blob(1, sticker) + number(2, 123)))
        self.assertEqual(decoded["count"], 1)
        self.assertEqual(decoded["updateTime"], 123)
        self.assertEqual(decoded["origin_missing"], 0)
        origin = decoded["stickers"][0]["image"]["origin"]
        self.assertEqual(origin["width"], 320)
        self.assertEqual(origin["key"], "ordinary-key")
        self.assertFalse(origin["encrypted"])
        self.assertEqual(decode_pull(envelope(b"", status=200))["count"], 0)

    def test_secret_fields_never_return_or_raise(self):
        secret = "SYNTHETIC_SECRET_MUST_NOT_APPEAR"
        image = (
            blob(1, "plain-key") + blob(5, secret) + blob(9, secret) + blob(6, secret)
        )
        image += blob(2, secret) + blob(12, secret) + blob(13, secret)
        sticker = (
            blob(9, "synthetic-sticker") + blob(4, blob(2, image)) + blob(5, secret)
        )
        decoded = decode_pull(envelope(blob(1, sticker)))
        rendered = json.dumps(decoded)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("secureKey", rendered)
        self.assertNotIn("crypto", rendered)
        self.assertEqual(decoded["encrypted_count"], 1)
        self.assertEqual(decoded["stickers"][0]["image"]["origin"]["type"], "ENCRYPTED")
        with self.assertRaises(StickerWireError) as ctx:
            decode_pull(envelope(secret.encode(), status=401))
        self.assertEqual(str(ctx.exception), "server_status_error")
        self.assertNotIn(secret, str(ctx.exception))

    def test_reject_wrong_packet(self):
        cases = [
            envelope(b"", command=104),
            envelope(b"", payload_type=2),
            envelope(b"", status=0),
            number(3, 103),
            envelope(b"") + number(3, 103),
            envelope(b"") + number(11, 104),
            envelope(b"") + blob(8, b""),
        ]
        for data in cases:
            with (
                self.subTest(data_length=len(data)),
                self.assertRaises(StickerWireError),
            ):
                decode_pull(data)

    def test_malformed_wire_and_utf8(self):
        cases = [
            b"\x00",
            b"\x80",
            b"\x08" + b"\xff" * 10,
            b"\x2a\x05\x00",
            b"\x0b",
            b"\x09\x00",
            envelope(blob(1, blob(9, b"\xff"))),
            envelope(blob(1, number(9, 10))),
        ]
        for data in cases:
            with (
                self.subTest(data_length=len(data)),
                self.assertRaises(StickerWireError),
            ):
                decode_pull(data)

    def test_count_boundary_and_missing_or_duplicate_ids(self):
        item = blob(1, blob(9, "same-synthetic-id"))
        result = decode_pull(envelope(item * 1000))
        self.assertTrue(result["at_client_limit"])
        self.assertEqual(result["completeness"], "unverified")
        self.assertEqual(result["duplicate_sticker_ids"], 999)
        with self.assertRaises(StickerWireError):
            decode_pull(envelope(item * 1001))
        result = decode_pull(envelope(blob(1, b"")))
        self.assertEqual(result["missing_sticker_ids"], 1)

    def test_unknown_fields_ignored_and_all_variants(self):
        image = blob(1, "key") + number(77, 9)
        image_set = b"".join(blob(field, image) for field in [2, 3, 5, 6, 7, 8, 9, 10])
        payload = blob(1, blob(4, image_set)) + blob(50, b"opaque")
        decoded = decode_pull(envelope(payload))
        self.assertEqual(len(decoded["variant_counts"]), 8)


if __name__ == "__main__":
    unittest.main()
