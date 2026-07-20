# -*- coding: utf-8 -*-
"""Beijing Stock Exchange lifecycle data from official public endpoints."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import requests


logger = logging.getLogger(__name__)

BSE_OPEN_DATE = date(2021, 11, 15)
_FINAL_DELIST_KEYWORD = "\u7ec8\u6b62\u4e0a\u5e02\u66a8\u6458\u724c"
_FINAL_DELIST_TITLE = "\u5173\u4e8e\u516c\u53f8\u80a1\u7968\u7ec8\u6b62\u4e0a\u5e02\u66a8\u6458\u724c\u7684\u516c\u544a"


class _TableCellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell_parts: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            text = str(data or "").strip()
            if text:
                self._cell_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._row is not None and self._cell_parts is not None:
            self._row.append(" ".join(self._cell_parts).strip())
            self._cell_parts = None
        elif tag.lower() == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


class BseLifecycleFetcher:
    """Build a complete, fail-closed BSE lifecycle snapshot."""

    BASE_URL = "https://www.bse.cn"
    CURRENT_URL = f"{BASE_URL}/nqxxController/nqxxCnzq.do"
    CODE_MAPPING_URL = f"{BASE_URL}/service/code_mapping.html"
    ANNOUNCEMENT_URL = f"{BASE_URL}/disclosureInfoController/companyAnnouncement.do"
    MAX_PAGES = 100

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 20.0,
        today: Optional[date] = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.today = today or date.today()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            ),
        }

    def get_stock_lifecycle_list(self) -> pd.DataFrame:
        current = self._fetch_current_rows()
        mapping = self._fetch_code_mapping_rows()
        delisted = self._fetch_final_delist_rows()

        current_by_code = {row["code"]: row for row in current}
        mapping_by_code = {row["code"]: row for row in mapping}
        delisted_by_code = {row["code"]: row for row in delisted}

        unknown_delisted = sorted(set(delisted_by_code) - (set(current_by_code) | set(mapping_by_code)))
        if unknown_delisted:
            raise RuntimeError(
                "BSE delisted symbols lack an authoritative listing-date row: "
                + ", ".join(unknown_delisted)
            )

        unexplained_missing = sorted((set(mapping_by_code) - set(current_by_code)) - set(delisted_by_code))
        if unexplained_missing:
            raise RuntimeError(
                "BSE code-mapping symbols are absent from the current list without a final delisting notice: "
                + ", ".join(unexplained_missing)
            )

        combined: Dict[str, Dict[str, Any]] = dict(mapping_by_code)
        combined.update(current_by_code)
        for code, event in delisted_by_code.items():
            row = dict(combined[code])
            row.update({
                "name": event.get("name") or row.get("name"),
                "list_status": "D",
                "delist_date": event["delist_date"],
            })
            combined[code] = row

        frame = pd.DataFrame(sorted(combined.values(), key=lambda item: item["code"]))
        if frame.empty:
            raise RuntimeError("BSE official lifecycle sources returned no stock rows")
        frame = frame[[
            "code",
            "name",
            "industry",
            "market",
            "exchange",
            "list_status",
            "list_date",
            "delist_date",
        ]]
        frame.attrs["lifecycle_methodology"] = {
            "source": "bse.official_lifecycle",
            "sources": [
                "bse.nqxxCnzq",
                "bse.code_mapping",
                "bse.companyAnnouncement",
            ],
            "coverage_exchanges": ["BSE"],
            "includes_delisted_symbols": True,
            "uses_current_universe_fallback": False,
            "point_in_time_name_and_industry": False,
            "permission_requirement": None,
            "delist_date_semantics": "last_trading_date_from_final_delist_notice_publish_date",
            "coverage_validation": {
                "current_count": len(current_by_code),
                "code_mapping_count": len(mapping_by_code),
                "delisted_count": len(delisted_by_code),
                "unexplained_missing_count": 0,
                "unknown_delisted_count": 0,
            },
        }
        return frame

    def _fetch_current_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for page in range(self.MAX_PAGES):
            response = self.session.post(
                self.CURRENT_URL,
                data={
                    "page": str(page),
                    "typejb": "T",
                    "xxfcbj[]": "2",
                    "xxzqdm": "",
                    "sortfield": "xxzqdm",
                    "sorttype": "asc",
                },
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = self._parse_jsonp(response.text)
            if not payload or not isinstance(payload[0], dict):
                raise RuntimeError("BSE current stock endpoint returned an invalid payload")
            page_data = payload[0]
            rows.extend(self._normalize_current_row(item) for item in page_data.get("content", []))
            if bool(page_data.get("lastPage")) or page + 1 >= int(page_data.get("totalPages") or 1):
                break
        else:
            raise RuntimeError("BSE current stock endpoint exceeded the pagination safety limit")
        return [row for row in rows if row is not None]

    def _fetch_code_mapping_rows(self) -> List[Dict[str, Any]]:
        response = self.session.get(
            self.CODE_MAPPING_URL,
            headers=self.headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        parser = _TableCellParser()
        parser.feed(response.text)
        rows = []
        for values in parser.rows:
            if len(values) < 5:
                continue
            old_code, new_code = values[-2:]
            if not re.fullmatch(r"\d{6}", old_code) or not re.fullmatch(r"920\d{3}", new_code):
                continue
            listed = self._normalize_list_date(values[-3])
            rows.append({
                "code": new_code,
                "name": values[-4].strip(),
                "industry": None,
                "market": "BSE",
                "exchange": "BSE",
                "list_status": "L",
                "list_date": listed,
                "delist_date": None,
            })
        if not rows:
            raise RuntimeError("BSE code-mapping page returned no parseable rows")
        return rows

    def _fetch_final_delist_rows(self) -> List[Dict[str, Any]]:
        fields = [
            "companyCd",
            "companyName",
            "disclosureTitle",
            "destFilePath",
            "publishDate",
            "xxfcbj",
            "fileExt",
        ]
        rows: List[Dict[str, Any]] = []
        for page in range(self.MAX_PAGES):
            data: List[Tuple[str, str]] = [
                ("disclosureType[]", "5"),
                ("disclosureSubtype[]", ""),
                ("page", str(page)),
                ("companyCd", ""),
                ("isNewThree", "1"),
                ("startTime", BSE_OPEN_DATE.isoformat()),
                ("endTime", self.today.isoformat()),
                ("keyword", _FINAL_DELIST_KEYWORD),
                ("xxfcbj[]", "2"),
                ("hyType", ""),
            ]
            data.extend(("needFields[]", field) for field in fields)
            response = self.session.post(
                self.ANNOUNCEMENT_URL,
                data=data,
                headers={**self.headers, "Referer": f"{self.BASE_URL}/disclosure/announcement.html"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = self._parse_jsonp(response.text)
            if not payload or not isinstance(payload[0], dict):
                raise RuntimeError("BSE announcement endpoint returned an invalid payload")
            page_data = payload[0].get("listInfo") or {}
            for item in page_data.get("content", []):
                title = str(item.get("disclosureTitle") or "")
                code = str(item.get("companyCd") or "").strip()
                if _FINAL_DELIST_TITLE not in title or not re.fullmatch(r"920\d{3}", code):
                    continue
                rows.append({
                    "code": code,
                    "name": str(item.get("companyName") or "").strip(),
                    "delist_date": self._normalize_date(item.get("publishDate")),
                })
            if bool(page_data.get("lastPage", True)):
                break
        else:
            raise RuntimeError("BSE announcement endpoint exceeded the pagination safety limit")
        return rows

    @staticmethod
    def _parse_jsonp(text: str) -> Any:
        start = str(text or "").find("[")
        end = str(text or "").rfind("]")
        if start < 0 or end < start:
            raise RuntimeError("BSE endpoint returned malformed JSONP")
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("BSE endpoint returned malformed JSONP") from exc

    @classmethod
    def _normalize_list_date(cls, value: Any) -> str:
        parsed = cls._parse_date(value)
        if parsed is None:
            raise RuntimeError(f"BSE lifecycle row has an invalid listing date: {value}")
        return max(parsed, BSE_OPEN_DATE).isoformat()

    @staticmethod
    def _normalize_date(value: Any) -> str:
        parsed = BseLifecycleFetcher._parse_date(value)
        if parsed is None:
            raise RuntimeError(f"BSE lifecycle row has an invalid date: {value}")
        return parsed.isoformat()

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date()

    @classmethod
    def _normalize_current_row(cls, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        code = str(item.get("xxzqdm") or "").strip()
        name = str(item.get("xxzqjc") or "").strip()
        if not re.fullmatch(r"920\d{3}", code) or not name:
            return None
        return {
            "code": code,
            "name": name,
            "industry": str(item.get("xxhyzl") or "").strip() or None,
            "market": "BSE",
            "exchange": "BSE",
            "list_status": "L",
            "list_date": cls._normalize_list_date(item.get("fxssrq")),
            "delist_date": None,
        }
