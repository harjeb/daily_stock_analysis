# -*- coding: utf-8 -*-
"""Tests for the LLM-driven Wolf analysis service."""

import json
from unittest.mock import MagicMock, patch

from src.config import Config
from src.services.wolf_llm_analysis_service import (
    WolfLLMAnalysisService,
    _extract_json_from_response,
    _normalize_wolf_llm_result,
    _build_stock_data_prompt,
)


def test_extract_json_direct():
    text = json.dumps({"wolf_action": "watch", "position_cap": "0%"})
    result = _extract_json_from_response(text)
    assert result is not None
    assert result["wolf_action"] == "watch"


def test_extract_json_from_code_block():
    text = '```json\n{"wolf_action": "enter", "position_cap": "20%"}\n```'
    result = _extract_json_from_response(text)
    assert result is not None
    assert result["wolf_action"] == "enter"


def test_extract_json_with_surrounding_text():
    text = 'Here is the analysis:\n{"wolf_action": "probe"}\nDone.'
    result = _extract_json_from_response(text)
    assert result is not None
    assert result["wolf_action"] == "probe"


def test_extract_json_empty():
    assert _extract_json_from_response("") is None
    assert _extract_json_from_response("no json here") is None


def test_normalize_validates_enums():
    raw = {
        "wolf_action": "invalid_action",
        "market_gate": "invalid_gate",
        "position_gate": "invalid_gate",
        "entry_type": "pullback_support",
        "position_cap": "10%",
        "confidence": "medium",
        "reasons": ["reason 1", "reason 2"],
        "entry_conditions": ["cond 1"],
        "next_day_paths": ["path 1"],
        "stop_reference_type": "ma5",
    }
    result = _normalize_wolf_llm_result(raw)
    assert result["wolf_action"] == "watch"
    assert result["market_gate"] == "unknown"
    assert result["position_gate"] == "unknown"
    assert result["entry_type"] == "pullback_support"
    assert result["position_cap"] == "10%"
    assert result["reasons"] == ["reason 1", "reason 2"]
    assert result["entry_conditions"] == ["cond 1"]
    assert result["next_day_paths"] == ["path 1"]
    assert result["stop_reference_type"] == "ma5"


def test_normalize_handles_missing_fields():
    result = _normalize_wolf_llm_result({})
    assert result["wolf_action"] == "watch"
    assert result["position_cap"] == "0%"
    assert result["confidence"] == "low"
    assert result["hard_vetoes"] == []
    assert result["reasons"] == []


def test_build_prompt_includes_stock_info():
    context = {
        "today": {
            "open": 10.0,
            "close": 10.5,
            "high": 10.8,
            "low": 9.9,
            "volume": 100000,
            "ma5": 10.2,
            "ma10": 10.0,
            "ma20": 9.8,
        },
    }
    prompt = _build_stock_data_prompt(
        "600519", "贵州茅台", context,
        daily_market_context={"summary": "大盘正常", "risk_tags": []},
        hot_sectors=["机器人"],
    )
    assert "600519" in prompt
    assert "贵州茅台" in prompt
    assert "大盘正常" in prompt
    assert "机器人" in prompt
    assert "多头排列" in prompt


def test_build_prompt_renders_breadth_and_sector_trend():
    context = {
        "today": {"close": 10.0, "ma5": 9.9, "ma10": 9.8, "ma20": 9.5},
        "breadth": {
            "up_count": 2800,
            "down_count": 2100,
            "limit_up_count": 45,
            "limit_down_count": 3,
            "total_amount": 85000000000,
        },
        "sector_trend": {
            "matched_sectors": [{"name": "机器人", "change_60d": 25.3}],
            "top_sectors_60d": [
                {"name": "AI芯片", "change_60d": 30.1},
                {"name": "机器人", "change_60d": 25.3},
            ],
        },
        "realtime_extra": {
            "pe_ratio": 35.2,
            "turnover_rate": 1.8,
            "amplitude": 3.5,
        },
        "user_can_monitor_intraday": True,
    }
    prompt = _build_stock_data_prompt(
        "300750", "宁德时代", context,
        daily_market_context={"summary": "震荡", "risk_tags": ["量能不足"]},
        hot_sectors=["机器人"],
    )
    assert "上涨家数：2800" in prompt
    assert "下跌家数：2100" in prompt
    assert "涨停家数：45" in prompt
    assert "60日涨幅 25.3%" in prompt
    assert "AI芯片" in prompt
    assert "pe_ratio" in prompt
    assert "turnover_rate" in prompt
    assert "能否盯盘：是" in prompt
    assert "量能不足" in prompt


def test_build_prompt_handles_missing_supplementary_data():
    context = {
        "today": {"close": 10.0},
    }
    prompt = _build_stock_data_prompt(
        "600519", "贵州茅台", context,
        daily_market_context=None,
        hot_sectors=None,
    )
    assert "600519" in prompt
    assert "市场广度" not in prompt
    assert "用户配置" not in prompt


