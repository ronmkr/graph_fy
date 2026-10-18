"""XML utility helpers for extractors."""
from __future__ import annotations

_PROJECT_XML_MAX_BYTES = 2 * 1024 * 1024


def _project_xml_is_safe(src: bytes) -> bool:
    """Reject XML that declares DTDs or entities.

    Stdlib xml.etree.ElementTree does not cap entity expansion, so a
    crafted project file could trigger a billion-laughs style DoS. External
    entity resolution is already disabled by pyexpat defaults, but rejecting
    <!DOCTYPE / <!ENTITY outright is defense in depth.

    Legitimate MSBuild and Lazarus package files never contain a DOCTYPE
    or ENTITY declaration, so this is a zero-false-positive screen.
    """
    lowered = src.lower()
    return b"<!doctype" not in lowered and b"<!entity" not in lowered
