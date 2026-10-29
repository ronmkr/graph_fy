"""Tests for centralized rationale redaction and extended secret patterns (#4647 / #67)."""
from pathlib import Path
import pytest

from graph_fy.security import redact_credentials, _redact_rationale_labels
from graph_fy.extract import extract


def test_redact_url_credentials():
    # Empty username
    assert redact_credentials("connect to https://:mypassword@example.com/api") == "connect to https://[redacted]@example.com/api"
    # Normal user:password
    assert redact_credentials("postgres://user:secret@localhost:5432/db") == "postgres://[redacted]@localhost:5432/db"
    # Password with slash and @
    assert redact_credentials("https://user:p/a@ss@example.com:8080/path") == "https://[redacted]@example.com:8080/path"
    # Clean URLs left untouched
    assert redact_credentials("https://example.com/docs") == "https://example.com/docs"
    assert redact_credentials("https://example.com:8080/docs") == "https://example.com:8080/docs"


def test_redact_pem_private_keys():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Yv...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert redact_credentials(pem) == "[redacted]"


def test_redact_tokens_and_jwt():
    # Token glued with underscore
    assert redact_credentials("token=prefix_ghp_123456789012345678901234567890123456_suffix") == "token=prefix_[redacted]_suffix"
    # Slack refresh token
    assert redact_credentials("refresh: xoxe-123456789-abcdef") == "refresh: [redacted]"
    # Short JWT
    assert redact_credentials("jwt: eyJhbGciOi.eyJzdWIiOi.abcdefghij") == "jwt: [redacted]"


def test_benign_strings_not_redacted():
    # Hashes
    sha = "hash: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"
    assert redact_credentials(sha) == sha
    # Prose with sk-
    prose = "use sk-learn or task-oriented pipeline with sk-1 control"
    assert redact_credentials(prose) == prose


def test_safe_extract_choke_point_redacts_python_rationale(tmp_path: Path):
    f = tmp_path / "sensitive.py"
    f.write_text(
        '"""Connect to https://admin:supersecret@cluster.internal/db using ghp_111122223333444455556666777788889999."""\n'
        'def connect():\n'
        '    pass\n'
    )
    result = extract([f], root=tmp_path)
    rationale_nodes = [n for n in result["nodes"] if n.get("file_type") == "rationale"]
    assert len(rationale_nodes) >= 1
    for r in rationale_nodes:
        assert "supersecret" not in r["label"]
        assert "111122223333" not in r["label"]
        assert "[redacted]" in r["label"]


def test_safe_extract_choke_point_redacts_commonlisp_rationale(tmp_path: Path):
    f = tmp_path / "sensitive.lisp"
    f.write_text(
        '(defun auth-service ()\n'
        '  "Authenticate via https://service:topsecret@auth.internal:8080/token"\n'
        '  t)\n'
    )
    result = extract([f], root=tmp_path)
    rationale_nodes = [n for n in result["nodes"] if n.get("file_type") == "rationale"]
    assert len(rationale_nodes) >= 1
    for r in rationale_nodes:
        assert "topsecret" not in r["label"]
        assert "[redacted]" in r["label"]
