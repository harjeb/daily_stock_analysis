from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.config import Config
from src.services.alphasift_daily_pool_service import (
    AlphaSiftDailyPoolService,
    AlphaSiftDailyPoolResult,
    format_daily_pool_markdown,
    load_pool_codes,
    normalize_pool_code,
)
import src.services.alphasift_daily_pool_service as daily_pool_service


def test_load_pool_codes_accepts_common_a_share_formats(tmp_path):
    pool = tmp_path / "watch_pool.csv"
    pool.write_text("code,name\n600519,贵州茅台\nSZ300750,宁德时代\n000001.SZ,平安银行\n", encoding="utf-8")

    assert load_pool_codes(str(pool)) == ["600519", "300750", "000001"]
    assert normalize_pool_code("SH600519") == "600519"
    assert normalize_pool_code("600519.SH") == "600519"


def test_daily_pool_runs_multiple_strategies_and_dedupes(tmp_path, monkeypatch):
    pool = tmp_path / "watch_pool.csv"
    pool.write_text("600519,300750,000001", encoding="utf-8")
    config = Config(
        alphasift_enabled=True,
        alphasift_daily_notify=True,
        alphasift_daily_pool_file=str(pool),
        alphasift_daily_strategies=["shrink_pullback", "balanced_alpha"],
        alphasift_daily_top_n=2,
        alphasift_daily_use_llm=False,
    )
    service = AlphaSiftDailyPoolService(config)

    def fake_screen_strategy(*, strategy, codes, top_n, use_llm):
        assert codes == ["600519", "300750", "000001"]
        assert top_n == 2
        assert use_llm is False
        if strategy == "shrink_pullback":
            return {
                "strategy": strategy,
                "candidates": [
                    {"code": "600519", "name": "贵州茅台", "score": 80.0},
                    {"code": "300750", "name": "宁德时代", "score": 70.0},
                ],
            }
        return {
            "strategy": strategy,
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 85.0},
                {"code": "000001", "name": "平安银行", "score": 60.0},
            ],
        }

    monkeypatch.setattr(service, "_screen_strategy", fake_screen_strategy)

    fake_alphasift_service = SimpleNamespace(
        _ensure_alphasift_available_for_use=lambda: None,
        _normalize_candidates=lambda raw: raw.get("candidates", []),
        _list_text_values=lambda value: list(value or []),
    )
    with patch.object(daily_pool_service, "_alphasift_service", return_value=fake_alphasift_service):
        result = service.run()

    assert result.enabled is True
    assert result.pool_count == 3
    assert [pick.code for pick in result.picks] == ["600519", "300750", "000001"]
    assert result.picks[0].strategies == ["shrink_pullback", "balanced_alpha"]
    assert result.picks[0].score == 85.0


def test_format_daily_pool_markdown_includes_strategy_hits():
    result = AlphaSiftDailyPoolResult(
        enabled=True,
        pool_file="data/pools/watch_pool.csv",
        pool_count=100,
        top_n=5,
        strategies=["shrink_pullback", "balanced_alpha"],
        picks=[
            SimpleNamespace(
                code="600519",
                name="贵州茅台",
                score=82.5,
                screen_score=80.0,
                strategies=["shrink_pullback", "balanced_alpha"],
                strategy="shrink_pullback",
                reason="趋势回踩确认",
                risk_flags=[],
                risk_level="",
                llm_thesis="",
                llm_catalysts=[],
                llm_risks=[],
                price=1688.0,
                change_pct=1.2,
                industry="白酒",
            )
        ],
    )

    markdown = format_daily_pool_markdown(result)

    assert "AlphaSift 股票池入场候选" in markdown
    assert "贵州茅台(600519)" in markdown
    assert "shrink_pullback, balanced_alpha" in markdown
