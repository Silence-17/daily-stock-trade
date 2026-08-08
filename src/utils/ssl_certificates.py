# -*- coding: utf-8 -*-
"""TLS CA bundle compatibility helpers for native Windows HTTP clients."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Optional

import certifi


CA_ENV_KEYS = ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE")


def _configured_ca_bundle() -> Optional[Path]:
    for key in CA_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_file():
            return path.resolve()
    return None


def _ascii_runtime_root() -> Path:
    candidates = (
        os.environ.get("PROGRAMDATA"),
        os.environ.get("TEMP"),
        tempfile.gettempdir(),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text and text.isascii():
            return Path(text).resolve()
    raise RuntimeError("ascii_runtime_directory_unavailable")


def ensure_ascii_ca_bundle() -> Dict[str, object]:
    """Point native curl clients at an ASCII-safe copy of the trusted CA bundle."""

    source = _configured_ca_bundle() or Path(certifi.where()).resolve()
    if not source.is_file():
        raise RuntimeError("ca_bundle_unavailable")

    if str(source).isascii():
        target = source
        copied = False
    else:
        content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        target_dir = _ascii_runtime_root() / "daily-stock-analysis" / "certificates"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"certifi-{content_hash[:16]}.pem"
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == content_hash:
            copied = False
        else:
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            copied = True

    target_text = str(target)
    for key in CA_ENV_KEYS:
        os.environ[key] = target_text
    return {
        "configured": True,
        "source_was_ascii": str(source).isascii(),
        "target_is_ascii": target_text.isascii(),
        "copied": copied,
        "target": target_text,
    }
