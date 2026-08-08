# -*- coding: utf-8 -*-
"""Tests for ASCII-safe TLS CA bundle configuration."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path

import pytest

from src.utils import ssl_certificates


def _clear_ca_environment(monkeypatch) -> None:
    for key in ssl_certificates.CA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def ascii_runtime_root():
    root = Path(".tmp") / f"ssl-certificates-{uuid.uuid4().hex}"
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_existing_ascii_ca_bundle_is_reused(tmp_path, monkeypatch) -> None:
    if not str(tmp_path.resolve()).isascii():
        pytest.skip("test requires an ASCII temporary path")
    source = tmp_path / "cacert.pem"
    source.write_bytes(b"trusted-ca")
    _clear_ca_environment(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", str(source))

    status = ssl_certificates.ensure_ascii_ca_bundle()

    assert status["source_was_ascii"] is True
    assert status["copied"] is False
    assert status["target"] == str(source.resolve())
    for key in ssl_certificates.CA_ENV_KEYS:
        assert ssl_certificates.os.environ[key] == str(source.resolve())


def test_non_ascii_certifi_bundle_is_copied_to_programdata(
    tmp_path,
    monkeypatch,
    ascii_runtime_root,
) -> None:
    source = tmp_path / "证书" / "cacert.pem"
    source.parent.mkdir()
    source.write_bytes(b"trusted-ca-content")
    _clear_ca_environment(monkeypatch)
    monkeypatch.setattr(
        ssl_certificates,
        "_ascii_runtime_root",
        lambda: ascii_runtime_root,
    )
    monkeypatch.setattr(ssl_certificates.certifi, "where", lambda: str(source))

    first = ssl_certificates.ensure_ascii_ca_bundle()
    second = ssl_certificates.ensure_ascii_ca_bundle()

    target = Path(str(first["target"]))
    assert first["source_was_ascii"] is False
    assert first["target_is_ascii"] is True
    assert first["copied"] is True
    assert second["copied"] is False
    assert target.read_bytes() == source.read_bytes()
    assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
    for key in ssl_certificates.CA_ENV_KEYS:
        assert ssl_certificates.os.environ[key] == str(target)
