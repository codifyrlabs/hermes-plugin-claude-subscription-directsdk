import pytest

from sidecar.password import MIN_PASSWORD_LENGTH, hash_password, main, verify_password

PW = "correct horse battery staple"


def test_round_trip_and_format():
    h = hash_password(PW)
    assert h.startswith("scrypt:16384:8:1:") and len(h.split(":")) == 6
    assert "$" not in h and "=" not in h
    assert verify_password(PW, h)
    assert not verify_password(PW + "x", h)


def test_salt_makes_hashes_differ():
    assert hash_password(PW) != hash_password(PW)


def test_short_password_rejected():
    with pytest.raises(ValueError):
        hash_password("x" * (MIN_PASSWORD_LENGTH - 1))


@pytest.mark.parametrize("encoded", [
    "", "scrypt", "scrypt:16384:8:1:abc", "bcrypt:16384:8:1:AAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAA",
    "scrypt:x:8:1:AAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAA", "scrypt:16384:8:1:!!!!:AAAAAAAAAAAAAAAAAAAAAAA",
    "scrypt:16384:8:1:AAAAAAAAAAA:AAAA", "scrypt:1048576:8:1:AAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAA",
    "scrypt:1000:8:1:AAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAA",
])
def test_malformed_hashes_never_verify(encoded):
    assert verify_password(PW, encoded) is False


def test_cli_prints_hash_only(capsys):
    answers = iter([PW, PW])
    assert main(read=lambda prompt: next(answers)) == 0
    out, err = capsys.readouterr()
    assert verify_password(PW, out.strip())
    assert PW not in out and PW not in err


def test_cli_rejects_mismatch_and_short(capsys):
    answers = iter([PW, PW + "x"])
    assert main(read=lambda prompt: next(answers)) == 1
    answers = iter(["short", "short"])
    assert main(read=lambda prompt: next(answers)) == 1
    assert capsys.readouterr().out == ""
