"""Offline, fail-closed origin URL resolver for the scoped Feishu probe.

No authentication, network, storage, or cryptography is performed here.  Inputs
must come from the root probe's approved settings request and sanitized sticker
decoder.  Returned URLs still require the root HTTP guard's approval.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

# This is intentionally narrow. Add a domain only after source/config evidence
# and an explicit update to the root network guard; settings alone do not grant
# permission to contact an arbitrary host.
ALLOWED_CDN_SUFFIXES = ("feishucdn.com",)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_RESOURCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,1023}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PLACEHOLDER = re.compile(r"{{([A-Za-z_][A-Za-z0-9_]*)}}")
_FALLBACK_DISALLOWED = ("avatar", "bundle", "file", "image", "sticker")
_SENSITIVE_FIELDS = (
    "secureKey",
    "secure_key",
    "secureUrls",
    "secure_urls",
    "crypto",
    "secureWidth",
    "secure_width",
    "secureHeight",
    "secure_height",
)
_MAX_UNITS = 512
_MAX_HOSTS = 16
_MAX_TEMPLATE = 8192


class ResourceResolutionError(ValueError):
    """Exception containing a fixed reason code and no input values."""

    def __init__(self, code: str, *, blocked: bool = False):
        self.code = code
        self.blocked = blocked
        super().__init__(code)

    def as_status(self) -> dict:
        return {"status": "blocked" if self.blocked else "missing", "reason": self.code}


class ResourceBlockedError(ResourceResolutionError):
    def __init__(self, code: str):
        super().__init__(code, blocked=True)


def _mapping(value, code: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ResourceResolutionError(code)
    return value


def _identifier(value, code: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ResourceBlockedError(code)
    return value


def _path_template(value) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_TEMPLATE:
        raise ResourceBlockedError("invalid_path_template")
    if not value.startswith("/") or value.startswith("//"):
        raise ResourceBlockedError("invalid_path_template")
    if (
        any(ord(c) < 33 or ord(c) == 127 for c in value)
        or "\\" in value
        or "#" in value
    ):
        raise ResourceBlockedError("invalid_path_template")
    remaining = _PLACEHOLDER.sub("", value)
    if "{" in remaining or "}" in remaining or "{{key}}" not in value:
        raise ResourceBlockedError("invalid_path_template")
    return value


def _host_shape(value) -> str:
    # Structural validation during extraction. Domain approval happens only for
    # the selected fsUnit, so an unrelated unapproved unit does not prevent the
    # caller from inspecting the filtered settings shape.
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ResourceBlockedError("invalid_cdn_host")
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise ResourceBlockedError("invalid_cdn_host")
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError:
        raise ResourceBlockedError("invalid_cdn_host") from None
    if (
        p.scheme != "https"
        or not p.hostname
        or p.username is not None
        or p.password is not None
        or port is not None
        or p.path not in ("", "/")
        or p.query
        or p.fragment
    ):
        raise ResourceBlockedError("invalid_cdn_host")
    host = p.hostname
    if (
        not host.isascii()
        or host.endswith(".")
        or not all(_HOST_LABEL.fullmatch(x) for x in host.split("."))
    ):
        raise ResourceBlockedError("invalid_cdn_host")
    # Lowercase canonical origin, with no trailing slash for path concatenation.
    return "https://" + host


def _approved_host(value) -> str:
    normalized = _host_shape(value)
    host = urlsplit(normalized).hostname
    if not any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_CDN_SUFFIXES
    ):
        raise ResourceBlockedError("cdn_domain_requires_review")
    return normalized


def extract_resource_config(settings_json) -> dict:
    """Select only the known resource settings from a complete /settings/v3.

    Accepted wire envelope: {"meta":{"complete":true}, "data":{
      "resource_url_tpl":{"fs_unit_tpl":{unit:{"hosts":[],
                          "path_tpl":"...", "param":{}}}},
      "fallback_fs_unit":{"cdn":"...", "direct":"..."}}}.

    Unknown settings and metadata are neither copied nor logged.  This method
    does not accept cache wrappers or recursively search arbitrary JSON.
    """
    top = _mapping(settings_json, "invalid_settings_envelope")
    meta = _mapping(top.get("meta"), "missing_settings_meta")
    if meta.get("complete") is not True:
        raise ResourceResolutionError("settings_not_complete")
    data = _mapping(top.get("data"), "missing_settings_data")
    resource = _mapping(data.get("resource_url_tpl"), "missing_resource_url_tpl")
    units = _mapping(resource.get("fs_unit_tpl"), "missing_fs_unit_tpl")
    if not units or len(units) > _MAX_UNITS:
        raise ResourceResolutionError("invalid_fs_unit_count")
    clean_units = {}
    for unit, raw in units.items():
        unit = _identifier(unit, "invalid_fs_unit")
        entry = _mapping(raw, "invalid_fs_unit_entry")
        hosts = entry.get("hosts")
        if not isinstance(hosts, list) or not hosts or len(hosts) > _MAX_HOSTS:
            raise ResourceResolutionError("invalid_cdn_host_count")
        params = entry.get("param", {})
        if params is None:
            params = {}
        params = _mapping(params, "invalid_template_params")
        clean_params = {}
        for parameter, options in params.items():
            parameter = _identifier(parameter, "invalid_template_param_name")
            options = _mapping(options, "invalid_template_param_options")
            clean_options = {}
            for option, value in options.items():
                if not isinstance(option, str) or not isinstance(
                    value, (str, int, float, bool)
                ):
                    raise ResourceResolutionError("invalid_template_param_value")
                # These are static transformations, not selected by origin
                # resolution. Keep their shape without arbitrary nested data.
                if len(option) > 128 or (
                    isinstance(value, str) and len(value) > _MAX_TEMPLATE
                ):
                    raise ResourceResolutionError("invalid_template_param_value")
                clean_options[option] = value
            clean_params[parameter] = clean_options
        clean_units[unit] = {
            "hosts": [_host_shape(host) for host in hosts],
            "path_tpl": _path_template(entry.get("path_tpl")),
            "param": clean_params,
        }
    result = {"resource_url_tpl": {"fs_unit_tpl": clean_units}}
    fallback = data.get("fallback_fs_unit")
    if fallback is not None:
        fallback = _mapping(fallback, "invalid_fallback_fs_unit")
        # Source requires both values. Do not synthesize a user-unit fallback.
        result["fallback_fs_unit"] = {
            "cdn": _identifier(fallback.get("cdn"), "invalid_fallback_cdn"),
            "direct": _identifier(fallback.get("direct"), "invalid_fallback_direct"),
        }
    return result


def describe_resource_config(config: dict) -> dict:
    """Return field structure/counts, without resource keys or config values."""
    config = _mapping(config, "invalid_resource_config")
    resource = _mapping(config.get("resource_url_tpl"), "missing_resource_url_tpl")
    units = _mapping(resource.get("fs_unit_tpl"), "missing_fs_unit_tpl")
    return {
        "wire_envelope": "meta.complete=true; data.<setting>",
        "selected_setting_names": [
            name for name in ("resource_url_tpl", "fallback_fs_unit") if name in config
        ],
        "resource_url_tpl": {
            "fs_unit_tpl": {
                "type": "object indexed by fsUnit",
                "unit_count": len(units),
                "entry_fields": {
                    "hosts": "list[str]",
                    "path_tpl": "str",
                    "param": "dict[str,dict]",
                },
                "host_count": sum(len(entry["hosts"]) for entry in units.values()),
            }
        },
        "fallback_fs_unit": {
            "present": "fallback_fs_unit" in config,
            "fields": ["cdn", "direct"],
        },
    }


def _block_protected(*objects: Mapping) -> None:
    for obj in objects:
        # Inspect field presence/flags only. Never access or copy protection
        # material. The scoped decoder normally removes these raw fields.
        if any(name in obj for name in _SENSITIVE_FIELDS):
            raise ResourceBlockedError("protected_resource")
        if obj.get("encrypted") not in (None, False):
            raise ResourceBlockedError("protected_resource")
        if obj.get("type") in (2, "2", "ENCRYPTED", "encrypted"):
            raise ResourceBlockedError("protected_resource")


def resolve_origin(sticker_dict: dict, config: dict) -> list[str]:
    """Construct only unprotected origin URLs; never choose a thumbnail.

    Input is the scoped gateway decoder's camelCase shape. A missing origin
    fsUnit is reported, even when sticker.fsUnit exists: this version does not
    assume those scopes are interchangeable. Protected inputs raise a fixed
    ResourceBlockedError; its as_status() emits only a blocked flag/reason.
    """
    sticker = _mapping(sticker_dict, "invalid_sticker")
    _block_protected(sticker)
    images = _mapping(sticker.get("image"), "missing_image")
    _block_protected(images)
    origin = _mapping(images.get("origin"), "missing_origin")
    _block_protected(origin)
    key = origin.get("key")
    if not key:
        raise ResourceResolutionError("missing_origin_key")
    if (
        not isinstance(key, str)
        or not _RESOURCE_KEY.fullmatch(key)
        or key in (".", "..")
    ):
        raise ResourceBlockedError("invalid_origin_key")
    fs_unit = origin.get("fsUnit")
    if not fs_unit:
        raise ResourceResolutionError("missing_origin_fs_unit")
    fs_unit = _identifier(fs_unit, "invalid_origin_fs_unit")
    config = _mapping(config, "invalid_resource_config")
    resource = _mapping(config.get("resource_url_tpl"), "missing_resource_url_tpl")
    units = _mapping(resource.get("fs_unit_tpl"), "missing_fs_unit_tpl")
    entry = units.get(fs_unit)
    if entry is None:
        if any(part in fs_unit for part in _FALLBACK_DISALLOWED):
            raise ResourceResolutionError("fs_unit_fallback_not_permitted")
        fallback = _mapping(config.get("fallback_fs_unit"), "missing_fallback_fs_unit")
        if not fallback.get("cdn") or not fallback.get("direct"):
            raise ResourceResolutionError("incomplete_fallback_fs_unit")
        fallback_unit = fallback["cdn"] if "cdn" in fs_unit else fallback["direct"]
        fallback_unit = _identifier(fallback_unit, "invalid_fallback_fs_unit")
        entry = units.get(fallback_unit)
        if entry is None:
            raise ResourceResolutionError("fallback_fs_unit_not_configured")
    entry = _mapping(entry, "invalid_fs_unit_entry")
    path = _path_template(entry.get("path_tpl"))
    hosts = entry.get("hosts")
    if not isinstance(hosts, list) or not hosts or len(hosts) > _MAX_HOSTS:
        raise ResourceResolutionError("invalid_cdn_host_count")
    # Web sticker code invokes cdn(key, fsUnit, {}), so no image-size/format
    # option is selected. Unknown placeholders are replaced with empty text.
    path = _PLACEHOLDER.sub(lambda match: key if match.group(1) == "key" else "", path)
    result = []
    for raw_host in hosts:
        host = _approved_host(raw_host)
        url = host + path
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != urlsplit(host).netloc
            or parsed.fragment
        ):
            raise ResourceBlockedError("invalid_resolved_url")
        if url not in result:
            result.append(url)
    return result
