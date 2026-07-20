# -*- coding: utf-8 -*-

from datetime import date
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from data_provider.bse_corporate_action_fetcher import BseCorporateActionFetcher


class BseCorporateActionFetcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        BseCorporateActionFetcher.clear_cache()
        self.session = MagicMock()
        self.fetcher = BseCorporateActionFetcher(
            session=self.session,
            today=date(2026, 7, 20),
        )
        self.fetcher.lifecycle.get_legacy_code_map = MagicMock(return_value={})

    @staticmethod
    def _announcement() -> dict:
        return {
            "companyCd": "920425",
            "disclosureTitle": "[临时公告]乐创技术:2025年年度权益分派实施公告",
            "destFilePath": "/disclosure/2026/example.pdf",
            "publishDate": "2026-07-16",
        }

    def _pdf_response(self) -> MagicMock:
        response = MagicMock()
        response.content = b"%PDF-test"
        response.raise_for_status.return_value = None
        self.session.get.return_value = response
        return response

    def test_parses_cash_and_combined_send_transfer_ratio(self) -> None:
        self._pdf_response()
        text = (
            "证券代码：920425 2025年年度权益分派实施公告 "
            "权益分派方案为：每 10 股送红股 1 股，每 10 股转增 4 股，"
            "每 10 股派 2.50 元人民币现金。分红前总股本为100股。"
            "除权除息日为：2026 年 7 月 23 日"
        )
        reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)])
        with patch(
            "data_provider.bse_corporate_action_fetcher.PdfReader",
            return_value=reader,
        ):
            events = self.fetcher._parse_announcement(
                "920425",
                self._announcement(),
            )

        self.assertEqual([item["action_type"] for item in events], [
            "cash_dividend",
            "split_adjustment",
        ])
        self.assertEqual(events[0]["effective_date"], date(2026, 7, 23))
        self.assertEqual(events[0]["cash_dividend_per_share"], 0.25)
        self.assertEqual(events[1]["split_ratio"], 1.5)
        self.assertTrue(all(
            item["source"] == "bse.companyAnnouncement.pdf" for item in events
        ))

    def test_symbol_mismatch_and_missing_ex_date_fail_closed(self) -> None:
        self._pdf_response()
        mismatch = (
            "证券代码：920999 权益分派实施公告 权益分派方案为："
            "每10股派1元现金。分红前总股本100股。"
            "除权除息日为：2026年7月23日"
        )
        no_date = (
            "证券代码：920425 权益分派实施公告 权益分派方案为："
            "每10股派1元现金。分红前总股本100股。"
        )
        for text, message in ((mismatch, "symbol mismatch"), (no_date, "ex-date")):
            reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)])
            with (
                patch(
                    "data_provider.bse_corporate_action_fetcher.PdfReader",
                    return_value=reader,
                ),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                self.fetcher._parse_announcement("920425", self._announcement())

    def test_official_legacy_symbol_is_accepted_for_historical_pdf(self) -> None:
        self._pdf_response()
        self.fetcher.lifecycle.get_legacy_code_map.return_value = {
            "920425": "430425",
        }
        text = (
            "证券代码：430425 权益分派实施公告 权益分派方案为："
            "每10股派1元现金。分红前总股本100股。"
            "除权除息日为：2024年7月23日"
        )
        reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)])
        with patch(
            "data_provider.bse_corporate_action_fetcher.PdfReader",
            return_value=reader,
        ):
            events = self.fetcher._parse_announcement(
                "920425",
                self._announcement(),
            )

        self.assertEqual(events[0]["symbol"], "920425")
        self.assertEqual(events[0]["cash_dividend_per_share"], 0.1)

    def test_announcement_api_filters_non_implementation_titles(self) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.text = "callback([{" + (
            '"listInfo":{"content":['
            '{"companyCd":"920425","disclosureTitle":"2025年年度权益分派实施公告",'
            '"destFilePath":"/disclosure/ok.pdf","publishDate":"2026-07-16"},'
            '{"companyCd":"920425","disclosureTitle":"2025年年度权益分派预案公告",'
            '"destFilePath":"/disclosure/no.pdf","publishDate":"2026-04-01"}'
            '],"lastPage":true}'
        ) + "}])"
        self.session.post.return_value = response

        rows = self.fetcher._fetch_announcements("920425")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["destFilePath"], "/disclosure/ok.pdf")

    def test_announcement_api_queries_current_and_official_legacy_codes(self) -> None:
        self.fetcher.lifecycle.get_legacy_code_map.return_value = {
            "920425": "430425",
        }

        def response_for(code: str, path: str) -> MagicMock:
            response = MagicMock()
            response.raise_for_status.return_value = None
            payload = [{"listInfo": {"content": [{
                "companyCd": code,
                "disclosureTitle": "年度权益分派实施公告",
                "destFilePath": path,
                "publishDate": "2025-01-01",
            }], "lastPage": True}}]
            response.text = f"callback({json.dumps(payload, ensure_ascii=False)})"
            return response

        self.session.post.side_effect = [
            response_for("920425", "/disclosure/current.pdf"),
            response_for("430425", "/disclosure/legacy.pdf"),
        ]

        rows = self.fetcher._fetch_announcements("920425")

        self.assertEqual(
            {row["destFilePath"] for row in rows},
            {"/disclosure/current.pdf", "/disclosure/legacy.pdf"},
        )
        queried_codes = [
            dict(call.kwargs["data"])["companyCd"]
            for call in self.session.post.call_args_list
        ]
        self.assertEqual(queried_codes, ["920425", "430425"])

    def test_get_filters_dates_and_uses_symbol_cache(self) -> None:
        events = [{
            "symbol": "920425",
            "effective_date": date(2026, 7, 23),
            "action_type": "cash_dividend",
            "cash_dividend_per_share": 0.25,
            "source": "bse.companyAnnouncement.pdf",
            "source_record_key": "official",
        }]
        with (
            patch.object(
                self.fetcher,
                "_fetch_announcements",
                return_value=[self._announcement()],
            ) as announcements,
            patch.object(
                self.fetcher,
                "_parse_announcement",
                return_value=events,
            ),
        ):
            included = self.fetcher.get_stock_corporate_actions(
                "920425",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 31),
            )
            excluded = self.fetcher.get_stock_corporate_actions(
                "920425",
                end_date=date(2026, 7, 22),
            )

        self.assertEqual(len(included), 1)
        self.assertEqual(excluded, [])
        announcements.assert_called_once_with("920425")


if __name__ == "__main__":
    unittest.main()