def test_build_prompt_renders_entry_zone_and_stock_profile():
    context = {
        "today": {"close": 10.0, "ma5": 9.8, "ma10": 9.5, "ma20": 9.0},
        "planned_entry_zone": {
            "current_close": 10.0,
            "supports": [
                {"level": "MA5", "price": 9.8, "distance_pct": 2.04},
                {"level": "MA20", "price": 9.0, "distance_pct": 11.11},
            ],
            "resistances": [
                {"level": "近20日高点", "price": 10.5, "distance_pct": -4.76},
            ],
        },
        "stock_profile": {
            "industry": "白酒",
            "roe": 30.5,
            "pe_ttm": 35.2,
            "belong_boards": ["白酒概念", "消费升级", "MSCI中国"],
        },
        "sector_trend": {
            "daily_top_sectors": [
                {"name": "半导体", "change_pct": 3.2},
            ],
            "daily_bottom_sectors": [
                {"name": "房地产", "change_pct": -2.1},
            ],
        },
    }
    prompt = _build_stock_data_prompt(
        "600519", "贵州茅台", context,
        daily_market_context={"summary": "正常", "risk_tags": []},
        hot_sectors=["白酒概念"],
    )
    assert "计划入场区间" in prompt
    assert "MA5" in prompt
    assert "9.8" in prompt
    assert "候选阻力位" in prompt
    assert "近20日高点" in prompt
    assert "个股基本面" in prompt
    assert "白酒" in prompt
    assert "ROE" in prompt
    assert "30.5" in prompt
    assert "MSCI中国" in prompt
    assert "当日领涨板块" in prompt
    assert "半导体" in prompt
    assert "当日领跌板块" in prompt
    assert "房地产" in prompt


def test_build_prompt_handles_missing_entry_zone_and_profile():
    context = {
        "today": {"close": 10.0},
    }
    prompt = _build_stock_data_prompt(
        "600519", "贵州茅台", context,
        daily_market_context=None,
        hot_sectors=None,
    )
    assert "计划入场区间" not in prompt
    assert "个股基本面" not in prompt
    assert "当日领涨板块" not in prompt


def test_analyze_stock_fallback_when_no_analyzer():
    config = Config(wolf_daily_report_enabled=True, wolf_daily_use_llm=True)
    service = WolfLLMAnalysisService(config, analyzer=None)
    # _get_analyzer will fail, should return fallback
    with patch("src.services.wolf_llm_analysis_service.WolfLLMAnalysisService._get_analyzer", return_value=None):
        result = service.analyze_stock("600519", "贵州茅台", {"today": {}})
    assert result["wolf_action"] == "watch"
    assert result["position_cap"] == "0%"
    assert "llm_unavailable" in result["data_limitations"][0]


def test_analyze_stock_success_with_mock():
    mock_analyzer = MagicMock()
    mock_analyzer.is_available.return_value = True
    llm_response = json.dumps({
        "wolf_action": "enter",
        "market_gate": "allow",
        "position_gate": "allow",
        "entry_type": "pullback_support",
        "market_sector_stock_alignment": "aligned",
        "position_cap": "20%",
        "confidence": "medium",
        "reasons": ["多头排列且贴近MA5"],
        "entry_conditions": ["次日回踩MA5不破"],
        "invalid_conditions": ["跌破MA20"],
        "next_day_paths": ["如果回踩MA5缩量企稳, 则试探入场"],
        "stop_reference_type": "ma5",
        "evidence_rule_ids": ["no_chase_high", "position_trading"],
    })
    mock_analyzer._call_litellm.return_value = (llm_response, "test-model", {})

    config = Config(wolf_daily_report_enabled=True, wolf_daily_use_llm=True)
    service = WolfLLMAnalysisService(config, analyzer=mock_analyzer)

    result = service.analyze_stock(
        "600519", "贵州茅台",
        {"today": {"close": 10.0, "ma5": 9.9, "ma10": 9.8, "ma20": 9.5}},
    )
    assert result["wolf_action"] == "enter"
    assert result["position_cap"] == "20%"
    assert result["confidence"] == "medium"
    assert result["market_gate"] == "allow"
    assert "多头排列且贴近MA5" in result["reasons"]
    assert result["_llm_model"] == "test-model"


def test_analyze_stock_fallback_on_json_parse_failure():
    mock_analyzer = MagicMock()
    mock_analyzer.is_available.return_value = True
    mock_analyzer._call_litellm.return_value = ("not json at all", "test-model", {})

    config = Config(wolf_daily_report_enabled=True, wolf_daily_use_llm=True)
    service = WolfLLMAnalysisService(config, analyzer=mock_analyzer)

    result = service.analyze_stock("600519", "贵州茅台", {"today": {}})
    assert result["wolf_action"] == "watch"
    assert "json_parse_failed" in result["data_limitations"][0]
