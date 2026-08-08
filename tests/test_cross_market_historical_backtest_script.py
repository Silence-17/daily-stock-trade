# -*- coding: utf-8 -*-
"""Input and provenance tests for the historical replay CLI."""

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.run_cross_market_historical_backtest import (
    _load_frames,
    _load_manifest,
    _verify_frames_binding,
)


class CrossMarketHistoricalBacktestScriptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_manifest_requires_explicit_real_data_provenance(self) -> None:
        path = self.root / "manifest.json"
        path.write_text(
            json.dumps({
                "dataset_kind": "historical_market",
                "dataset_id": "vendor-2024-2026",
                "data_sources": ["vendor-cross-market"],
            }),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "manifest_minute_bar_source_required"):
            _load_manifest(path)

    def test_loads_timezone_aware_vendor_jsonl_frame(self) -> None:
        path = self.root / "frames.jsonl"
        path.write_text(
            json.dumps({
                "session_date": "2026-07-22",
                "signal_at": "2026-07-22T01:31:00+00:00",
                "symbol": "300308",
                "theme": "cpo",
                "cn_gap_pct": -0.5,
                "reclaimed_open": True,
                "above_vwap": False,
                "sector_signal_score": 75.0,
                "expected_gross_edge_pct": 4.0,
                "signal_price": 10.0,
                "next_minute_bar": {
                    "timestamp": "2026-07-22T01:32:00+00:00",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 1000000,
                    "amount": 10000000,
                },
                "close_price": 10.2,
                "evidence_timestamps": {
                    "cn_open": "2026-07-22T01:30:00+00:00"
                },
            }),
            encoding="utf-8",
        )

        frames = _load_frames(path)

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].theme, "cpo")
        self.assertIsNotNone(frames[0].signal_at.tzinfo)
        self.assertEqual(frames[0].next_minute_bar.vwap, 10.0)

    def test_verifies_raw_artifacts_frame_binding_and_per_frame_sources(self) -> None:
        raw_files = {
            "cn.csv": b"cn vendor records\n",
            "us.csv": b"us vendor records\n",
            "kr.csv": b"kr vendor records\n",
        }
        for filename, content in raw_files.items():
            (self.root / filename).write_bytes(content)
        frame_payload = {
            "session_date": "2026-07-22",
            "signal_at": "2026-07-22T01:31:00+00:00",
            "symbol": "688981",
            "theme": "semiconductor",
            "cn_gap_pct": -0.5,
            "reclaimed_open": True,
            "above_vwap": True,
            "sector_signal_score": 75.0,
            "expected_gross_edge_pct": 4.0,
            "signal_price": 10.0,
            "next_minute_bar": {
                "timestamp": "2026-07-22T01:32:00+00:00",
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 1000000,
                "amount": 10000000,
            },
            "close_price": 10.2,
            "us_tech_score": 0.8,
            "korea_gate": {"buy_allowed": True},
            "evidence_timestamps": {
                "cn_open": "2026-07-22T01:30:00+00:00",
                "us": "2026-07-22T01:30:00+00:00",
                "korea": "2026-07-22T01:30:30+00:00",
            },
            "provenance": {
                "source_ids": ["cn", "us", "kr"],
                "minute_bar_source_id": "cn",
                "record_keys": {
                    "cn": "688981:20260722:0932",
                    "us": "20260721:close",
                    "kr": "20260722:103030",
                },
            },
        }
        frames_path = self.root / "frames.jsonl"
        frames_path.write_text(json.dumps(frame_payload) + "\n", encoding="utf-8")

        sources = []
        source_specs = {
            "cn": ("cn.csv", ["cn_signal", "cn_minute"]),
            "us": ("us.csv", ["us_tech"]),
            "kr": ("kr.csv", ["kr_live"]),
        }
        for source_id, (filename, roles) in source_specs.items():
            content = raw_files[filename]
            sources.append({
                "id": source_id,
                "provider": f"provider-{source_id}",
                "dataset": f"dataset-{source_id}",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "roles": roles,
                "artifact": {
                    "path": filename,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
            })
        frame_bytes = frames_path.read_bytes()
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(
            json.dumps({
                "schema_version": 1,
                "dataset_kind": "historical_market",
                "dataset_id": "vendor-2024-2026",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "coverage": {
                    "start_date": "2026-07-22",
                    "end_date": "2026-07-22",
                },
                "data_sources": sources,
                "minute_bar_source": "cn",
                "frames_artifact": {
                    "path": "frames.jsonl",
                    "size_bytes": len(frame_bytes),
                    "sha256": hashlib.sha256(frame_bytes).hexdigest(),
                    "record_count": 1,
                },
            }),
            encoding="utf-8",
        )

        manifest = _load_manifest(manifest_path)
        frames = _load_frames(frames_path, manifest=manifest)
        binding = _verify_frames_binding(manifest_path, frames_path, manifest, frames)

        self.assertTrue(manifest["verification"]["source_artifacts_verified"])
        self.assertEqual(manifest["verification"]["raw_artifact_count"], 3)
        self.assertTrue(binding["verified"])

    def test_rejects_frame_without_per_source_record_keys(self) -> None:
        manifest = {
            "data_sources": [
                {"id": "cn", "roles": ["cn_signal", "cn_minute"]},
            ],
            "minute_bar_source": "cn",
        }
        path = self.root / "frames.jsonl"
        payload = {
            "session_date": "2026-07-22",
            "signal_at": "2026-07-22T01:31:00+00:00",
            "symbol": "300308",
            "theme": "cpo",
            "cn_gap_pct": -0.5,
            "reclaimed_open": True,
            "above_vwap": False,
            "sector_signal_score": 75.0,
            "expected_gross_edge_pct": 4.0,
            "signal_price": 10.0,
            "next_minute_bar": {
                "timestamp": "2026-07-22T01:32:00+00:00",
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 1000000,
                "amount": 10000000,
            },
            "close_price": 10.2,
            "evidence_timestamps": {
                "cn_open": "2026-07-22T01:30:00+00:00",
            },
            "provenance": {
                "source_ids": ["cn"],
                "minute_bar_source_id": "cn",
                "record_keys": {},
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "frame_provenance_record_keys_required"):
            _load_frames(path, manifest=manifest)


if __name__ == "__main__":
    unittest.main()
