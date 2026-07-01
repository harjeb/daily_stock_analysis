# -*- coding: utf-8 -*-
"""Daily-K-only Wolf post-market report service."""

from __future__ import annotations

import base64
import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import pandas as pd

from src.config import Config
from src.services.history_loader import load_history_df
from src.services.wolf_decision_policy import evaluate_wolf_postmarket_policy

logger = logging.getLogger(__name__)


@dataclass
class WolfDailyPick:
    code: str
    scope: str
    name: str = ""
    source: str = ""
    policy: Dict[str, Any] = field(default_factory=dict)

    @property
    def action(self) -> str:
        return str(self.policy.get("wolf_action") or "watch")

    @property
    def position_cap(self) -> str:
        return str(self.policy.get("position_cap") or "0%")

    @property
    def confidence(self) -> str:
        return str(self.policy.get("confidence") or "low")


@dataclass
class WolfDailyReportResult:
    enabled: bool
    whitelist_count: int = 0
    stock_list_count: int = 0
    picks: List[WolfDailyPick] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source_errors: List[str] = field(default_factory=list)


class WolfDailyReportService:
    """Build a deterministic daily-K Wolf report for whitelist and STOCK_LIST."""

    def __init__(self, config: Config, *, daily_market_context: Optional[Mapping[str, Any]] = None) -> None:
        self.config = config
        self.daily_market_context = dict(daily_market_context or {})
        self._fetcher_manager = None

    def run(
        self,
        *,
        stock_codes: Optional[Iterable[str]] = None,
        daily_market_context: Optional[Mapping[str, Any]] = None,
    ) -> WolfDailyReportResult:
        if not bool(getattr(self.config, "wolf_daily_report_enabled", False)):
            return WolfDailyReportResult(enabled=False)

        if daily_market_context is not None:
            self.daily_market_context = dict(daily_market_context)

        result = WolfDailyReportResult(enabled=True)
        max_codes = max(1, int(getattr(self.config, "wolf_daily_max_codes", 30) or 30))

        if bool(getattr(self.config, "wolf_daily_whitelist_enabled", False)):
            whitelist_codes, whitelist_warnings = load_wolf_codes(
                file_path=str(getattr(self.config, "wolf_daily_whitelist_file", "") or ""),
                inline_content=str(getattr(self.config, "wolf_daily_whitelist_content", "") or ""),
                inline_content_b64=str(getattr(self.config, "wolf_daily_whitelist_content_b64", "") or ""),
            )
            result.warnings.extend(whitelist_warnings)
            result.whitelist_count = len(whitelist_codes)
            for code in whitelist_codes[:max_codes]:
                pick = self._evaluate_code(code, scope="whitelist")
                if pick:
                    result.picks.append(pick)

        if bool(getattr(self.config, "wolf_daily_stock_list_enabled", True)):
            normalized_stock_codes, skipped = normalize_wolf_code_list(stock_codes or getattr(self.config, "stock_list", []))
            result.stock_list_count = len(normalized_stock_codes)
            result.warnings.extend(f"STOCK_LIST skipped unsupported code: {code}" for code in skipped[:8])
            for code in normalized_stock_codes[:max_codes]:
                pick = self._evaluate_code(code, scope="stock_list")
                if pick:
                    result.picks.append(pick)

        if not result.picks and not result.warnings and not result.source_errors:
            result.warnings.append("Wolf daily report has no enabled scopes or valid A-share codes")
        return result

    def _evaluate_code(self, code: str, *, scope: str) -> Optional[WolfDailyPick]:
        days = max(30, int(getattr(self.config, "wolf_daily_history_days", 120) or 120))
        try:
            df, source = load_history_df(code, days=days)
        except Exception as exc:
            logger.warning("Wolf daily report failed to load %s history: %s", code, exc)
            return WolfDailyPick(
                code=code,
                scope=scope,
                name=self._stock_name(code),
                policy={
                    "wolf_action": "watch",
                    "position_cap": "0%",
                    "confidence": "low",
                    "data_limitations": [f"daily_history_error: {exc}"],
                },
            )

        if df is None or df.empty:
            return WolfDailyPick(
                code=code,
                scope=scope,
                name=self._stock_name(code),
                source=source,
                policy={
                    "wolf_action": "watch",
                    "position_cap": "0%",
                    "confidence": "low",
                    "data_limitations": ["missing_daily_history"],
                },
            )

        context = self._build_policy_context(df)
        context["daily_market_context"] = self.daily_market_context or {
            "summary": "",
            "risk_tags": [],
            "source": "wolf_daily_report",
        }
        context["market_phase_context"] = {"phase": "postmarket"}
        policy = evaluate_wolf_postmarket_policy(context)
        return WolfDailyPick(
            code=code,
            scope=scope,
            name=self._stock_name(code),
            source=source,
            policy=policy,
        )

    def _build_policy_context(self, df: pd.DataFrame) -> Dict[str, Any]:
        frame = df.copy()
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
            frame = frame.sort_values("date")
        frame = frame.dropna(subset=["close"]).reset_index(drop=True)
        if frame.empty:
            return {"today": {}}

        for column in ("open", "high", "low", "close", "volume", "ma5", "ma10", "ma20", "volume_ratio"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")

        close = pd.to_numeric(frame["close"], errors="coerce")
        for window, column in ((5, "ma5"), (10, "ma10"), (20, "ma20")):
            if column not in frame.columns or frame[column].isna().all():
                frame[column] = close.rolling(window, min_periods=1).mean()

        if "volume_ratio" not in frame.columns or frame["volume_ratio"].isna().all():
            volume = pd.to_numeric(frame.get("volume"), errors="coerce")
            avg_volume = volume.shift(1).rolling(5, min_periods=1).mean()
            frame["volume_ratio"] = volume / avg_volume.replace(0, pd.NA)

        rolling_mid = close.rolling(20, min_periods=5).mean()
        rolling_std = close.rolling(20, min_periods=5).std(ddof=0)
        frame["boll_mid"] = rolling_mid
        frame["boll_upper"] = rolling_mid + rolling_std * 2
        frame["boll_lower"] = rolling_mid - rolling_std * 2

        current = frame.iloc[-1].to_dict()
        previous_red_low = _previous_red_low(frame.iloc[:-1])
        if previous_red_low is not None:
            current["prior_red_low"] = previous_red_low

        return {
            "today": _drop_nan(current),
            "trend_analysis": {
                "ma5": _safe_float(current.get("ma5")),
                "ma10": _safe_float(current.get("ma10")),
                "ma20": _safe_float(current.get("ma20")),
                "bias_ma5": _calculate_bias(
                    _safe_float(current.get("close")),
                    _safe_float(current.get("ma5")),
                ),
                "boll_upper": _safe_float(current.get("boll_upper")),
                "boll_mid": _safe_float(current.get("boll_mid")),
                "boll_lower": _safe_float(current.get("boll_lower")),
            },
            "realtime": {
                "volume_ratio": _safe_float(current.get("volume_ratio")),
            },
        }

    def _stock_name(self, code: str) -> str:
        try:
            manager = self._get_fetcher_manager()
            name = manager.get_stock_name(code, allow_realtime=False)
            return str(name or "").strip()
        except Exception:
            return ""

    def _get_fetcher_manager(self):
        if self._fetcher_manager is None:
            from data_provider import DataFetcherManager

            self._fetcher_manager = DataFetcherManager()
        return self._fetcher_manager


def format_wolf_daily_report_markdown(result: WolfDailyReportResult) -> str:
    if not result.enabled:
        return ""

    whitelist = [pick for pick in result.picks if pick.scope == "whitelist"]
    stock_list = [pick for pick in result.picks if pick.scope == "stock_list"]
    lines = [
        "# 🐺 狼哥日K盘后分析",
        "",
        f"> 白名单 {result.whitelist_count} 只 | STOCK_LIST {result.stock_list_count} 只 | 已评估 {len(result.picks)} 只",
        "> 数据边界：本报告只使用日 K、均线、BOLL、量价和已有大盘摘要；不使用 15 分钟 K，不生成盘中即时买卖指令。",
        "",
    ]

    if whitelist:
        lines.extend(["## 白名单观察 / 入场候选", ""])
        for index, pick in enumerate(_sort_picks(whitelist), 1):
            lines.extend(_format_pick(index, pick, holding_mode=False))
        lines.append("")

    if stock_list:
        lines.extend(["## STOCK_LIST 操作分析", ""])
        for index, pick in enumerate(_sort_picks(stock_list), 1):
            lines.extend(_format_pick(index, pick, holding_mode=True))
        lines.append("")

    if not whitelist and not stock_list:
        lines.extend(["本次没有可评估的 A 股标的。", ""])

    notes = list(dict.fromkeys([*result.warnings, *result.source_errors]))
    if notes:
        lines.extend(["## 降级提示", ""])
        for note in notes[:8]:
            lines.append(f"- {_compact_text(note, 160)}")
        lines.append("")

    return "\n".join(lines).strip()


def load_wolf_codes(
    *,
    file_path: str,
    inline_content: str = "",
    inline_content_b64: str = "",
) -> tuple[List[str], List[str]]:
    warnings: List[str] = []
    content_parts: List[str] = []
    if file_path:
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            content_parts.append(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            warnings.append(f"Wolf whitelist file not found: {file_path}")
        except UnicodeDecodeError:
            content_parts.append(path.read_text(encoding="gbk", errors="ignore"))
    if inline_content:
        content_parts.append(inline_content)
    if inline_content_b64:
        try:
            content_parts.append(base64.b64decode(inline_content_b64).decode("utf-8-sig"))
        except Exception as exc:
            warnings.append(f"Wolf whitelist b64 content cannot be decoded: {exc}")

    codes, skipped = normalize_wolf_code_list(_pool_tokens("\n".join(content_parts)))
    warnings.extend(f"Wolf whitelist skipped unsupported code: {code}" for code in skipped[:8])
    return codes, warnings


def normalize_wolf_code_list(values: Iterable[Any]) -> tuple[List[str], List[str]]:
    codes: List[str] = []
    skipped: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        code = normalize_wolf_code(text)
        if code:
            codes.append(code)
        else:
            skipped.append(text)
    return list(dict.fromkeys(codes)), list(dict.fromkeys(skipped))


def normalize_wolf_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = text.replace("SH.", "SH").replace("SZ.", "SZ")
    text = re.sub(r"^(SH|SZ)", "", text)
    text = re.sub(r"\.(SH|SZ)$", "", text)
    match = re.search(r"\b\d{6}\b", text)
    return match.group(0) if match else ""


def _pool_tokens(content: str) -> List[str]:
    tokens: List[str] = []
    try:
        dialect = csv.Sniffer().sniff(content[:4096])
        rows = csv.reader(content.splitlines(), dialect)
        for row in rows:
            tokens.extend(row)
    except Exception:
        tokens = re.split(r"[\s,;，；|]+", content)
    expanded: List[str] = []
    for token in tokens:
        expanded.extend(re.split(r"[\s,;，；|]+", str(token)))
    return expanded


def _previous_red_low(frame: pd.DataFrame) -> Optional[float]:
    if frame.empty or not {"open", "close", "low"}.issubset(frame.columns):
        return None
    for _, row in frame.iloc[::-1].iterrows():
        open_price = _safe_float(row.get("open"))
        close = _safe_float(row.get("close"))
        low = _safe_float(row.get("low"))
        if open_price is not None and close is not None and low is not None and close > open_price:
            return low
    return None


def _drop_nan(values: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if _safe_float(value) is not None or key in {"date", "code"}}


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed


def _calculate_bias(close: Optional[float], ma: Optional[float]) -> Optional[float]:
    if close is None or ma in (None, 0):
        return None
    return (close - ma) / ma * 100


def _sort_picks(picks: List[WolfDailyPick]) -> List[WolfDailyPick]:
    action_rank = {"enter": 0, "probe": 1, "watch": 2, "no_entry": 3, "reduce": 4, "exit": 5}
    return sorted(picks, key=lambda item: (action_rank.get(item.action, 9), item.code))


def _format_pick(index: int, pick: WolfDailyPick, *, holding_mode: bool) -> List[str]:
    policy = pick.policy
    name_text = f"{pick.name}({pick.code})" if pick.name and pick.name != pick.code else pick.code
    action_label = _holding_action_label(pick.action) if holding_mode else _entry_action_label(pick.action)
    lines = [
        f"{index}. **{name_text}** | {action_label} | 仓位上限 {pick.position_cap} | 置信度 {pick.confidence}",
    ]
    metrics = policy.get("metrics") if isinstance(policy.get("metrics"), Mapping) else {}
    metric_text = _format_metrics(metrics)
    if metric_text:
        lines.append(f"   - 日K：{metric_text}")
    reasons = [str(item) for item in policy.get("reasons") or [] if str(item).strip()]
    if reasons:
        lines.append(f"   - 依据：{_compact_text('；'.join(reasons[:3]), 160)}")
    entry_conditions = [str(item) for item in policy.get("entry_conditions") or [] if str(item).strip()]
    if entry_conditions:
        lines.append(f"   - 触发：{_compact_text('；'.join(entry_conditions[:2]), 140)}")
    invalid_conditions = [str(item) for item in policy.get("invalid_conditions") or [] if str(item).strip()]
    if invalid_conditions:
        lines.append(f"   - 失效：{_compact_text('；'.join(invalid_conditions[:2]), 140)}")
    hard_vetoes = [str(item) for item in policy.get("hard_vetoes") or [] if str(item).strip()]
    if hard_vetoes:
        lines.append(f"   - 硬否决：{', '.join(hard_vetoes[:4])}")
    return lines


def _entry_action_label(action: str) -> str:
    return {
        "enter": "可入场",
        "probe": "可试探",
        "watch": "观察",
        "no_entry": "不入场",
        "reduce": "减仓信号",
        "exit": "退出信号",
    }.get(action, action)


def _holding_action_label(action: str) -> str:
    return {
        "enter": "可加关注/低吸",
        "probe": "只可试探",
        "watch": "持有观察/等确认",
        "no_entry": "不加仓",
        "reduce": "减仓",
        "exit": "退出",
    }.get(action, action)


def _format_metrics(metrics: Mapping[str, Any]) -> str:
    parts = []
    for key, label in (
        ("close", "收盘"),
        ("ma5", "MA5"),
        ("ma10", "MA10"),
        ("ma20", "MA20"),
        ("bias_ma5", "MA5乖离"),
        ("volume_ratio", "量比"),
    ):
        if key not in metrics:
            continue
        value = metrics[key]
        if isinstance(value, float):
            value = round(value, 2)
        suffix = "%" if key == "bias_ma5" else ""
        parts.append(f"{label} {value}{suffix}")
    return " | ".join(parts)


def _compact_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
