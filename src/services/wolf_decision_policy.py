# -*- coding: utf-8 -*-
"""Deterministic guardrails for wolf-style post-market entry decisions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from src.report_language import normalize_report_language


_BLOCKING_MARKET_TAGS = {"high_risk", "market_cooling"}
_CAUTION_MARKET_TAGS = {"conservative", "low_position_cap"}
_BLOCKING_SUMMARY_TERMS = ("高风险", "退潮", "系统性风险", "risk-off", "high risk")
_NEGATED_BLOCKING_SUMMARY_TERMS = (
    "未触发高风险",
    "无高风险",
    "没有高风险",
    "未见高风险",
    "not high risk",
    "no high risk",
)
_CAUTION_SUMMARY_TERMS = ("谨慎", "观望", "等待确认", "震荡", "cautious", "watch")


@dataclass(frozen=True)
class WolfDecisionPolicyResult:
    """Machine-readable post-market decision guardrail result."""

    wolf_action: str
    market_gate: str
    position_gate: str
    entry_type: str
    position_cap: str
    confidence: str
    hard_vetoes: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    entry_conditions: List[str] = field(default_factory=list)
    invalid_conditions: List[str] = field(default_factory=list)
    data_limitations: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wolf_action": self.wolf_action,
            "market_gate": self.market_gate,
            "position_gate": self.position_gate,
            "entry_type": self.entry_type,
            "position_cap": self.position_cap,
            "confidence": self.confidence,
            "hard_vetoes": list(self.hard_vetoes),
            "reasons": list(self.reasons),
            "entry_conditions": list(self.entry_conditions),
            "invalid_conditions": list(self.invalid_conditions),
            "data_limitations": list(self.data_limitations),
            "metrics": dict(self.metrics),
        }


def evaluate_wolf_postmarket_policy(context: Mapping[str, Any]) -> Dict[str, Any]:
    """Evaluate hard post-market entry guardrails from analysis context."""

    result = WolfDecisionPolicy.evaluate(context)
    return result.to_dict()


def format_wolf_policy_prompt_section(
    policy: Mapping[str, Any],
    *,
    report_language: str = "zh",
) -> str:
    """Render policy result as prompt constraints for the LLM."""

    if not isinstance(policy, Mapping) or not policy:
        return ""

    language = normalize_report_language(report_language)
    metrics = policy.get("metrics") if isinstance(policy.get("metrics"), Mapping) else {}

    if language == "en":
        lines = [
            "\n## Wolf Post-Market Decision Guardrails",
            "Treat this section as deterministic hard constraints. Do not override hard vetoes with narrative reasons.",
            f"- Action: {policy.get('wolf_action', 'watch')}",
            f"- Market gate: {policy.get('market_gate', 'unknown')}",
            f"- Position gate: {policy.get('position_gate', 'unknown')}",
            f"- Entry type: {policy.get('entry_type', 'wait')}",
            f"- Position cap: {policy.get('position_cap', '0%')}",
            f"- Confidence: {policy.get('confidence', 'low')}",
        ]
        if metrics:
            lines.append(f"- Metrics: {_format_metrics(metrics)}")
        _append_list(lines, "Hard vetoes", policy.get("hard_vetoes"))
        _append_list(lines, "Reasons", policy.get("reasons"))
        _append_list(lines, "Entry conditions", policy.get("entry_conditions"))
        _append_list(lines, "Invalid conditions", policy.get("invalid_conditions"))
        _append_list(lines, "Data limitations", policy.get("data_limitations"))
        lines.append("- Output a next-session plan; do not present post-market analysis as an already executable intraday trade.")
        return "\n".join(lines) + "\n"

    lines = [
        "\n## 狼大盘后决策护栏",
        "以下为确定性硬约束，LLM 不得用题材叙事、主观预期或新闻情绪突破 hard_vetoes。",
        f"- wolf_action：{policy.get('wolf_action', 'watch')}",
        f"- 大盘门禁：{policy.get('market_gate', 'unknown')}",
        f"- 位置门禁：{policy.get('position_gate', 'unknown')}",
        f"- 入场类型：{policy.get('entry_type', 'wait')}",
        f"- 仓位上限：{policy.get('position_cap', '0%')}",
        f"- 置信度：{policy.get('confidence', 'low')}",
    ]
    if metrics:
        lines.append(f"- 关键指标：{_format_metrics(metrics)}")
    _append_list(lines, "硬否决", policy.get("hard_vetoes"))
    _append_list(lines, "判断依据", policy.get("reasons"))
    _append_list(lines, "允许入场条件", policy.get("entry_conditions"))
    _append_list(lines, "失效条件", policy.get("invalid_conditions"))
    _append_list(lines, "数据限制", policy.get("data_limitations"))
    lines.append("- 输出时必须把结论写成次日交易计划，不得把盘后判断伪装成可立即成交的盘中指令。")
    return "\n".join(lines) + "\n"


class WolfDecisionPolicy:
    """Wolf-style deterministic post-market entry policy."""

    @classmethod
    def evaluate(cls, context: Mapping[str, Any]) -> WolfDecisionPolicyResult:
        context = context if isinstance(context, Mapping) else {}
        today = context.get("today") if isinstance(context.get("today"), Mapping) else {}
        trend = (
            context.get("trend_analysis")
            if isinstance(context.get("trend_analysis"), Mapping)
            else {}
        )
        realtime = context.get("realtime") if isinstance(context.get("realtime"), Mapping) else {}

        open_price = _first_float(today.get("open"), realtime.get("open"))
        close = _first_float(today.get("close"), realtime.get("price"))
        ma5 = _first_float(today.get("ma5"), trend.get("ma5"))
        ma10 = _first_float(today.get("ma10"), trend.get("ma10"))
        ma20 = _first_float(today.get("ma20"), trend.get("ma20"))
        boll_upper = _first_float(today.get("boll_upper"), trend.get("boll_upper"))
        boll_mid = _first_float(today.get("boll_mid"), today.get("boll_middle"), trend.get("boll_mid"))
        boll_lower = _first_float(today.get("boll_lower"), trend.get("boll_lower"))
        prior_red_low = _first_float(today.get("prior_red_low"), context.get("prior_red_low"))
        bias_ma5 = _first_float(trend.get("bias_ma5"), _calculate_bias(close, ma5))
        volume_ratio = _first_float(
            realtime.get("volume_ratio"),
            today.get("volume_ratio"),
            context.get("volume_change_ratio"),
            trend.get("volume_ratio_5d"),
        )

        data_limitations: List[str] = []
        if close is None:
            data_limitations.append("missing_close")
        for label, value in (("ma5", ma5), ("ma10", ma10), ("ma20", ma20)):
            if value is None:
                data_limitations.append(f"missing_{label}")

        market_gate, market_reasons = _evaluate_market_gate(context.get("daily_market_context"))
        phase_note = _phase_note(context.get("market_phase_context"))
        if phase_note:
            data_limitations.append(phase_note)

        hard_vetoes: List[str] = []
        reasons: List[str] = list(market_reasons)
        entry_conditions: List[str] = []
        invalid_conditions = [
            "跌破 MA20 或结构低点后不再按入场计划执行",
            "次日高开高走且 MA5 乖离扩大到 5% 以上时放弃追买",
            "放量滞涨、冲高回落或重大利空出现时转为观望",
        ]

        if data_limitations:
            reasons.append("关键价格或均线数据不足，盘后计划只能降级为观察")

        bullish_alignment = _ordered_desc(ma5, ma10, ma20)
        near_ma5 = bias_ma5 is not None and abs(bias_ma5) <= 2.0
        stretched = bias_ma5 is not None and bias_ma5 > 5.0
        mildly_high = bias_ma5 is not None and 2.0 < bias_ma5 <= 5.0
        below_ma20 = close is not None and ma20 is not None and close < ma20
        below_ma10 = close is not None and ma10 is not None and close < ma10
        black_k = open_price is not None and close is not None and close < open_price
        boll_overstretch = close is not None and boll_upper is not None and close > boll_upper
        below_boll_mid = close is not None and boll_mid is not None and close < boll_mid
        prior_red_break = (
            close is not None
            and prior_red_low is not None
            and close < prior_red_low
            and volume_ratio is not None
            and volume_ratio >= 1.8
        )
        black_k_break_ma5 = (
            black_k
            and close is not None
            and ma5 is not None
            and close < ma5
            and volume_ratio is not None
            and volume_ratio >= 1.2
        )

        position_gate = "unknown"
        wolf_action = "watch"
        entry_type = "wait"
        position_cap = "0%"
        confidence = "low"

        if market_gate == "block":
            hard_vetoes.append("market_gate_block")
            reasons.append("大盘环境处于阻断区，单股不做主动入场")

        if stretched:
            hard_vetoes.append("high_bias_no_chase")
            reasons.append("价格相对 MA5 乖离超过 5%，符合不追高硬约束")

        if boll_overstretch:
            hard_vetoes.append("boll_overstretch_no_chase")
            reasons.append("收盘价偏离 BOLL 上轨，按偏离过大处理，先等回轨")

        if prior_red_break:
            hard_vetoes.append("high_volume_prior_red_break")
            reasons.append("放量跌破前一根红 K 低点，上涨段失效风险较高")

        if black_k_break_ma5:
            hard_vetoes.append("black_k_break_ma5_reduce")
            reasons.append("黑 K 放量跌破 MA5，按短期见顶/减仓信号处理")

        if below_ma20:
            hard_vetoes.append("below_ma20")
            reasons.append("收盘价跌破 MA20，中期结构未站稳")

        if data_limitations:
            position_gate = "insufficient_data"
        elif below_ma20:
            position_gate = "block"
        elif below_ma10:
            position_gate = "caution"
            reasons.append("收盘价低于 MA10，短线结构需要重新站回")
        elif bullish_alignment and near_ma5:
            position_gate = "allow"
            reasons.append("MA5>MA10>MA20 且价格贴近 MA5，位置满足试探条件")
        elif bullish_alignment and mildly_high:
            position_gate = "wait_pullback"
            reasons.append("多头排列仍在，但价格离 MA5 偏高，优先等回踩")
        elif bullish_alignment:
            position_gate = "caution"
            reasons.append("均线多头但入场位置未落在低吸窗口")
        else:
            position_gate = "caution"
            reasons.append("均线未形成清晰多头排列，不满足主动入场前提")

        if below_boll_mid and position_gate == "allow":
            position_gate = "caution"
            reasons.append("收盘未站上 BOLL 中轨，右侧确认不足")
        elif close is not None and boll_mid is not None and close >= boll_mid:
            reasons.append("收盘位于 BOLL 中轨之上，日 K 右侧条件未被破坏")

        if volume_ratio is not None and volume_ratio > 3.0:
            hard_vetoes.append("volume_overheated")
            reasons.append("量比过高，存在冲动放量或追高风险")

        if hard_vetoes:
            wolf_action = "no_entry"
            entry_type = "blocked"
            position_cap = "0%"
            confidence = "medium" if not data_limitations else "low"
        elif position_gate == "allow" and market_gate == "allow":
            wolf_action = "enter" if _healthy_volume(volume_ratio) else "probe"
            entry_type = "pullback_support"
            position_cap = "20%" if wolf_action == "enter" else "10%"
            confidence = "medium"
            entry_conditions.extend(
                [
                    "次日不明显高开，回踩 MA5 附近不破",
                    "分时企稳后成交量温和放大",
                    "板块或主线没有同步退潮",
                ]
            )
        elif position_gate == "allow":
            wolf_action = "probe"
            entry_type = "pullback_support"
            position_cap = "10%"
            confidence = "medium"
            entry_conditions.extend(
                [
                    "只允许次日低吸试探，不允许追涨确认",
                    "大盘转强或目标板块同步修复后再考虑加仓",
                ]
            )
        elif position_gate == "wait_pullback":
            wolf_action = "watch"
            entry_type = "wait_pullback"
            position_cap = "0%"
            confidence = "medium"
            entry_conditions.extend(
                [
                    "等待回踩 MA5 或 MA10 附近后再判断",
                    "回踩必须缩量且不能跌破关键均线",
                ]
            )
        else:
            wolf_action = "watch"
            entry_type = "wait_confirmation"
            position_cap = "0%"
            confidence = "low" if data_limitations else "medium"
            entry_conditions.extend(
                [
                    "先观察是否重新站上 MA5/MA10",
                    "等待量价和板块共振确认",
                ]
            )

        metrics = {
            "open": open_price,
            "close": close,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "boll_upper": boll_upper,
            "boll_mid": boll_mid,
            "boll_lower": boll_lower,
            "prior_red_low": prior_red_low,
            "bias_ma5": bias_ma5,
            "volume_ratio": volume_ratio,
            "bullish_alignment": bullish_alignment,
        }

        return WolfDecisionPolicyResult(
            wolf_action=wolf_action,
            market_gate=market_gate,
            position_gate=position_gate,
            entry_type=entry_type,
            position_cap=position_cap,
            confidence=confidence,
            hard_vetoes=hard_vetoes,
            reasons=_dedupe(reasons),
            entry_conditions=_dedupe(entry_conditions),
            invalid_conditions=_dedupe(invalid_conditions),
            data_limitations=_dedupe(data_limitations),
            metrics={key: value for key, value in metrics.items() if value is not None},
        )


def _evaluate_market_gate(daily_market_context: Any) -> tuple[str, List[str]]:
    if not isinstance(daily_market_context, Mapping):
        return "unknown", ["缺少大盘环境摘要，不能把个股机会放大为确定性入场"]

    tags = {
        str(tag).strip()
        for tag in daily_market_context.get("risk_tags", [])
        if str(tag).strip()
    } if isinstance(daily_market_context.get("risk_tags"), list) else set()
    summary = str(daily_market_context.get("summary") or "").strip()
    lowered = summary.lower()

    has_negated_blocking_text = any(
        term.lower() in lowered for term in _NEGATED_BLOCKING_SUMMARY_TERMS
    )
    has_blocking_text = (
        any(term.lower() in lowered for term in _BLOCKING_SUMMARY_TERMS)
        and not has_negated_blocking_text
    )

    if tags & _BLOCKING_MARKET_TAGS or has_blocking_text:
        return "block", ["大盘摘要命中高风险/退潮标签"]
    if tags & _CAUTION_MARKET_TAGS or any(term.lower() in lowered for term in _CAUTION_SUMMARY_TERMS):
        return "caution", ["大盘摘要偏谨慎，仓位必须下调"]
    return "allow", ["大盘摘要未触发阻断标签"]


def _phase_note(phase_context: Any) -> str:
    if not isinstance(phase_context, Mapping):
        return ""
    phase = str(phase_context.get("phase") or "").strip()
    if phase in {"intraday", "lunch_break", "closing_auction"}:
        return "not_postmarket_phase"
    if phase_context.get("is_partial_bar") is True:
        return "partial_daily_bar"
    return ""


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _calculate_bias(close: Optional[float], ma: Optional[float]) -> Optional[float]:
    if close is None or ma in (None, 0):
        return None
    return (close - ma) / ma * 100


def _ordered_desc(*values: Optional[float]) -> bool:
    if any(value is None for value in values):
        return False
    return all(values[idx] > values[idx + 1] for idx in range(len(values) - 1))  # type: ignore[operator]


def _healthy_volume(volume_ratio: Optional[float]) -> bool:
    return volume_ratio is None or 0.8 <= volume_ratio <= 2.5


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _append_list(lines: List[str], label: str, values: Any) -> None:
    if not isinstance(values, list) or not values:
        return
    lines.append(f"- {label}:")
    for value in values:
        text = str(value).strip()
        if text:
            lines.append(f"  - {text}")


def _format_metrics(metrics: Mapping[str, Any]) -> str:
    parts = []
    for key in (
        "open",
        "close",
        "ma5",
        "ma10",
        "ma20",
        "boll_upper",
        "boll_mid",
        "boll_lower",
        "prior_red_low",
        "bias_ma5",
        "volume_ratio",
        "bullish_alignment",
    ):
        if key not in metrics:
            continue
        value = metrics[key]
        if isinstance(value, float):
            value = round(value, 4)
        parts.append(f"{key}={value}")
    return ", ".join(parts)
