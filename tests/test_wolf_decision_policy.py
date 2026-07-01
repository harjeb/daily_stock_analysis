# -*- coding: utf-8 -*-
"""Tests for wolf-style post-market decision guardrails."""

from src.services.wolf_decision_policy import (
    evaluate_wolf_postmarket_policy,
    format_wolf_policy_prompt_section,
)


def _base_context():
    return {
        "today": {
            "close": 10.0,
            "ma5": 9.9,
            "ma10": 9.5,
            "ma20": 9.0,
        },
        "realtime": {"volume_ratio": 1.2},
        "daily_market_context": {
            "summary": "市场环境正常，结构未触发高风险。",
            "risk_tags": [],
            "source": "test",
        },
        "market_phase_context": {"phase": "non_trading"},
    }


def test_high_ma5_bias_blocks_entry():
    context = _base_context()
    context["trend_analysis"] = {"bias_ma5": 6.2}

    result = evaluate_wolf_postmarket_policy(context)

    assert result["wolf_action"] == "no_entry"
    assert result["position_cap"] == "0%"
    assert "high_bias_no_chase" in result["hard_vetoes"]


def test_close_below_ma20_blocks_entry():
    context = _base_context()
    context["today"]["close"] = 8.8

    result = evaluate_wolf_postmarket_policy(context)

    assert result["wolf_action"] == "no_entry"
    assert result["position_gate"] == "block"
    assert "below_ma20" in result["hard_vetoes"]


def test_bullish_near_ma5_allows_entry_plan():
    context = _base_context()

    result = evaluate_wolf_postmarket_policy(context)

    assert result["wolf_action"] == "enter"
    assert result["position_gate"] == "allow"
    assert result["entry_type"] == "pullback_support"
    assert result["position_cap"] == "20%"
    assert not result["hard_vetoes"]


def test_missing_price_data_downgrades_to_watch():
    context = {
        "daily_market_context": {
            "summary": "市场环境正常。",
            "risk_tags": [],
        }
    }

    result = evaluate_wolf_postmarket_policy(context)

    assert result["wolf_action"] == "watch"
    assert result["position_gate"] == "insufficient_data"
    assert result["confidence"] == "low"
    assert "missing_close" in result["data_limitations"]


def test_market_risk_blocks_even_with_good_position():
    context = _base_context()
    context["daily_market_context"] = {
        "summary": "市场退潮，高风险。",
        "risk_tags": ["market_cooling"],
    }

    result = evaluate_wolf_postmarket_policy(context)

    assert result["wolf_action"] == "no_entry"
    assert result["market_gate"] == "block"
    assert "market_gate_block" in result["hard_vetoes"]


def test_prompt_section_contains_hard_constraints():
    result = evaluate_wolf_postmarket_policy(_base_context())

    section = format_wolf_policy_prompt_section(result)

    assert "狼大盘后决策护栏" in section
    assert "LLM 不得" in section
    assert "wolf_action" in section


def test_black_k_break_ma5_blocks_entry():
    context = _base_context()
    context["today"].update({"open": 10.4, "close": 9.7})
    context["realtime"]["volume_ratio"] = 1.6

    result = evaluate_wolf_postmarket_policy(context)

    assert result["wolf_action"] == "no_entry"
    assert "black_k_break_ma5_reduce" in result["hard_vetoes"]


def test_high_volume_prior_red_break_blocks_entry():
    context = _base_context()
    context["today"].update({"close": 9.8, "prior_red_low": 9.9})
    context["realtime"]["volume_ratio"] = 2.0

    result = evaluate_wolf_postmarket_policy(context)

    assert result["wolf_action"] == "no_entry"
    assert "high_volume_prior_red_break" in result["hard_vetoes"]
