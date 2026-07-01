# -*- coding: utf-8 -*-
"""Tests for the daily-K Wolf report service."""

import pandas as pd

from src.config import Config
from src.services.wolf_daily_report_service import (
    WolfDailyPick,
    WolfDailyReportResult,
    WolfDailyReportService,
    format_wolf_daily_report_markdown,
    load_wolf_codes,
    normalize_wolf_code_list,
)


def test_normalize_wolf_code_list_only_keeps_a_share_codes():
    codes, skipped = normalize_wolf_code_list(["SH600519", "300750.SZ", "hk00700", "AAPL"])

    assert codes == ["600519", "300750"]
    assert "hk00700" in skipped
    assert "AAPL" in skipped


def test_load_wolf_codes_supports_inline_content():
    codes, warnings = load_wolf_codes(
        file_path="",
        inline_content="600519, SZ300750\nAAPL",
    )

    assert codes == ["600519", "300750"]
    assert warnings


def test_build_policy_context_adds_daily_k_indicators():
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=30, freq="D"),
            "open": [10 + i * 0.1 for i in range(30)],
            "high": [10.2 + i * 0.1 for i in range(30)],
            "low": [9.8 + i * 0.1 for i in range(30)],
            "close": [10.1 + i * 0.1 for i in range(30)],
            "volume": [100000 + i * 1000 for i in range(30)],
        }
    )
    service = WolfDailyReportService(Config(wolf_daily_report_enabled=True))

    context = service._build_policy_context(df)

    assert context["today"]["ma5"] > context["today"]["ma10"]
    assert "boll_upper" in context["today"]
    assert context["realtime"]["volume_ratio"] is not None
    assert context["today"]["prior_red_low"] > 0


def test_format_wolf_daily_report_markdown_groups_scopes():
    result = WolfDailyReportResult(
        enabled=True,
        whitelist_count=1,
        stock_list_count=1,
        picks=[
            WolfDailyPick(
                code="600519",
                scope="whitelist",
                name="贵州茅台",
                policy={
                    "wolf_action": "probe",
                    "position_cap": "10%",
                    "confidence": "medium",
                    "reasons": ["位置接近 MA5"],
                    "metrics": {"close": 10.0, "ma5": 9.9},
                },
            ),
            WolfDailyPick(
                code="300750",
                scope="stock_list",
                name="宁德时代",
                policy={
                    "wolf_action": "watch",
                    "position_cap": "0%",
                    "confidence": "medium",
                    "reasons": ["等待确认"],
                },
            ),
        ],
    )

    markdown = format_wolf_daily_report_markdown(result)

    assert "狼哥日K盘后分析" in markdown
    assert "白名单观察 / 入场候选" in markdown
    assert "STOCK_LIST 操作分析" in markdown
    assert "贵州茅台(600519)" in markdown
