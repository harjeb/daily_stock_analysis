# -*- coding: utf-8 -*-
"""LLM-driven Wolf post-market analysis service.

Instead of hardcoded rules, this module sends stock technical data to an LLM
with the wolf rulebook as system context and parses structured JSON output.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.config import Config

logger = logging.getLogger(__name__)

# Load wolf rulebook from the strategy YAML (instructions section)
_WOLF_STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategies" / "wolf_postmarket.yaml"

_WOLF_SYSTEM_PROMPT: Optional[str] = None


def _load_wolf_system_prompt() -> str:
    """Load and cache the wolf strategy instructions as system prompt."""
    global _WOLF_SYSTEM_PROMPT
    if _WOLF_SYSTEM_PROMPT is not None:
        return _WOLF_SYSTEM_PROMPT

    try:
        import yaml
        content = _WOLF_STRATEGY_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        instructions = data.get("instructions", "")
    except Exception:
        # Fallback: read raw file and extract instructions block
        try:
            content = _WOLF_STRATEGY_PATH.read_text(encoding="utf-8")
            match = re.search(r"instructions:\s*\|\n(.*)", content, re.DOTALL)
            instructions = match.group(1) if match else content
        except Exception:
            instructions = ""

    _WOLF_SYSTEM_PROMPT = f"""你是一个严格遵循"狼大盘后决策框架"的 A 股盘后分析 agent。

你的唯一职责是：基于提供的日 K 技术数据和大盘环境，对单个股票输出结构化的盘后决策 JSON。

## 核心约束

1. 输出必须是次日交易计划，不是盘后可立即成交的指令。
2. 必须严格按照决策顺序：大盘 → 板块/主线 → 个股位置 → 量价 → 仓位。
3. 不得用题材故事、新闻情绪或主观预期突破硬约束。
4. 数据缺失时必须降级，不得脑补。
5. 输出 JSON 必须严格符合指定格式。

{instructions}

## 输出要求

你必须且只能输出一个 JSON 对象，格式如下（不要输出任何其他文字）：

```json
{{
  "wolf_action": "no_entry|watch|probe|enter|reduce|exit",
  "market_gate": "block|caution|allow|unknown",
  "position_gate": "block|caution|allow|wait_pullback|insufficient_data|unknown",
  "entry_type": "core_mainline|low_flat_base|pullback_support|breakout_confirm|wait_pullback|wait_confirmation|blocked",
  "market_sector_stock_alignment": "aligned|mixed|conflict|unknown",
  "position_cap": "0%|probe|10%|20%|30%",
  "confidence": "high|medium|low",
  "hard_vetoes": ["veto_reason_1"],
  "reasons": ["判断依据1", "判断依据2"],
  "entry_conditions": ["次日允许执行的条件"],
  "invalid_conditions": ["计划失效条件"],
  "next_day_paths": ["路径1: 如果..., 则...", "路径2: 如果..., 则..."],
  "stop_reference_type": "stock_close|sector_index|ma5|ma20|ma60|prior_red_candle|none",
  "data_limitations": ["缺失数据说明"],
  "evidence_rule_ids": ["触发的规则名称"]
}}
```
"""
    return _WOLF_SYSTEM_PROMPT


def _build_stock_data_prompt(
    code: str,
    name: str,
    context: Mapping[str, Any],
    daily_market_context: Optional[Mapping[str, Any]] = None,
    hot_sectors: Optional[List[str]] = None,
) -> str:
    """Build the user prompt containing stock technical data."""
    today = context.get("today") if isinstance(context.get("today"), Mapping) else {}
    trend = context.get("trend_analysis") if isinstance(context.get("trend_analysis"), Mapping) else {}
    realtime = context.get("realtime") if isinstance(context.get("realtime"), Mapping) else {}

    # Format market context
    market_section = ""
    if daily_market_context and isinstance(daily_market_context, Mapping):
        summary = daily_market_context.get("summary", "")
        risk_tags = daily_market_context.get("risk_tags", [])
        market_section = f"""
