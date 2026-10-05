from app.services.pii import PIILeakError, assert_deidentified, find_pii, redact_text


def test_redaction_removes_direct_identifiers() -> None:
    text = "张三，电话13800138000，病历号 JNU-000128，地址：广州市天河区某路1号"
    redacted = redact_text(text, ["张三", "JNU-000128"])
    assert "张三" not in redacted
    assert "13800138000" not in redacted
    assert "JNU-000128" not in redacted
    assert "广州市天河区" not in redacted
    assert find_pii(redacted, ["张三", "JNU-000128"]) == []


def test_deidentification_guard_fails_closed() -> None:
    try:
        assert_deidentified({"text": "联系电话 13800138000"})
    except PIILeakError as exc:
        assert "phone" in str(exc)
    else:
        raise AssertionError("PII guard should reject a phone number")


def test_structural_identifiers_do_not_false_trigger_phone_guard() -> None:
    # A UUID whose hex segment happens to contain an 11-digit decimal run must
    # not be mistaken for a phone number (intermittent false rejection).
    tricky_uuid = "12345678-1234-1234-1234-13427689123a"
    assert_deidentified({"evidence_id": tricky_uuid, "hash": "a" * 32})
    assert_deidentified({"nested": {"ids": [tricky_uuid, "b" * 40]}})


def test_phone_still_rejected_after_structural_scrub() -> None:
    try:
        assert_deidentified(
            {"note": "电话 13800138000", "id": "12345678-1234-1234-1234-13427689123a"}
        )
    except PIILeakError as exc:
        assert "phone" in str(exc)
    else:
        raise AssertionError("PII guard should still reject a real phone number")
