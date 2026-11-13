"""mist.recipes against the zero-delay local S3 server (docs/recipes/coordination.md)."""
import json

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from mist import recipes


@pytest.fixture
def s3(s3server):
    def make(**kw):
        srv = s3server(default=None, **kw)
        client = boto3.client(
            "s3", endpoint_url=srv.url, region_name="us-east-1",
            aws_access_key_id="AK", aws_secret_access_key="SK",
            config=Config(retries={"mode": "standard", "max_attempts": 1}),
        )
        return client, srv

    return make


def stored(srv, bucket, key):
    return srv.objects["/%s/%s" % (bucket, key)]


def test_write_once_true_then_false_on_412(s3):
    c, srv = s3()
    assert recipes.write_once(c, "b", "k", b"first") is True
    assert recipes.write_once(c, "b", "k", b"second") is False
    assert stored(srv, "b", "k") == b"first"


def test_claim_is_true_when_my_earlier_write_landed(s3):
    c, srv = s3()
    c.put_object(Bucket="b", Key="claims/t1", Body=b"w1")  # my earlier attempt landed; the reply was lost
    assert recipes.claim(c, "b", "t1", "w1") is True
    assert recipes.claim(c, "b", "t1", "w2") is False


def test_update_json_retries_on_412_and_returns_tries(s3):
    c, srv = s3()
    c.put_object(Bucket="b", Key="doc", Body=b'{"n": 1}')
    srv.put_script.append(412)  # the next PUT loses a race
    assert recipes.update_json(c, "b", "doc", lambda d: {"n": d["n"] + 1}) == 2
    assert json.loads(stored(srv, "b", "doc")) == {"n": 2}


def test_update_json_creates_with_if_none_match(s3):
    c, srv = s3()
    assert recipes.update_json(c, "b", "new", lambda d: {"made": True}) == 1
    puts = [s for s in srv.seen if s[0] == "PUT"]
    assert {k.lower(): v for k, v in puts[0][3].items()}["if-none-match"] == "*"
    assert json.loads(stored(srv, "b", "new")) == {"made": True}


def test_commit_pointer_does_not_add_a_file_twice(s3):
    c, srv = s3()
    assert recipes.commit_pointer(c, "b", "ptr", ["f1"]) == 1
    assert recipes.commit_pointer(c, "b", "ptr", ["f1"]) == 1
    assert recipes.commit_pointer(c, "b", "ptr", ["f2"]) == 2
    assert recipes.read_snapshot(c, "b", "ptr") == ["f1", "f2"]


def test_read_snapshot_is_empty_before_the_first_commit(s3):
    c, _ = s3()
    assert recipes.read_snapshot(c, "b", "ptr") == []


def test_other_errors_are_not_swallowed(s3):
    c, _ = s3(deny=["AK"])
    with pytest.raises(ClientError):
        recipes.write_once(c, "b", "k", b"x")
