"""Redaction of debug output.

The fake credentials below are assembled at runtime from obviously fake parts, so the
repository's secret scanner (gitleaks) never sees a literal that looks like a real key.
"""

from arl.redaction import redact, redact_text

FAKE = "x" * 6 + "0" * 6  # 12 characters, no real secret
FAKE_KEY = "sk-" + "ant-" + "fake" + FAKE
FAKE_DB_PASS = "fakepw" + FAKE


def test_known_secret_values_are_replaced(monkeypatch):
    monkeypatch.setenv("PLATFORM_OPS_TOKEN", FAKE)
    assert redact_text(f"value in log: {FAKE}") == "value in log: [REDACTED]"


def test_credential_patterns_and_emails_are_masked():
    text = (
        f"key {FAKE_KEY} and Bearer {FAKE} and "
        f"postgresql://arl_agent:{FAKE_DB_PASS}@localhost:5433/arl_ops and password={FAKE_DB_PASS} "
        "from dana.whitfield@corp.example"
    )
    out = redact_text(text, secrets=[])
    for leaked in (FAKE_KEY, FAKE, FAKE_DB_PASS, "dana.whitfield"):
        assert leaked not in out
    assert "postgresql://arl_agent:[REDACTED]@localhost" in out
    assert "[EMAIL]" in out


def test_nested_structures_are_redacted_and_non_strings_kept():
    data = {"output": {"rationale": f"password: {FAKE}", "confidence": 0.4}, "ids": ["a@b.co"]}
    assert redact(data, secrets=[]) == {
        "output": {"rationale": "password: [REDACTED]", "confidence": 0.4},
        "ids": ["[EMAIL]"],
    }
