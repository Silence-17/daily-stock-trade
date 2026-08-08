# -*- coding: utf-8 -*-
"""Run the cross-market acceptance replay from vendor-supplied JSONL frames."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.v1.schemas.vnpy_paper_trading import CrossMarketReplayFrameInput  # noqa: E402
from src.services.cross_market_acceptance_service import CrossMarketAcceptanceService  # noqa: E402
from src.services.cross_market_backtest_service import CrossMarketBacktestService, ReplayFrame  # noqa: E402
from src.services.cross_market_paper_strategy import MinuteBar  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and replay real cross-market historical frames without lookahead.",
    )
    parser.add_argument("--manifest", required=True, help="Dataset provenance manifest JSON path")
    parser.add_argument("--frames", required=True, help="UTF-8 JSONL replay frame path")
    parser.add_argument("--output", required=True, help="Replay result JSON path")
    parser.add_argument(
        "--acceptance-state",
        default="data/cross_market_strategy_acceptance.json",
        help="Persistent acceptance state JSON path",
    )
    parser.add_argument("--minimum-sessions", type=int, default=500)
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    return parser


def _load_manifest(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest_must_be_object")
    if str(payload.get("dataset_kind") or "").strip() != "historical_market":
        raise ValueError("manifest_dataset_kind_must_be_historical_market")
    if not str(payload.get("dataset_id") or "").strip():
        raise ValueError("manifest_dataset_id_required")
    sources = payload.get("data_sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("manifest_data_sources_required")
    if not str(payload.get("minute_bar_source") or "").strip():
        raise ValueError("manifest_minute_bar_source_required")
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("manifest_schema_version_must_be_1")
    _require_aware_datetime(payload.get("generated_at"), "manifest_generated_at_required")

    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("manifest_coverage_required")
    try:
        coverage_start = date.fromisoformat(str(coverage.get("start_date") or ""))
        coverage_end = date.fromisoformat(str(coverage.get("end_date") or ""))
    except ValueError as exc:
        raise ValueError("manifest_coverage_dates_invalid") from exc
    if coverage_start > coverage_end:
        raise ValueError("manifest_coverage_dates_reversed")

    root = path.resolve().parent
    normalized_sources: List[Dict[str, Any]] = []
    source_ids: Set[str] = set()
    artifact_hashes: List[str] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError(f"manifest_data_source_must_be_object:{index}")
        source_id = str(source.get("id") or "").strip()
        provider = str(source.get("provider") or "").strip()
        dataset = str(source.get("dataset") or "").strip()
        roles = sorted({str(item).strip() for item in source.get("roles") or [] if str(item).strip()})
        if not source_id or source_id in source_ids:
            raise ValueError(f"manifest_data_source_id_invalid:{index}")
        if not provider or not dataset or not roles:
            raise ValueError(f"manifest_data_source_metadata_required:{source_id}")
        _require_aware_datetime(
            source.get("retrieved_at"),
            f"manifest_data_source_retrieved_at_required:{source_id}",
        )
        artifact = _verify_artifact(
            root,
            source.get("artifact"),
            reason_prefix=f"manifest_data_source_artifact:{source_id}",
        )
        source_ids.add(source_id)
        artifact_hashes.append(f"{source_id}:{artifact['sha256']}")
        normalized_sources.append({
            **source,
            "id": source_id,
            "provider": provider,
            "dataset": dataset,
            "roles": roles,
            "artifact": artifact,
        })

    minute_source = str(payload["minute_bar_source"]).strip()
    minute_entry = next((item for item in normalized_sources if item["id"] == minute_source), None)
    if minute_entry is None or "cn_minute" not in minute_entry["roles"]:
        raise ValueError("manifest_minute_bar_source_must_reference_cn_minute_role")

    frames_artifact = payload.get("frames_artifact")
    if not isinstance(frames_artifact, dict):
        raise ValueError("manifest_frames_artifact_required")
    _validate_artifact_descriptor(frames_artifact, "manifest_frames_artifact")
    try:
        frame_count = int(frames_artifact.get("record_count") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest_frames_record_count_invalid") from exc
    if frame_count <= 0:
        raise ValueError("manifest_frames_record_count_invalid")

    aggregate = "\n".join(sorted(artifact_hashes)).encode("utf-8")
    return {
        **payload,
        "coverage": {
            "start_date": coverage_start.isoformat(),
            "end_date": coverage_end.isoformat(),
        },
        "data_sources": normalized_sources,
        "minute_bar_source": minute_source,
        "frames_artifact": {
            **frames_artifact,
            "record_count": frame_count,
            "sha256": str(frames_artifact["sha256"]).strip().lower(),
        },
        "verification": {
            "schema_version": 1,
            "source_artifacts_verified": True,
            "raw_artifact_count": len(normalized_sources),
            "raw_artifacts_sha256": hashlib.sha256(aggregate).hexdigest(),
        },
    }


def _require_aware_datetime(value: Any, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(reason) from exc
    if parsed.tzinfo is None:
        raise ValueError(reason)
    if parsed.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError(f"{reason}:future")
    return parsed.astimezone(timezone.utc)


def _validate_artifact_descriptor(artifact: Dict[str, Any], reason_prefix: str) -> None:
    relative_path = str(artifact.get("path") or "").strip()
    sha256 = str(artifact.get("sha256") or "").strip().lower()
    if not relative_path:
        raise ValueError(f"{reason_prefix}_path_required")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError(f"{reason_prefix}_sha256_invalid")
    try:
        size_bytes = int(artifact.get("size_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{reason_prefix}_size_invalid") from exc
    if size_bytes <= 0:
        raise ValueError(f"{reason_prefix}_size_invalid")


def _verify_artifact(root: Path, artifact: Any, *, reason_prefix: str) -> Dict[str, Any]:
    if not isinstance(artifact, dict):
        raise ValueError(f"{reason_prefix}_required")
    _validate_artifact_descriptor(artifact, reason_prefix)
    artifact_path = (root / str(artifact["path"])).resolve()
    if not artifact_path.is_relative_to(root) or not artifact_path.is_file():
        raise ValueError(f"{reason_prefix}_path_unavailable")
    actual_size = artifact_path.stat().st_size
    if actual_size != int(artifact["size_bytes"]):
        raise ValueError(f"{reason_prefix}_size_mismatch")
    actual_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if actual_hash != str(artifact["sha256"]).strip().lower():
        raise ValueError(f"{reason_prefix}_sha256_mismatch")
    return {
        **artifact,
        "path": str(artifact["path"]).replace("\\", "/"),
        "size_bytes": actual_size,
        "sha256": actual_hash,
    }


def _load_frames(path: Path, manifest: Optional[Dict[str, Any]] = None) -> List[ReplayFrame]:
    frames: List[ReplayFrame] = []
    source_roles = {
        str(item["id"]): set(item.get("roles") or [])
        for item in (manifest or {}).get("data_sources", [])
        if isinstance(item, dict) and item.get("id")
    }
    minute_source = str((manifest or {}).get("minute_bar_source") or "")
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            raw_item = json.loads(line)
            item = CrossMarketReplayFrameInput.model_validate(raw_item)
        except Exception as exc:
            raise ValueError(f"invalid_replay_frame_line:{line_number}:{exc}") from exc
        if manifest is not None:
            _validate_frame_provenance(
                raw_item,
                item.theme,
                line_number=line_number,
                source_roles=source_roles,
                minute_source=minute_source,
            )
        frames.append(
            ReplayFrame(
                session_date=item.session_date,
                signal_at=item.signal_at,
                symbol=item.symbol,
                theme=item.theme,
                cn_gap_pct=item.cn_gap_pct,
                reclaimed_open=item.reclaimed_open,
                above_vwap=item.above_vwap,
                sector_signal_score=item.sector_signal_score,
                expected_gross_edge_pct=item.expected_gross_edge_pct,
                signal_price=item.signal_price,
                next_minute_bar=MinuteBar(**item.next_minute_bar.model_dump()),
                close_price=item.close_price,
                us_tech_score=item.us_tech_score,
                korea_gate=item.korea_gate,
                cpo_signal=item.cpo_signal,
                gold_signal=item.gold_signal,
                range_signal=item.range_signal,
                evidence_timestamps=item.evidence_timestamps,
                instrument_type=item.instrument_type,
                split_ratio=item.split_ratio,
                cash_dividend_per_share=item.cash_dividend_per_share,
                order_cancel_requested=item.order_cancel_requested,
                previous_close=item.previous_close,
                price_limit_pct=item.price_limit_pct,
            )
        )
    if not frames:
        raise ValueError("historical_replay_frames_required")
    return frames


def _validate_frame_provenance(
    raw_item: Dict[str, Any],
    theme: str,
    *,
    line_number: int,
    source_roles: Dict[str, Set[str]],
    minute_source: str,
) -> None:
    provenance = raw_item.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError(f"frame_provenance_required:{line_number}")
    source_ids = {
        str(item).strip()
        for item in provenance.get("source_ids") or []
        if str(item).strip()
    }
    unknown = sorted(source_ids.difference(source_roles))
    if not source_ids or unknown:
        raise ValueError(f"frame_provenance_source_invalid:{line_number}:{','.join(unknown)}")
    if str(provenance.get("minute_bar_source_id") or "").strip() != minute_source:
        raise ValueError(f"frame_provenance_minute_source_mismatch:{line_number}")
    records = provenance.get("record_keys")
    if not isinstance(records, dict) or any(
        not str(records.get(source_id) or "").strip()
        for source_id in source_ids
    ):
        raise ValueError(f"frame_provenance_record_keys_required:{line_number}")
    available_roles: Set[str] = set()
    for source_id in source_ids:
        available_roles.update(source_roles[source_id])
    required_roles = {"cn_signal", "cn_minute"}
    if theme in {"semiconductor", "memory", "equipment", "materials"}:
        required_roles.update({"us_tech", "kr_live"})
    elif theme == "gold":
        required_roles.update({"gold_market", "rate_news"})
    missing_roles = sorted(required_roles.difference(available_roles))
    if missing_roles:
        raise ValueError(
            f"frame_provenance_roles_missing:{line_number}:{','.join(missing_roles)}"
        )


def _verify_frames_binding(
    manifest_path: Path,
    frames_path: Path,
    manifest: Dict[str, Any],
    frames: List[ReplayFrame],
) -> Dict[str, Any]:
    descriptor = manifest["frames_artifact"]
    manifest_root = manifest_path.resolve().parent
    expected_path = (manifest_root / str(descriptor["path"])).resolve()
    actual_path = frames_path.resolve()
    if not expected_path.is_relative_to(manifest_root) or expected_path != actual_path:
        raise ValueError("manifest_frames_artifact_path_mismatch")
    actual_size = actual_path.stat().st_size
    if actual_size != int(descriptor["size_bytes"]):
        raise ValueError("manifest_frames_artifact_size_mismatch")
    actual_hash = hashlib.sha256(actual_path.read_bytes()).hexdigest()
    if actual_hash != str(descriptor["sha256"]):
        raise ValueError("manifest_frames_artifact_sha256_mismatch")
    if len(frames) != int(descriptor["record_count"]):
        raise ValueError("manifest_frames_record_count_mismatch")
    sessions = sorted({frame.session_date for frame in frames})
    coverage = manifest["coverage"]
    if sessions[0].isoformat() != coverage["start_date"] or sessions[-1].isoformat() != coverage["end_date"]:
        raise ValueError("manifest_coverage_does_not_match_frames")
    return {
        "verified": True,
        "frames_sha256": actual_hash,
        "frame_count": len(frames),
        "session_start": sessions[0].isoformat(),
        "session_end": sessions[-1].isoformat(),
    }


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    args = _parser().parse_args()
    manifest_path = Path(args.manifest).resolve()
    frames_path = Path(args.frames).resolve()
    manifest = _load_manifest(manifest_path)
    frames = _load_frames(frames_path, manifest=manifest)
    frames_binding = _verify_frames_binding(
        manifest_path,
        frames_path,
        manifest,
        frames,
    )
    if args.minimum_sessions < 500:
        raise ValueError("minimum_sessions_must_be_at_least_500_for_historical_acceptance")
    result = CrossMarketBacktestService(initial_cash=args.initial_cash).run_ablation_suite(
        frames,
        minimum_sessions=args.minimum_sessions,
    )
    verification = manifest["verification"]
    request_evidence = {
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "frames_sha256": frames_binding["frames_sha256"],
        "frame_count": len(frames),
        "provenance_schema_version": verification["schema_version"],
        "source_artifacts_verified": verification["source_artifacts_verified"],
        "raw_artifact_count": verification["raw_artifact_count"],
        "raw_artifacts_sha256": verification["raw_artifacts_sha256"],
        "frames_binding_verified": frames_binding["verified"],
        "minimum_sessions": args.minimum_sessions,
        "initial_cash": args.initial_cash,
    }
    result["acceptance_evidence"] = CrossMarketAcceptanceService(
        state_path=Path(args.acceptance_state),
    ).record_backtest(
        result=result,
        dataset_kind="historical_market",
        dataset_id=str(manifest["dataset_id"]),
        data_sources=[str(item["id"]) for item in manifest["data_sources"]],
        minute_bar_source=str(manifest["minute_bar_source"]),
        request_payload=request_evidence,
    )
    result["dataset"] = request_evidence
    _write_json_atomic(Path(args.output), result)
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "session_count": result["session_count"],
        "validation": result["validation"],
        "historical_eligible": result["acceptance_evidence"]["historical_eligible"],
    }, ensure_ascii=False))
    return 0 if result["validation"].get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
