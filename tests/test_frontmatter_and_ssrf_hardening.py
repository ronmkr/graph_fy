"""Regression tests for SSRF and YAML frontmatter hardening (Issue #78)."""
from __future__ import annotations

import ipaddress
import pytest

from graph_fy.export import _obsidian_tag
from graph_fy.google_workspace import _safe_yaml_str
from graph_fy.security import _ip_is_blocked, validate_url


def test_ip_is_blocked_rejects_multicast_and_unspecified():
    """#4708: a host resolving to a multicast or unspecified address must be
    blocked by the SSRF guard; ordinary public IPs still pass."""
    for bad in ("224.0.0.1", "239.1.1.1", "ff02::1", "0.0.0.0", "::"):
        assert _ip_is_blocked(ipaddress.ip_address(bad)), f"{bad} should be blocked"
    for good in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
        assert not _ip_is_blocked(ipaddress.ip_address(good)), f"{good} should pass"


def test_obsidian_tag_strips_yaml_breaking_chars():
    """#4709: a tag value carrying a newline cannot break out of the frontmatter
    `tags:` list into a column-0 sibling key."""
    tag = _obsidian_tag("document\ninjected_key: pwned")
    assert "\n" not in tag and ":" not in tag
    assert tag == "documentinjected_key_pwned"


def test_safe_yaml_str_escapes_line_breaks_and_controls():
    """#4710: a hostile shortcut file id/url carrying raw line breaks (including
    U+2028 and U+0085) or a tab is escaped so it cannot inject sibling keys."""
    out = _safe_yaml_str("abc\u2028injected: pwned\u0085evil: x\ttabbed\nnl\rcr")
    for raw in ("\u2028", "\u2029", "\u0085", "\t", "\n", "\r"):
        assert raw not in out, f"{raw!r} must be escaped, not left raw"
    assert "injected" in out


def test_validate_url_rejects_hostless_and_credentials_and_bad_ports():
    with pytest.raises(ValueError, match="without a hostname"):
        validate_url("http:///path")

    with pytest.raises(ValueError, match="embedded credentials"):
        validate_url("http://user:pass@example.com/api")

    with pytest.raises(ValueError, match="invalid port"):
        validate_url("http://example.com:notaport/api")

    with pytest.raises(ValueError, match="port 0"):
        validate_url("http://example.com:0/api")

    # Valid URLs pass through
    assert validate_url("https://example.com/api") == "https://example.com/api"
    assert validate_url("http://example.com:8080/api") == "http://example.com:8080/api"
