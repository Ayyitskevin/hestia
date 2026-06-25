"""Local object-storage backend."""

import io

import pytest

from hestia.storage import LocalStorage, image_key


def test_put_open_roundtrip(tmp_path):
    s = LocalStorage(tmp_path)
    key = s.put("t1/1/9.jpg", io.BytesIO(b"hello"), "image/jpeg")
    assert key == "t1/1/9.jpg"
    assert s.exists(key)
    assert s.open(key) == b"hello"
    assert s.public_path(key) == "/media/t1/1/9.jpg"


def test_delete(tmp_path):
    s = LocalStorage(tmp_path)
    s.put("a/b/c.png", io.BytesIO(b"x"))
    s.delete("a/b/c.png")
    assert not s.exists("a/b/c.png")


def test_key_traversal_rejected(tmp_path):
    s = LocalStorage(tmp_path)
    with pytest.raises(ValueError):
        s.put("../../etc/passwd", io.BytesIO(b"x"))


def test_image_key_format():
    assert image_key("tenant", 3, 7, ".JPG") == "tenant/3/7.jpg"
    assert image_key("tenant", 3, 7, "png") == "tenant/3/7.png"
