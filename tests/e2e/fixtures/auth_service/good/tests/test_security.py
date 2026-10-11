from app import security


def test_password_roundtrip_and_wrong_password():
    stored = security.hash_password("correct horse battery")
    assert security.verify_password("correct horse battery", stored)
    assert not security.verify_password("wrong password", stored)
    assert stored != security.hash_password("correct horse battery")  # salted


def test_malformed_hash_is_rejected_not_raised():
    assert not security.verify_password("x", "garbage")


def test_token_roundtrip_expiry_and_tamper():
    token = security.create_token("alice", "k", ttl=60, now=1000)
    assert security.decode_token(token, "k", now=1030) == "alice"
    assert security.decode_token(token, "k", now=1061) is None
    assert security.decode_token(token, "other-key", now=1030) is None
    head, sig = token.split(".")
    assert security.decode_token(head + "x." + sig, "k", now=1030) is None
    assert security.decode_token("not-a-token", "k") is None