## 大盘环境
- 大盘摘要：{summary or '无'}
- 风险标签：{', '.join(str(t) for t in risk_tags) if risk_tags else '无'}
"""

    # Format hot sector info with trend data
    sector_section = ""
    sector_trend = context.get("sector_trend") if isinstance(context.get("sector_trend"), Mapping) else {}
    if hot_sectors:
        sector_lines = [f"- 命中近60日强势板块：{', '.join(hot_sectors[:5])}"]
        matched = sector_trend.get("matched_sectors") if isinstance(sector_trend.get("matched_sectors"), list) else []
        for ms in matched[:5]:
            if isinstance(ms, Mapping):
                sector_lines.append(f"  - {ms.get('name', '')}: 60日涨幅 {ms.get('change_60d', 'N/A')}%")
        top_sectors = sector_trend.get("top_sectors_60d") if isinstance(sector_trend.get("top_sectors_60d"), list) else []
        if top_sectors:
            sector_lines.append("- 全市场强势板块前5（60日涨幅）：")
            for ts in top_sectors[:5]:
                if isinstance(ts, Mapping):
                    sector_lines.append(f"  - {ts.get('name', '')}: {ts.get('change_60d', 'N/A')}%")
        daily_top = sector_trend.get("daily_top_sectors") if isinstance(sector_trend.get("daily_top_sectors"), list) else []
        if daily_top:
            sector_lines.append("- 当日领涨板块前5：")
            for ds in daily_top[:5]:
                if isinstance(ds, Mapping):
                    sector_lines.append(f"  - {ds.get('name', '')}: {ds.get('change_pct', 'N/A')}%")
        daily_bottom = sector_trend.get("daily_bottom_sectors") if isinstance(sector_trend.get("daily_bottom_sectors"), list) else []
        if daily_bottom:
            sector_lines.append("- 当日领跌板块前3：")
            for ds in daily_bottom[:3]:
                if isinstance(ds, Mapping):
                    sector_lines.append(f"  - {ds.get('name', '')}: {ds.get('change_pct', 'N/A')}%")
        sector_section = "\n## 板块信息\n" + "\n".join(sector_lines) + "\n"

    # Market breadth
    breadth = context.get("breadth") if isinstance(context.get("breadth"), Mapping) else {}
    breadth_section = ""
    if breadth:
        bl = []
        if breadth.get("up_count") is not None:
            bl.append(f"- 上涨家数：{breadth['up_count']}")
        if breadth.get("down_count") is not None:
            bl.append(f"- 下跌家数：{breadth['down_count']}")
        if breadth.get("limit_up_count") is not None:
            bl.append(f"- 涨停家数：{breadth['limit_up_count']}")
        if breadth.get("limit_down_count") is not None:
            bl.append(f"- 跌停家数：{breadth['limit_down_count']}")
        if breadth.get("total_amount") is not None:
            bl.append(f"- 两市成交额：{breadth['total_amount']}")
        if bl:
            breadth_section = "\n## 市场广度\n" + "\n".join(bl) + "\n"

    # Collect all available metrics
    metrics = {}
    for key in ("open", "high", "low", "close", "volume", "ma5", "ma10", "ma20",
                "ma60", "boll_upper", "boll_mid", "boll_lower", "volume_ratio",
                "turnover_rate", "prior_red_low"):
        val = today.get(key) or trend.get(key) or realtime.get(key)
        if val is not None:
            try:
                metrics[key] = round(float(val), 4)
            except (TypeError, ValueError):
                pass

    # Merge realtime_extra (pe_ratio, pb_ratio, total_mv, circ_mv, amplitude, etc.)
    realtime_extra = context.get("realtime_extra") if isinstance(context.get("realtime_extra"), Mapping) else {}
    for key in ("volume_ratio", "turnover_rate", "pe_ratio", "pb_ratio",
                "total_mv", "circ_mv", "amplitude", "change_60d", "high_52w", "low_52w"):
        if key not in metrics and realtime_extra.get(key) is not None:
            try:
                metrics[key] = round(float(realtime_extra[key]), 4)
            except (TypeError, ValueError):
                pass

    # Add computed fields from trend
    for key in ("bias_ma5", "boll_upper", "boll_mid", "boll_lower"):
        if key not in metrics and trend.get(key) is not None:
            try:
                metrics[key] = round(float(trend[key]), 4)
            except (TypeError, ValueError):
                pass

    # Volume ratio from realtime
    if "volume_ratio" not in metrics and realtime.get("volume_ratio") is not None:
        try:
            metrics["volume_ratio"] = round(float(realtime["volume_ratio"]), 4)
        except (TypeError, ValueError):
            pass

    # Format metrics
    metrics_lines = "\n".join(f"- {k}: {v}" for k, v in metrics.items())

    # Build K-line pattern notes
    kline_notes = ""
    if metrics.get("open") is not None and metrics.get("close") is not None:
        open_p = metrics["open"]
        close_p = metrics["close"]
        high_p = metrics.get("high", max(open_p, close_p))
        low_p = metrics.get("low", min(open_p, close_p))
        is_red = close_p >= open_p
        body = abs(close_p - open_p)
        upper_shadow = high_p - max(open_p, close_p)
        lower_shadow = min(open_p, close_p) - low_p
        range_val = high_p - low_p
        if range_val > 0:
            kline_notes = f"""
