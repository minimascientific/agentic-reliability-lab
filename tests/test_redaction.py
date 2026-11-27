from arl.redaction import redact, redact_text


def test_known_secret_values_are_replaced(monkeypatch):
    monkeypatch.setenv("PLATFORM_OPS_TOKEN", "abcd1234efgh5678")
    assert redact_text("token in log: abcd1234efgh5678") == "token in log: [REDACTED]"


def test_credential_patterns_and_emails_are_masked():
    text = (
        "key sk-ant-api03-AbCdEf123456 and Bearer abcdef0123456789 and "
        "postgresql://arl_agent:s3cretpass@localhost:5433/arl_ops and password=hunter22 "
        "from dana.whitfield@corp.example"
    )
    out = redact_text(text, secrets=[])
    for leaked in ("sk-ant-api03", "abcdef0123456789", "s3cretpass", "hunter22", "dana.whitfield"):
        assert leaked not in out
    assert "postgresql://arl_agent:[REDACTED]@localhost" in out
    assert "[EMAIL]" in out


def test_nested_structures_are_redacted_and_non_strings_kept():
    data = {"output": {"rationale": "password: x1y2z3w4", "confidence": 0.4}, "ids": ["a@b.co"]}
    assert redact(data, secrets=[]) == {
        "output": {"rationale": "password: [REDACTED]", "confidence": 0.4},
        "ids": ["[EMAIL]"],
    }
