"""Synthetic tests only. No network or user account data."""

import copy
import unittest

from feishu_adapter._resolver import (
    ResourceBlockedError,
    ResourceResolutionError,
    extract_resource_config,
    resolve_origin,
)


def wire_config():
    return {
        "meta": {"complete": True, "unrelated_meta": "must-not-copy"},
        "data": {
            "unrelated_setting": {"value": "must-not-copy"},
            "resource_url_tpl": {
                "fs_unit_tpl": {
                    "sample-cdn": {
                        "hosts": ["https://cdn.feishucdn.com"],
                        "path_tpl": "/static-resource/v1/{{key}}~?format={{format}}",
                        "param": {"format": {"small": "default-small"}},
                        "unrelated_entry_field": "must-not-copy",
                    },
                    "sample-direct": {
                        "hosts": ["https://direct.feishucdn.com"],
                        "path_tpl": "/resource/{{key}}",
                        "param": {},
                    },
                },
                "unrelated_resource_field": "must-not-copy",
            },
            "fallback_fs_unit": {"cdn": "sample-cdn", "direct": "sample-direct"},
        },
    }


def sticker():
    return {
        "stickerId": "synthetic-123",
        "fsUnit": "different-root-unit",
        "encrypted": False,
        "image": {
            "origin": {
                "key": "img_v3_synthetic_origin",
                "fsUnit": "sample-cdn",
                "type": "NORMAL",
                "encrypted": False,
            },
            "thumbnail": {"key": "img_v3_synthetic_thumb", "fsUnit": "sample-cdn"},
        },
        "smallUrl": "https://thumbnail.feishucdn.com/small.webp",
    }


