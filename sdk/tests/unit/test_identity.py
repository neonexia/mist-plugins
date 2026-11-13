"""Identity sources (docs/design/sdk.md section 3.1)."""
import pytest

from mist.errors import IdentityError
from mist.identity import Callback, PrincipalKey, Token, TokenFile, resolve

MIST_VAR = "MIST_IDENTITY_TOKEN_FILE"
AWS_VAR = "AWS_WEB_IDENTITY_TOKEN_FILE"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(MIST_VAR, raising=False)
    monkeypatch.delenv(AWS_VAR, raising=False)


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_no_identity_names_both_variables():
    with pytest.raises(IdentityError) as e:
        resolve(None)
    assert MIST_VAR in str(e.value)
    assert AWS_VAR in str(e.value)


def test_mist_variable_wins_over_the_aws_one(tmp_path, monkeypatch):
    monkeypatch.setenv(MIST_VAR, str(write(tmp_path / "mist", "mist-jwt")))
    monkeypatch.setenv(AWS_VAR, str(write(tmp_path / "aws", "aws-jwt")))
    source = resolve(None)
    assert isinstance(source, TokenFile)
    assert source.token() == "mist-jwt"


def test_aws_variable_is_used_when_the_mist_one_is_unset(tmp_path, monkeypatch):
    monkeypatch.setenv(AWS_VAR, str(write(tmp_path / "aws", "aws-jwt")))
    assert resolve(None).token() == "aws-jwt"


def test_explicit_identity_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(MIST_VAR, str(write(tmp_path / "mist", "mist-jwt")))
    explicit = Token("explicit-jwt")
    assert resolve(explicit) is explicit
    key = PrincipalKey("mk-1", "s3cret")
    assert resolve(key) is key
    assert (key.key_id, key.secret) == ("mk-1", "s3cret")


def test_token_file_is_read_again_on_every_call(tmp_path):
    path = write(tmp_path / "token", "first")
    source = TokenFile(path)
    assert source.token() == "first"
    write(path, "second")
    assert source.token() == "second"


def test_token_file_strips_a_trailing_newline(tmp_path):
    source = TokenFile(write(tmp_path / "token", "  eyJ.abc.def\n"))
    assert source.token() == "eyJ.abc.def"


def test_empty_or_missing_token_file_raises_identity_error_with_the_path(tmp_path):
    empty = write(tmp_path / "empty", " \n")
    with pytest.raises(IdentityError) as e:
        TokenFile(empty).token()
    assert str(empty) in str(e.value)
    missing = tmp_path / "missing"
    with pytest.raises(IdentityError) as e:
        TokenFile(missing).token()
    assert str(missing) in str(e.value)


def test_callback_is_called_each_time():
    tokens = iter(["one", "two"])
    source = Callback(lambda: next(tokens))
    assert source.token() == "one"
    assert source.token() == "two"


def test_secrets_stay_out_of_repr():
    assert "s3cret" not in repr(PrincipalKey("mk-1", "s3cret"))
    assert "eyJ.secret" not in repr(Token("eyJ.secret"))


def test_unknown_identity_is_refused():
    with pytest.raises(TypeError):
        resolve("eyJ.a-bare-string")