## 今日 K 线特征
- 颜色：{'红K' if is_red else '黑K'}
- 实体：{body:.4f}
- 上影线：{upper_shadow:.4f} (占比 {upper_shadow/range_val*100:.1f}%)
- 下影线：{lower_shadow:.4f} (占比 {lower_shadow/range_val*100:.1f}%)
- 振幅：{range_val:.4f}
"""

    # MA alignment
    ma_note = ""
    ma5 = metrics.get("ma5")
    ma10 = metrics.get("ma10")
    ma20 = metrics.get("ma20")
    if ma5 is not None and ma10 is not None and ma20 is not None:
        if ma5 > ma10 > ma20:
            ma_note = "- 均线排列：多头排列（MA5 > MA10 > MA20）"
        elif ma5 < ma10 < ma20:
            ma_note = "- 均线排列：空头排列（MA5 < MA10 < MA20）"
        else:
            ma_note = "- 均线排列：混合/缠绕"

    # Bias
    bias_note = ""
    bias = metrics.get("bias_ma5")
    if bias is None and metrics.get("close") and ma5:
        bias = (metrics["close"] - ma5) / ma5 * 100
    if bias is not None:
        bias_note = f"- MA5 乖离率：{bias:.2f}%"

    # User monitoring capability
    can_monitor = context.get("user_can_monitor_intraday")
    monitor_note = ""
    if can_monitor is not None:
        monitor_note = f"\n## 用户配置\n- 能否盯盘：{'是' if can_monitor else '否'}\n"

    # Planned entry zone (support/resistance levels)
    entry_zone = context.get("planned_entry_zone") if isinstance(context.get("planned_entry_zone"), Mapping) else {}
    entry_section = ""
    if entry_zone and (entry_zone.get("supports") or entry_zone.get("resistances")):
        el = [f"- 当前收盘价：{entry_zone.get('current_close', 'N/A')}"]
        supports = entry_zone.get("supports") or []
        if supports:
            el.append("- 候选支撑位（距收盘价由近到远）：")
            for s in supports[:5]:
                if isinstance(s, Mapping):
                    el.append(f"  - {s.get('level', '')}: {s.get('price', 'N/A')} (距离 {s.get('distance_pct', 'N/A')}%)")
        resistances = entry_zone.get("resistances") or []
        if resistances:
            el.append("- 候选阻力位（距收盘价由近到远）：")
            for r in resistances[:5]:
                if isinstance(r, Mapping):
                    el.append(f"  - {r.get('level', '')}: {r.get('price', 'N/A')} (距离 {r.get('distance_pct', 'N/A')}%)")
        entry_section = "\n## 计划入场区间（候选支撑/阻力位）\n" + "\n".join(el) + "\n"

    # Stock profile (industry, ROE, belong_boards, etc.)
    stock_profile = context.get("stock_profile") if isinstance(context.get("stock_profile"), Mapping) else {}
    profile_section = ""
    if stock_profile:
        pl = []
        if stock_profile.get("industry"):
            pl.append(f"- 所属行业：{stock_profile['industry']}")
        if stock_profile.get("roe") is not None:
            pl.append(f"- ROE：{stock_profile['roe']}")
        if stock_profile.get("net_margin") is not None:
            pl.append(f"- 净利率：{stock_profile['net_margin']}")
        if stock_profile.get("pe_ttm") is not None:
            pl.append(f"- 市盈率(动)：{stock_profile['pe_ttm']}")
        if stock_profile.get("pb") is not None:
            pl.append(f"- 市净率：{stock_profile['pb']}")
        belong_boards = stock_profile.get("belong_boards") or []
        if belong_boards:
            pl.append(f"- 所属板块：{', '.join(belong_boards[:6])}")
        if pl:
            profile_section = "\n## 个股基本面\n" + "\n".join(pl) + "\n"

    prompt = f"""请分析以下股票的盘后决策：