class ResourceResolverTest(unittest.TestCase):
    def setUp(self):
        self.config = extract_resource_config(wire_config())

    def test_extract_keeps_only_resource_settings(self):
        self.assertEqual(set(self.config), {"resource_url_tpl", "fallback_fs_unit"})
        self.assertNotIn("must-not-copy", repr(self.config))
        self.assertEqual(set(self.config["resource_url_tpl"]), {"fs_unit_tpl"})

    def test_settings_must_be_complete(self):
        for value in (False, "true", 1, None):
            payload = wire_config()
            payload["meta"]["complete"] = value
            with self.assertRaisesRegex(
                ResourceResolutionError, "settings_not_complete"
            ):
                extract_resource_config(payload)

    def test_missing_config_has_explicit_reason(self):
        with self.assertRaisesRegex(
            ResourceResolutionError, "missing_resource_url_tpl"
        ):
            extract_resource_config({"meta": {"complete": True}, "data": {}})

    def test_origin_only_no_small_format_option(self):
        self.assertEqual(
            resolve_origin(sticker(), self.config),
            [
                "https://cdn.feishucdn.com/static-resource/v1/img_v3_synthetic_origin~?format="
            ],
        )

    def test_no_origin_does_not_use_thumbnail_or_smallurl(self):
        value = sticker()
        del value["image"]["origin"]
        with self.assertRaisesRegex(ResourceResolutionError, "missing_origin"):
            resolve_origin(value, self.config)

    def test_no_origin_unit_does_not_guess_top_level_unit(self):
        value = sticker()
        del value["image"]["origin"]["fsUnit"]
        with self.assertRaisesRegex(ResourceResolutionError, "missing_origin_fs_unit"):
            resolve_origin(value, self.config)

    def test_protected_flags_never_return_urls(self):
        cases = [
            ("origin", "crypto", {"sentinel": "private-test-only"}),
            ("origin", "secureKey", "private-test-only"),
            ("origin", "secure_urls", ["private-test-only"]),
            ("origin", "type", "ENCRYPTED"),
            ("origin", "type", 2),
            ("image", "crypto", {}),
            ("root", "encrypted", True),
        ]
        for scope, key, data in cases:
            with self.subTest(scope=scope, key=key):
                value = sticker()
                target = (
                    value
                    if scope == "root"
                    else (
                        value["image"] if scope == "image" else value["image"]["origin"]
                    )
                )
                target[key] = data
                with self.assertRaises(ResourceBlockedError) as caught:
                    resolve_origin(value, self.config)
                self.assertTrue(caught.exception.blocked)
                self.assertEqual(
                    caught.exception.as_status(),
                    {"status": "blocked", "reason": "protected_resource"},
                )
                self.assertNotIn("private-test-only", str(caught.exception))

    def test_cdn_fallback_uses_only_configured_fallback(self):
        value = sticker()
        value["image"]["origin"]["fsUnit"] = "old-region-cdn"
        self.assertEqual(
            resolve_origin(value, self.config)[0].split("/")[2], "cdn.feishucdn.com"
        )

    def test_direct_fallback_matches_source_selection(self):
        value = sticker()
        value["image"]["origin"]["fsUnit"] = "old-region-direct"
        self.assertEqual(
            resolve_origin(value, self.config),
            ["https://direct.feishucdn.com/resource/img_v3_synthetic_origin"],
        )

    def test_resource_typed_units_cannot_fallback(self):
        for unit in ("sticker-cdn", "image-x", "bundle-x", "avatar-x", "file-x"):
            value = sticker()
            value["image"]["origin"]["fsUnit"] = unit
            with self.assertRaisesRegex(
                ResourceResolutionError, "fs_unit_fallback_not_permitted"
            ):
                resolve_origin(value, self.config)

    def test_missing_fallback_does_not_derive_from_user(self):
        config = copy.deepcopy(self.config)
        del config["fallback_fs_unit"]
        value = sticker()
        value["image"]["origin"]["fsUnit"] = "old-cdn"
        with self.assertRaisesRegex(
            ResourceResolutionError, "missing_fallback_fs_unit"
        ):
            resolve_origin(value, config)

    def test_unapproved_domain_cannot_pass_suffix_trick(self):
        for host in (
            "https://evil.example",
            "https://feishucdn.com.evil.example",
            "https://notfeishucdn.com",
        ):
            config = copy.deepcopy(self.config)
            config["resource_url_tpl"]["fs_unit_tpl"]["sample-cdn"]["hosts"] = [host]
            with self.assertRaisesRegex(
                ResourceBlockedError, "cdn_domain_requires_review"
            ):
                resolve_origin(sticker(), config)

    def test_host_authority_restrictions(self):
        for host in (
            "http://cdn.feishucdn.com",
            "https://user@cdn.feishucdn.com",
            "https://cdn.feishucdn.com:443",
            "https://cdn.feishucdn.com/a",
            "https://cdn.feishucdn.com/?x=y",
            "https://127.0.0.1",
            "https://cdn.feishucdn.com.\n",
        ):
            config = copy.deepcopy(self.config)
            config["resource_url_tpl"]["fs_unit_tpl"]["sample-cdn"]["hosts"] = [host]
            with self.assertRaises(ResourceBlockedError):
                resolve_origin(sticker(), config)

    def test_key_cannot_modify_authority_query_or_path(self):
        for key in (
            "../x",
            "x?target=bad",
            "x#bad",
            "x/../../a",
            "x%2f..",
            "..",
            "x\\a",
            "x\na",
        ):
            value = sticker()
            value["image"]["origin"]["key"] = key
            with self.assertRaisesRegex(ResourceBlockedError, "invalid_origin_key"):
                resolve_origin(value, self.config)

    def test_malformed_templates_are_blocked(self):
        for path in (
            "//evil.example/{{key}}",
            "https://evil.example/{{key}}",
            "/x/{{key}}#fragment",
            "/x/{key}",
            "/x/{{nested.key}}",
            "/x/{{key}}\n",
        ):
            config = copy.deepcopy(self.config)
            config["resource_url_tpl"]["fs_unit_tpl"]["sample-cdn"]["path_tpl"] = path
            with self.assertRaises(ResourceBlockedError):
                resolve_origin(sticker(), config)

    def test_urls_deduplicated_in_original_host_order(self):
        config = copy.deepcopy(self.config)
        config["resource_url_tpl"]["fs_unit_tpl"]["sample-cdn"]["hosts"] = [
            "https://cdn.feishucdn.com",
            "https://cdn.feishucdn.com/",
            "https://cdn2.feishucdn.com",
        ]
        urls = resolve_origin(sticker(), config)
        self.assertEqual(len(urls), 2)
        self.assertIn("cdn2.feishucdn.com", urls[1])


if __name__ == "__main__":
    unittest.main()
