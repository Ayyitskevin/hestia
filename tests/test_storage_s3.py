"""S3/R2 storage backend — exercised offline with moto (no real AWS)."""

import dataclasses
import io

import boto3
import pytest
from moto import mock_aws

from hestia.storage import S3Storage, build_storage


@mock_aws
def test_s3_put_open_exists_delete():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="hestia-test")
    s = S3Storage("hestia-test", client=client)

    s.put("t1/1/9.jpg", io.BytesIO(b"hello"), "image/jpeg")
    assert s.exists("t1/1/9.jpg")
    assert s.open("t1/1/9.jpg") == b"hello"
    s.delete("t1/1/9.jpg")
    assert not s.exists("t1/1/9.jpg")


@mock_aws
def test_s3_public_path_presigned():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="hestia-presign")
    s = S3Storage("hestia-presign", client=client)
    url = s.public_path("k/1.jpg")
    # short-lived presigned GET (SigV2 or SigV4, depending on client config)
    assert "k/1.jpg" in url and "Signature" in url and "Expires" in url


def test_s3_rejects_public_media_base():
    with pytest.raises(ValueError, match="bypass"):
        S3Storage("b", public_base_url="https://cdn.example.com/", client=object())


def test_s3_requires_bucket():
    with pytest.raises(ValueError):
        S3Storage("", client=object())


def test_build_storage_selects_s3(settings):
    s = dataclasses.replace(settings, storage_backend="s3", s3_bucket="my-bucket")
    assert isinstance(build_storage(s), S3Storage)


def test_build_storage_defaults_local(settings):
    from hestia.storage import LocalStorage
    assert isinstance(build_storage(settings), LocalStorage)
