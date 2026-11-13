import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def s3(monkeypatch):
    """An in-process fake S3 (moto) with one bucket, `agent-files`. No network."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(var, "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="agent-files")
        yield client