## 股票信息
- 代码：{code}
- 名称：{name or '未知'}
{market_section}
{breadth_section}
{sector_section}
{profile_section}
## 今日技术数据
{metrics_lines}
{ma_note}
{bias_note}
{kline_notes}
{entry_section}
{monitor_note}
请严格按照狼大盘后决策框架，输出结构化 JSON 决策。只输出 JSON，不要输出其他文字。
"""
    return prompt


def _extract_json_from_response(text: str) -> Optional[Dict[str, Any]]:
    """Extract JSON object from LLM response text."""
    if not text:
        return None

    # Try direct parse
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # Try extracting from code block
    match = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


_VALID_WOLF_ACTIONS = {"no_entry", "watch", "probe", "enter", "reduce", "exit"}
_VALID_MARKET_GATES = {"block", "caution", "allow", "unknown"}
_VALID_POSITION_GATES = {"block", "caution", "allow", "wait_pullback", "insufficient_data", "unknown"}


def _normalize_wolf_llm_result(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and validate LLM output to match expected contract."""
    result = {}

    # Enum fields with defaults
    result["wolf_action"] = raw.get("wolf_action", "watch") if raw.get("wolf_action") in _VALID_WOLF_ACTIONS else "watch"
    result["market_gate"] = raw.get("market_gate", "unknown") if raw.get("market_gate") in _VALID_MARKET_GATES else "unknown"
    result["position_gate"] = raw.get("position_gate", "unknown") if raw.get("position_gate") in _VALID_POSITION_GATES else "unknown"
    result["entry_type"] = str(raw.get("entry_type") or "wait_confirmation")
    result["market_sector_stock_alignment"] = str(raw.get("market_sector_stock_alignment") or "unknown")
    result["position_cap"] = str(raw.get("position_cap") or "0%")
    result["confidence"] = str(raw.get("confidence") or "low")
    result["stop_reference_type"] = str(raw.get("stop_reference_type") or "none")

    # List fields
    for list_key in ("hard_vetoes", "reasons", "entry_conditions", "invalid_conditions",
                     "next_day_paths", "data_limitations", "evidence_rule_ids"):
        val = raw.get(list_key)
        if isinstance(val, list):
            result[list_key] = [str(item) for item in val if item]
        else:
            result[list_key] = []

    return result


