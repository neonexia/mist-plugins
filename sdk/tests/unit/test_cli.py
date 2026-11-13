"""The `mist-credentials` credential_process helper (docs/design/sdk.md section 3.4).

`main` runs in-process; STS is an httpx.MockTransport passed through `http_client`.
"""
import json
from datetime import datetime

import httpx
import pytest

from mist.cli import main
from mist.credentials import scope_policy

from test_credentials import FakeSts, error_xml

ENDPOINT = "https://mist.test:9443"
VARS = ("MIST_ENDPOINT", "MIST_PRINCIPAL_KEY_ID", "MIST_PRINCIPAL_KEY_SECRET",
        "MIST_IDENTITY_TOKEN_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "jwt"
    p.write_text("the-jwt-secret\n")
    return str(p)


def run(argv, sts):
    return main(argv, http_client=httpx.Client(transport=httpx.MockTransport(sts)))


def test_prints_credential_process_json(token_file, capsys):
    sts = FakeSts(900)
    assert run(["--endpoint", ENDPOINT, "--token-file", token_file], sts) == 0
    out = capsys.readouterr()
    doc = json.loads(out.out)
    assert set(doc) == {"Version", "AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"}
    assert doc["Version"] == 1
    assert doc["AccessKeyId"] == "AK1"
    assert datetime.fromisoformat(doc["Expiration"].replace("Z", "+00:00")).tzinfo is not None
    assert out.err == ""


def test_role_flag_reaches_sts(token_file, capsys):
    sts = FakeSts(900)
    assert run(["--endpoint", ENDPOINT, "--token-file", token_file, "--role", "analyst"], sts) == 0
    assert sts.form(0)["RoleArn"].endswith("role/analyst")


def test_endpoint_from_the_environment(token_file, monkeypatch):
    monkeypatch.setenv("MIST_ENDPOINT", ENDPOINT + "/ignored/path")
    sts = FakeSts(900)
    assert run(["--token-file", token_file], sts) == 0
    assert str(sts.requests[0].url) == ENDPOINT + "/"


def test_principal_key_from_the_two_variables(monkeypatch):
    monkeypatch.setenv("MIST_PRINCIPAL_KEY_ID", "PKID")
    monkeypatch.setenv("MIST_PRINCIPAL_KEY_SECRET", "PKSECRET")
    sts = FakeSts(900)
    assert run(["--endpoint", ENDPOINT], sts) == 0
    assert sts.form(0)["Action"] == "AssumeRole"


def test_token_file_flag_wins_over_the_key_variables(monkeypatch, token_file):
    monkeypatch.setenv("MIST_PRINCIPAL_KEY_ID", "PKID")
    monkeypatch.setenv("MIST_PRINCIPAL_KEY_SECRET", "PKSECRET")
    sts = FakeSts(900)
    assert run(["--endpoint", ENDPOINT, "--token-file", token_file], sts) == 0
    assert sts.form(0)["Action"] == "AssumeRoleWithWebIdentity"


def test_token_file_from_the_environment(monkeypatch, token_file):
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", token_file)
    sts = FakeSts(900)
    assert run(["--endpoint", ENDPOINT], sts) == 0
    assert sts.form(0)["Action"] == "AssumeRoleWithWebIdentity"


@pytest.mark.parametrize("var", ["MIST_PRINCIPAL_KEY_ID", "MIST_PRINCIPAL_KEY_SECRET"])
def test_one_key_variable_alone_fails(monkeypatch, capsys, token_file, var):
    monkeypatch.setenv(var, "only-one-secret")
    monkeypatch.setenv("MIST_IDENTITY_TOKEN_FILE", token_file)
    sts = FakeSts(900)
    assert run(["--endpoint", ENDPOINT], sts) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "MIST_PRINCIPAL_KEY_ID" in out.err and "MIST_PRINCIPAL_KEY_SECRET" in out.err
    assert "only-one-secret" not in out.err
    assert sts.requests == []


def test_no_identity_source_fails(capsys):
    sts = FakeSts(900)
    assert run(["--endpoint", ENDPOINT], sts) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "MIST_IDENTITY_TOKEN_FILE" in out.err and "AWS_WEB_IDENTITY_TOKEN_FILE" in out.err
    assert sts.requests == []


def test_scope_flags_reach_sts_as_a_policy(token_file):
    sts = FakeSts(900)
    argv = ["--endpoint", ENDPOINT, "--token-file", token_file, "--read", "b/p/", "--write", "b/q/",
            "--delete", "b/r/", "--read", "c"]
    assert run(argv, sts) == 0
    expected = scope_policy(read=["b/p/", "c"], write=["b/q/"], delete=["b/r/"])
    assert json.loads(sts.form(0)["Policy"]) == expected


def test_no_scope_flags_send_no_policy(token_file):
    sts = FakeSts(900)
    assert run(["--endpoint", ENDPOINT, "--token-file", token_file], sts) == 0
    assert "Policy" not in sts.form(0)


def test_failure_prints_to_stderr_only_and_returns_1(token_file, capsys):
    sts = FakeSts((403, error_xml("AccessDenied", "no")))
    assert run(["--endpoint", ENDPOINT, "--token-file", token_file], sts) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "AccessDenied" in out.err
    assert out.err.count("\n") == 1
    assert "the-jwt-secret" not in out.err


def test_unreadable_token_file_fails_without_stdout(tmp_path, capsys):
    sts = FakeSts(900)
    assert run(["--endpoint", ENDPOINT, "--token-file", str(tmp_path / "nope")], sts) == 1
    assert capsys.readouterr().out == ""
    assert sts.requests == []


def test_missing_endpoint_returns_1(token_file, capsys):
    sts = FakeSts(900)
    assert run(["--token-file", token_file], sts) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "MIST_ENDPOINT" in out.err
    assert sts.requests == []


def test_bad_endpoint_returns_1(token_file, capsys):
    assert run(["--endpoint", "nonsense", "--token-file", token_file], FakeSts(900)) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("endpoint", ["http://host:abc", "http://["])
def test_a_malformed_endpoint_is_one_stderr_line(token_file, capsys, endpoint):
    assert run(["--endpoint", endpoint, "--token-file", token_file], FakeSts(900)) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.count("\n") == 1 and "Traceback" not in out.err


def test_an_unexpected_exception_prints_only_its_class(token_file, capsys):
    def boom(request):
        raise RuntimeError("the-jwt-secret leaked")

    assert run(["--endpoint", ENDPOINT, "--token-file", token_file], boom) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "mist-credentials: RuntimeError\n"
