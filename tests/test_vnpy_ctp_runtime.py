# -*- coding: utf-8 -*-
"""Optional structural checks against the installed official CTP Gateway."""

from __future__ import annotations

import pytest

from src.services.vnpy_runtime import _gateway_connection_confirmation


def test_official_ctp_gateway_exposes_confirmable_channel_login_state() -> None:
    pytest.importorskip("vnpy_ctp")
    from vnpy.event import EventEngine
    from vnpy_ctp import CtpGateway

    gateway = CtpGateway(EventEngine(), "CTP")

    assert _gateway_connection_confirmation(gateway) == (
        False,
        "gateway.channel_login_status",
    )

    gateway.td_api.login_status = True
    assert _gateway_connection_confirmation(gateway) == (
        False,
        "gateway.channel_login_status",
    )

    gateway.md_api.login_status = True
    assert _gateway_connection_confirmation(gateway) == (
        True,
        "gateway.channel_login_status",
    )