class WolfLLMAnalysisService:
    """LLM-driven wolf post-market analysis for individual stocks."""

    def __init__(self, config: Config, *, analyzer=None):
        """
        Args:
            config: Application config.
            analyzer: GeminiAnalyzer instance (or any object with generate_text method).
        """
        self.config = config
        self._analyzer = analyzer

    def _get_analyzer(self):
        """Lazy-load analyzer if not provided."""
        if self._analyzer is not None:
            return self._analyzer
        try:
            from src.analyzer import GeminiAnalyzer
            self._analyzer = GeminiAnalyzer()
            return self._analyzer
        except Exception as exc:
            logger.error("Wolf LLM: failed to create analyzer: %s", exc)
            return None

    def analyze_stock(
        self,
        code: str,
        name: str,
        context: Mapping[str, Any],
        *,
        daily_market_context: Optional[Mapping[str, Any]] = None,
        hot_sectors: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Analyze a single stock using LLM with the wolf framework.

        Returns:
            Dict matching the wolf output contract. Falls back to a safe
            default if the LLM call fails.
        """
        analyzer = self._get_analyzer()
        if analyzer is None or not analyzer.is_available():
            logger.warning("Wolf LLM: analyzer not available for %s, returning fallback", code)
            return self._fallback_result(code, reason="llm_unavailable")

        system_prompt = _load_wolf_system_prompt()
        user_prompt = _build_stock_data_prompt(
            code, name, context,
            daily_market_context=daily_market_context,
            hot_sectors=hot_sectors,
        )

        max_tokens = int(getattr(self.config, "wolf_llm_max_tokens", 2048) or 2048)
        temperature = float(getattr(self.config, "wolf_llm_temperature", 0.3) or 0.3)

        logger.info("Wolf LLM: analyzing %s (%s)", code, name)
        start_time = time.monotonic()

        try:
            # Use _call_litellm directly to pass system_prompt
            result_tuple = analyzer._call_litellm(
                user_prompt,
                generation_config={"max_tokens": max_tokens, "temperature": temperature},
                system_prompt=system_prompt,
            )
            if isinstance(result_tuple, tuple):
                response_text, model_used, usage = result_tuple
            else:
                response_text = result_tuple
                model_used = None

            elapsed = time.monotonic() - start_time
            logger.info(
                "Wolf LLM: %s responded in %.1fs, length=%d, model=%s",
                code, elapsed, len(response_text or ""), model_used,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start_time
            logger.warning("Wolf LLM: %s call failed after %.1fs: %s", code, elapsed, exc)
            return self._fallback_result(code, reason=f"llm_error: {type(exc).__name__}")

        # Parse response
        parsed = _extract_json_from_response(response_text)
        if parsed is None:
            logger.warning("Wolf LLM: %s response not valid JSON, using fallback", code)
            return self._fallback_result(code, reason="json_parse_failed")

        result = _normalize_wolf_llm_result(parsed)
        result["_llm_model"] = model_used
        result["_llm_elapsed_ms"] = int(elapsed * 1000)
        return result

    @staticmethod
    def _fallback_result(code: str, *, reason: str = "unknown") -> Dict[str, Any]:
        """Return a safe fallback when LLM analysis is unavailable."""
        return {
            "wolf_action": "watch",
            "market_gate": "unknown",
            "position_gate": "unknown",
            "entry_type": "wait_confirmation",
            "market_sector_stock_alignment": "unknown",
            "position_cap": "0%",
            "confidence": "low",
            "hard_vetoes": [],
            "reasons": [f"LLM 分析不可用: {reason}"],
            "entry_conditions": [],
            "invalid_conditions": [],
            "next_day_paths": [],
            "stop_reference_type": "none",
            "data_limitations": [f"llm_fallback: {reason}"],
            "evidence_rule_ids": [],
        }
