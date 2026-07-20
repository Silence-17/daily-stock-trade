# -*- coding: utf-8 -*-
"""Implemented BSE corporate actions parsed from official announcement PDFs."""

from __future__ import annotations

import re
import threading
import time
from datetime import date
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from pypdf import PdfReader
import requests

from data_provider.base import is_bse_code, normalize_stock_code
from data_provider.bse_lifecycle_fetcher import BSE_OPEN_DATE, BseLifecycleFetcher


_IMPLEMENTATION_KEYWORD = "\u5206\u6d3e\u5b9e\u65bd"
_IMPLEMENTATION_TITLE = "\u6743\u76ca\u5206\u6d3e\u5b9e\u65bd\u516c\u544a"


class BseCorporateActionFetcher:
    """Normalize BSE actions only from official implementation announcements."""

    CACHE_TTL_SECONDS = 60 * 60
    MAX_PAGES = 100
    MAX_PDF_BYTES = 10 * 1024 * 1024
    MAX_PDF_PAGES = 30
    _cache_lock = threading.Lock()
    _cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
    _mapping_cache: Optional[Tuple[float, Dict[str, str]]] = None

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 30.0,
        today: Optional[date] = None,
    ) -> None:
        self.lifecycle = BseLifecycleFetcher(
            session=session,
            timeout_seconds=timeout_seconds,
            today=today,
        )
        self.session = self.lifecycle.session
        self.timeout_seconds = timeout_seconds
        self.today = today or date.today()

    def get_stock_corporate_actions(
        self,
        stock_code: str,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        code = normalize_stock_code(stock_code)
        if not is_bse_code(code):
            raise ValueError("BSE corporate-action source requires a 920xxx symbol")
        events = self._load(code)
        return [
            dict(item)
            for item in events
            if (start_date is None or item["effective_date"] >= start_date)
            and (end_date is None or item["effective_date"] <= end_date)
        ]

    def _load(self, code: str) -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(code)
            if cached and now - cached[0] < self.CACHE_TTL_SECONDS:
                return [dict(item) for item in cached[1]]

        announcements = self._fetch_announcements(code)
        events_by_key: Dict[Tuple[date, str], Dict[str, Any]] = {}
        for announcement in sorted(
            announcements,
            key=lambda item: str(item.get("publishDate") or ""),
        ):
            for event in self._parse_announcement(code, announcement):
                events_by_key[(event["effective_date"], event["action_type"])] = event
        events = sorted(
            events_by_key.values(),
            key=lambda item: (item["effective_date"], item["action_type"]),
        )
        with self._cache_lock:
            self._cache[code] = (time.monotonic(), [dict(item) for item in events])
        return events

    def _fetch_announcements(self, code: str) -> List[Dict[str, Any]]:
        fields = [
            "companyCd",
            "companyName",
            "disclosureTitle",
            "destFilePath",
            "publishDate",
            "xxfcbj",
            "fileExt",
        ]
        legacy_code = self._legacy_code_map().get(code)
        query_codes = [code, *([legacy_code] if legacy_code else [])]
        rows_by_path: Dict[str, Dict[str, Any]] = {}
        for query_code in query_codes:
            for page in range(self.MAX_PAGES):
                data: List[Tuple[str, str]] = [
                    ("disclosureType[]", "5"),
                    ("disclosureSubtype[]", ""),
                    ("page", str(page)),
                    ("companyCd", query_code),
                    ("isNewThree", "1"),
                    ("startTime", BSE_OPEN_DATE.isoformat()),
                    ("endTime", self.today.isoformat()),
                    ("keyword", _IMPLEMENTATION_KEYWORD),
                    ("xxfcbj[]", "2"),
                    ("hyType", ""),
                ]
                data.extend(("needFields[]", field) for field in fields)
                response = self.session.post(
                    self.lifecycle.ANNOUNCEMENT_URL,
                    data=data,
                    headers={
                        **self.lifecycle.headers,
                        "Referer": (
                            f"{self.lifecycle.BASE_URL}/disclosure/announcement.html"
                        ),
                    },
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = self.lifecycle._parse_jsonp(response.text)
                if not payload or not isinstance(payload[0], dict):
                    raise RuntimeError(
                        "BSE corporate-action endpoint returned an invalid payload"
                    )
                page_data = payload[0].get("listInfo") or {}
                for item in page_data.get("content", []):
                    title = str(item.get("disclosureTitle") or "")
                    path = str(item.get("destFilePath") or "").strip()
                    if (
                        str(item.get("companyCd") or "").strip() == query_code
                        and _IMPLEMENTATION_TITLE in title
                        and path
                    ):
                        rows_by_path[path] = dict(item)
                if bool(page_data.get("lastPage", True)):
                    break
            else:
                raise RuntimeError(
                    "BSE corporate-action endpoint exceeded pagination limit"
                )
        return list(rows_by_path.values())

    def _parse_announcement(
        self,
        code: str,
        announcement: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        path = str(announcement.get("destFilePath") or "").strip()
        if not path.startswith("/disclosure/") or not path.lower().endswith(".pdf"):
            raise RuntimeError("BSE implementation announcement has an invalid PDF path")
        response = self.session.get(
            f"{self.lifecycle.BASE_URL}{path}",
            headers={
                **self.lifecycle.headers,
                "Referer": f"{self.lifecycle.BASE_URL}/disclosure/announcement.html",
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        if len(response.content) > self.MAX_PDF_BYTES:
            raise RuntimeError("BSE implementation announcement PDF exceeds size limit")
        reader = PdfReader(BytesIO(response.content))
        if not 1 <= len(reader.pages) <= self.MAX_PDF_PAGES:
            raise RuntimeError("BSE implementation announcement PDF has invalid page count")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        compact = re.sub(r"\s+", "", text)
        if _IMPLEMENTATION_TITLE not in compact:
            raise RuntimeError("BSE PDF is not an implementation announcement")
        code_match = re.search(r"\u8bc1\u5238\u4ee3\u7801[:\uff1a]?(\d{6})", compact)
        accepted_codes = {code}
        legacy_code = self._legacy_code_map().get(code)
        if legacy_code:
            accepted_codes.add(legacy_code)
        if code_match is None or code_match.group(1) not in accepted_codes:
            raise RuntimeError("BSE implementation announcement symbol mismatch")

        date_match = re.search(
            r"\u9664\u6743\u9664\u606f\u65e5\u4e3a[:\uff1a]?(\d{4})\u5e74(\d{1,2})\u6708(\d{1,2})\u65e5",
            compact,
        )
        if date_match is None:
            raise RuntimeError("BSE implementation announcement lacks an ex-date")
        effective_date = date(*(int(value) for value in date_match.groups()))
        plan_match = re.search(
            r"\u6743\u76ca\u5206\u6d3e\u65b9\u6848\u4e3a[:\uff1a](.*?)(?:\u5206\u7ea2\u524d|\u7279\u6b8a\u60c5\u51b5\u8bf4\u660e|2\u3001\u6263\u7a0e\u8bf4\u660e)",
            compact,
        )
        if plan_match is None:
            raise RuntimeError("BSE implementation announcement lacks a bounded plan section")
        plan = plan_match.group(1)
        cash_per_ten = self._first_number(plan, (
            r"\u6bcf10\u80a1\u6d3e(?:\u53d1)?(?:\u73b0\u91d1\u7ea2\u5229)?([0-9]+(?:\.[0-9]+)?)\u5143",
            r"\u6bcf10\u80a1\u6d3e\u53d1\u73b0\u91d1\u7ea2\u5229([0-9]+(?:\.[0-9]+)?)\u5143",
        ))
        send_per_ten = self._first_number(plan, (
            r"\u6bcf10\u80a1\u9001(?:\u7ea2\u80a1)?([0-9]+(?:\.[0-9]+)?)\u80a1",
        ))
        transfer_per_ten = self._first_number(plan, (
            r"\u6bcf10\u80a1\u8f6c\u589e([0-9]+(?:\.[0-9]+)?)\u80a1",
        ))
        if cash_per_ten <= 0 and send_per_ten <= 0 and transfer_per_ten <= 0:
            raise RuntimeError("BSE implementation announcement has no positive action ratio")

        source = "bse.companyAnnouncement.pdf"
        source_prefix = f"{path}|{effective_date.isoformat()}"
        events: List[Dict[str, Any]] = []
        if cash_per_ten > 0:
            events.append({
                "symbol": code,
                "effective_date": effective_date,
                "action_type": "cash_dividend",
                "cash_dividend_per_share": round(cash_per_ten / 10.0, 10),
                "source": source,
                "source_record_key": f"{source_prefix}|cash_dividend",
            })
        stock_per_ten = send_per_ten + transfer_per_ten
        if stock_per_ten > 0:
            events.append({
                "symbol": code,
                "effective_date": effective_date,
                "action_type": "split_adjustment",
                "split_ratio": round(1.0 + stock_per_ten / 10.0, 10),
                "source": source,
                "source_record_key": f"{source_prefix}|split_adjustment",
            })
        return events

    @staticmethod
    def _first_number(text: str, patterns: Tuple[str, ...]) -> float:
        for pattern in patterns:
            match = re.search(pattern, text)
            if match is not None:
                return float(match.group(1))
        return 0.0

    def _legacy_code_map(self) -> Dict[str, str]:
        cls = type(self)
        now = time.monotonic()
        with cls._cache_lock:
            cached = cls._mapping_cache
            if cached and now - cached[0] < self.CACHE_TTL_SECONDS:
                return dict(cached[1])
        mapping = self.lifecycle.get_legacy_code_map()
        with cls._cache_lock:
            cls._mapping_cache = (time.monotonic(), dict(mapping))
        return mapping

    @classmethod
    def clear_cache(cls) -> None:
        with cls._cache_lock:
            cls._cache.clear()
            cls._mapping_cache = None
