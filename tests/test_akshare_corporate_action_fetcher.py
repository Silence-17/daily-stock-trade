# -*- coding: utf-8 -*-

from datetime import date
import unittest
from unittest.mock import MagicMock

import pandas as pd

from data_provider.akshare_corporate_action_fetcher import (
    AkshareCorporateActionFetcher,
)


class AkshareCorporateActionFetcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        AkshareCorporateActionFetcher.clear_cache()

    def test_eastmoney_normalizes_only_implemented_events_and_filters_dates(self) -> None:
        client = MagicMock()
        client.stock_fhps_detail_em.return_value = pd.DataFrame([
            {
                "除权除息日": date(2024, 6, 20),
                "方案进度": "实施分配",
                "现金分红-现金分红比例": 3.0,
                "送转股份-送转总比例": 2.0,
                "送转股份-送股比例": 1.0,
                "送转股份-转股比例": 1.0,
                "最新公告日期": date(2024, 6, 10),
            },
            {
                "除权除息日": date(2025, 6, 20),
                "方案进度": "股东大会预案",
                "现金分红-现金分红比例": 5.0,
                "送转股份-送转总比例": 0.0,
                "送转股份-送股比例": 0.0,
                "送转股份-转股比例": 0.0,
                "最新公告日期": date(2025, 5, 10),
            },
        ])
        fetcher = AkshareCorporateActionFetcher(client=client)

        events = fetcher.get_stock_corporate_actions(
            "sh.600519",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )

        self.assertEqual([item["action_type"] for item in events], [
            "cash_dividend",
            "split_adjustment",
        ])
        self.assertAlmostEqual(events[0]["cash_dividend_per_share"], 0.3)
        self.assertAlmostEqual(events[1]["split_ratio"], 1.2)
        self.assertEqual(events[0]["symbol"], "600519")
        self.assertEqual(events[0]["source"], "akshare.stock_fhps_detail_em")
        self.assertIn("600519|2024-06-20|cash_dividend", events[0]["source_record_key"])

        fetcher.get_stock_corporate_actions("600519")
        client.stock_fhps_detail_em.assert_called_once_with(symbol="600519")
        client.stock_dividend_cninfo.assert_not_called()

    def test_cninfo_fallback_sums_send_and_conversion_ratios(self) -> None:
        client = MagicMock()
        client.stock_fhps_detail_em.side_effect = RuntimeError("eastmoney unavailable")
        client.stock_dividend_cninfo.return_value = pd.DataFrame([{
            "除权日": date(2024, 7, 1),
            "派息比例": 2.5,
            "送股比例": 1.0,
            "转增比例": 2.0,
            "实施方案公告日期": date(2024, 6, 20),
        }])

        events = AkshareCorporateActionFetcher(
            client=client
        ).get_stock_corporate_actions("000001")

        self.assertAlmostEqual(events[0]["cash_dividend_per_share"], 0.25)
        self.assertAlmostEqual(events[1]["split_ratio"], 1.3)
        self.assertTrue(all(
            item["source"] == "akshare.stock_dividend_cninfo" for item in events
        ))

    def test_invalid_payloads_fail_with_both_source_errors(self) -> None:
        client = MagicMock()
        client.stock_fhps_detail_em.return_value = pd.DataFrame()
        client.stock_dividend_cninfo.return_value = pd.DataFrame()

        with self.assertRaisesRegex(RuntimeError, "EastMoney.*CNInfo"):
            AkshareCorporateActionFetcher(
                client=client
            ).get_stock_corporate_actions("600519")

    def test_bse_routes_to_official_fetcher_without_calling_sse_szse_sources(self) -> None:
        client = MagicMock()
        bse_fetcher = MagicMock()
        bse_fetcher.get_stock_corporate_actions.return_value = [{
            "symbol": "920680",
            "source": "bse.companyAnnouncement.pdf",
        }]

        events = AkshareCorporateActionFetcher(
            client=client,
            bse_fetcher=bse_fetcher,
        ).get_stock_corporate_actions("920680")

        self.assertEqual(events[0]["source"], "bse.companyAnnouncement.pdf")
        bse_fetcher.get_stock_corporate_actions.assert_called_once_with(
            "920680",
            start_date=None,
            end_date=None,
        )
        client.stock_fhps_detail_em.assert_not_called()
        client.stock_dividend_cninfo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
