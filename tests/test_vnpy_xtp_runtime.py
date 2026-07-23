# -*- coding: utf-8 -*-
"""Structural checks against the installed A-share XTP Gateway."""

from __future__ import annotations

import pytest

from src.services.vnpy_adapter import _gateway_connection_confirmation as adapter_status
from src.services.vnpy_runtime import _gateway_connection_confirmation as runtime_status


def test_official_xtp_gateway_exposes_a_share_and_channel_contracts() -> None:
    pytest.importorskip("vnpy_xtp")
    from vnpy.event import EventEngine
    from vnpy.trader.constant import Exchange
    from vnpy_xtp import XtpGateway

    gateway = XtpGateway(EventEngine(), "XTP")

    assert set(gateway.exchanges) == {Exchange.SSE, Exchange.SZSE}
    assert set(gateway.default_setting) == {
        "账号",
        "密码",
        "客户号",
        "行情地址",
        "行情端口",
        "交易地址",
        "交易端口",
        "行情协议",
        "日志级别",
        "授权码",
    }
    assert runtime_status(gateway) == (False, "gateway.channel_login_status")
    assert adapter_status(gateway) == (False, "gateway.channel_login_status")

    gateway.td_api.login_status = True
    assert runtime_status(gateway) == (False, "gateway.channel_login_status")
    assert adapter_status(gateway) == (False, "gateway.channel_login_status")

    gateway.md_api.login_status = True
    assert runtime_status(gateway) == (True, "gateway.channel_login_status")
    assert adapter_status(gateway) == (True, "gateway.channel_login_status")
