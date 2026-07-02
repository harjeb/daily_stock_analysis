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
    hot_sectors: List[str] = field(default_factory=list)
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
    hot_sector_count: int = 0
    hot_sector_candidate_count: int = 0
    hot_sector_names: List[str] = field(default_factory=list)
    picks: List[WolfDailyPick] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source_errors: List[str] = field(default_factory=list)


@dataclass
class WolfHotSectorSelection:
    board_names: List[str] = field(default_factory=list)
    code_to_boards: Dict[str, List[str]] = field(default_factory=dict)
    source_errors: List[str] = field(default_factory=list)
    lookback_days: int = 60
    board_trends: Dict[str, float] = field(default_factory=dict)


class WolfDailyReportService:
    """Build a daily-K Wolf report for whitelist and STOCK_LIST.

    Supports two modes:
    - Deterministic (default): hardcoded guardrails via evaluate_wolf_postmarket_policy
    - LLM-driven (wolf_daily_use_llm=True): sends data to LLM with wolf rulebook
    """

    def __init__(self, config: Config, *, daily_market_context: Optional[Mapping[str, Any]] = None) -> None:
        self.config = config
        self.daily_market_context = dict(daily_market_context or {})
        self._fetcher_manager = None
        self._llm_service = None
        self._market_stats: Optional[Dict[str, Any]] = None

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
        hot_sector_selection = self._build_hot_sector_selection()
        if hot_sector_selection:
            result.hot_sector_count = len(hot_sector_selection.board_names)
            result.hot_sector_names = list(hot_sector_selection.board_names)
            result.source_errors.extend(hot_sector_selection.source_errors)

        use_llm = bool(getattr(self.config, "wolf_daily_use_llm", False))
        if use_llm:
            self._market_stats = self._fetch_market_stats()

        if bool(getattr(self.config, "wolf_daily_whitelist_enabled", False)):
            whitelist_codes, whitelist_warnings = load_wolf_codes(
                file_path=str(getattr(self.config, "wolf_daily_whitelist_file", "") or ""),
                inline_content=str(getattr(self.config, "wolf_daily_whitelist_content", "") or ""),
                inline_content_b64=str(getattr(self.config, "wolf_daily_whitelist_content_b64", "") or ""),
            )
            result.warnings.extend(whitelist_warnings)
            result.whitelist_count = len(whitelist_codes)
            selected_codes, selection_warnings = select_wolf_scope_codes(
                whitelist_codes,
                scope="whitelist",
                max_codes=max_codes,
                hot_sector_selection=hot_sector_selection,
            )
            result.warnings.extend(selection_warnings)
            result.hot_sector_candidate_count += sum(
                1 for code in selected_codes if hot_sector_selection and code in hot_sector_selection.code_to_boards
            )
            for code in selected_codes:
                pick = self._evaluate_code(
                    code,
                    scope="whitelist",
                    hot_sectors=(hot_sector_selection.code_to_boards.get(code, []) if hot_sector_selection else []),
                    board_trends=(hot_sector_selection.board_trends if hot_sector_selection else None),
                )
                if pick:
                    result.picks.append(pick)

        if bool(getattr(self.config, "wolf_daily_stock_list_enabled", True)):
            normalized_stock_codes, skipped = normalize_wolf_code_list(stock_codes or getattr(self.config, "stock_list", []))
            result.stock_list_count = len(normalized_stock_codes)
            result.warnings.extend(f"STOCK_LIST skipped unsupported code: {code}" for code in skipped[:8])
            selected_codes, selection_warnings = select_wolf_scope_codes(
                normalized_stock_codes,
                scope="stock_list",
                max_codes=max_codes,
                hot_sector_selection=hot_sector_selection,
            )
            result.warnings.extend(selection_warnings)
            result.hot_sector_candidate_count += sum(
                1 for code in selected_codes if hot_sector_selection and code in hot_sector_selection.code_to_boards
            )
            for code in selected_codes:
                pick = self._evaluate_code(
                    code,
                    scope="stock_list",
                    hot_sectors=(hot_sector_selection.code_to_boards.get(code, []) if hot_sector_selection else []),
                    board_trends=(hot_sector_selection.board_trends if hot_sector_selection else None),
                )
                if pick:
                    result.picks.append(pick)

        if not result.picks and not result.warnings and not result.source_errors:
            result.warnings.append("Wolf daily report has no enabled scopes or valid A-share codes")
        return result

    def _evaluate_code(
        self,
        code: str,
        *,
        scope: str,
        hot_sectors: Optional[List[str]] = None,
        board_trends: Optional[Dict[str, float]] = None,
    ) -> Optional[WolfDailyPick]:
        days = max(30, int(getattr(self.config, "wolf_daily_history_days", 120) or 120))
        hot_sectors = list(hot_sectors or [])
        try:
            df, source = load_history_df(code, days=days)
        except Exception as exc:
            logger.warning("Wolf daily report failed to load %s history: %s", code, exc)
            return WolfDailyPick(
                code=code,
                scope=scope,
                name=self._stock_name(code),
                hot_sectors=hot_sectors,
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
                hot_sectors=hot_sectors,
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

        # Choose evaluation path: LLM or deterministic
        use_llm = bool(getattr(self.config, "wolf_daily_use_llm", False))
        if use_llm:
            self._enrich_context_for_llm(code, context, hot_sectors=hot_sectors, board_trends=board_trends)
            policy = self._evaluate_with_llm(code, context, hot_sectors=hot_sectors)
        else:
            policy = evaluate_wolf_postmarket_policy(context)

        if hot_sectors:
            policy["hot_sector_matches"] = hot_sectors
            reasons = [str(item) for item in policy.get("reasons") or [] if str(item).strip()]
            policy["reasons"] = [
                f"命中近60日强势板块：{'、'.join(hot_sectors[:3])}",
                *reasons,
            ]
        return WolfDailyPick(
            code=code,
            scope=scope,
            name=self._stock_name(code),
            source=source,
            hot_sectors=hot_sectors,
            policy=policy,
        )

    def _evaluate_with_llm(
        self,
        code: str,
        context: Mapping[str, Any],
        *,
        hot_sectors: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Evaluate stock using LLM-driven wolf analysis."""
        if self._llm_service is None:
            from src.services.wolf_llm_analysis_service import WolfLLMAnalysisService
            self._llm_service = WolfLLMAnalysisService(self.config)

        name = self._stock_name(code)
        return self._llm_service.analyze_stock(
            code,
            name,
            context,
            daily_market_context=self.daily_market_context,
            hot_sectors=hot_sectors,
        )

    def _fetch_market_stats(self) -> Dict[str, Any]:
        try:
            manager = self._get_fetcher_manager()
            stats = manager.get_market_stats(purpose="wolf_llm")
            if stats and isinstance(stats, dict):
                return stats
        except Exception as exc:
            logger.warning("Wolf LLM: failed to fetch market stats: %s", exc)
        return {}

    def _enrich_context_for_llm(
        self,
        code: str,
        context: Dict[str, Any],
        *,
        hot_sectors: Optional[List[str]] = None,
        board_trends: Optional[Dict[str, float]] = None,
    ) -> None:
        """Enrich context with supplementary data for LLM analysis."""
        breadth: Dict[str, Any] = {}
        if self._market_stats:
            breadth = {
                key: val for key, val in self._market_stats.items()
                if key in ("up_count", "down_count", "flat_count",
                           "limit_up_count", "limit_down_count", "total_amount")
                and val is not None
            }
        context["breadth"] = breadth

        sector_info: Dict[str, Any] = {}
        if hot_sectors and board_trends:
            sector_info["matched_sectors"] = [
                {"name": name, "change_60d": board_trends.get(name)}
                for name in hot_sectors[:5]
                if name in board_trends
            ]
        if board_trends:
            sorted_trends = sorted(board_trends.items(), key=lambda x: x[1], reverse=True)
            sector_info["top_sectors_60d"] = [
                {"name": name, "change_60d": pct}
                for name, pct in sorted_trends[:5]
            ]
        context["sector_trend"] = sector_info

        # Fetch daily sector rankings for mainline判断
        try:
            manager = self._get_fetcher_manager()
            top_sectors, bottom_sectors = manager.get_sector_rankings(5)
            if top_sectors:
                sector_info["daily_top_sectors"] = [
                    {"name": s.get("name", ""), "change_pct": s.get("change_pct")}
                    for s in top_sectors[:5] if isinstance(s, Mapping)
                ]
            if bottom_sectors:
                sector_info["daily_bottom_sectors"] = [
                    {"name": s.get("name", ""), "change_pct": s.get("change_pct")}
                    for s in bottom_sectors[:3] if isinstance(s, Mapping)
                ]
        except Exception as exc:
            logger.debug("Wolf LLM: sector rankings fetch failed: %s", exc)

        try:
            manager = self._get_fetcher_manager()
            quote = manager.get_realtime_quote(code, log_final_failure=False)
            if quote is not None:
                realtime_extra: Dict[str, Any] = {}
                for field_name in ("volume_ratio", "turnover_rate", "pe_ratio",
                                   "pb_ratio", "total_mv", "circ_mv",
                                   "amplitude", "change_60d", "high_52w", "low_52w"):
                    val = getattr(quote, field_name, None)
                    if val is not None:
                        try:
                            realtime_extra[field_name] = round(float(val), 4)
                        except (TypeError, ValueError):
                            pass
                context["realtime_extra"] = realtime_extra
        except Exception as exc:
            logger.debug("Wolf LLM: realtime quote fetch failed for %s: %s", code, exc)

        # stock_is_core: fetch base_info for industry/ROE/etc
        stock_profile: Dict[str, Any] = {}
        try:
            manager = self._get_fetcher_manager()
            for fetcher in manager._fetchers:
                if hasattr(fetcher, "get_base_info"):
                    info = fetcher.get_base_info(code)
                    if info and isinstance(info, dict):
                        for raw_key, canonical in (
                            ("行业", "industry"), ("所属行业", "industry"),
                            ("市盈率(动)", "pe_ttm"), ("市净率", "pb"),
                            ("ROE", "roe"), ("净利率", "net_margin"),
                            ("总市值", "total_mv"), ("流通市值", "circ_mv"),
                        ):
                            val = info.get(raw_key)
                            if val is not None:
                                stock_profile[canonical] = val
                        break
        except Exception as exc:
            logger.debug("Wolf LLM: base_info fetch failed for %s: %s", code, exc)

        # belong_board
        try:
            manager = self._get_fetcher_manager()
            for fetcher in manager._fetchers:
                if hasattr(fetcher, "get_belong_board"):
                    belong_df = fetcher.get_belong_board(code)
                    if belong_df is not None and not belong_df.empty:
                        name_col = "板块名称" if "板块名称" in belong_df.columns else (
                            "name" if "name" in belong_df.columns else None
                        )
                        if name_col:
                            stock_profile["belong_boards"] = [
                                str(v) for v in belong_df[name_col].tolist()[:10] if str(v).strip()
                            ]
                        break
        except Exception as exc:
            logger.debug("Wolf LLM: belong_board fetch failed for %s: %s", code, exc)

        if stock_profile:
            context["stock_profile"] = stock_profile

        context["user_can_monitor_intraday"] = bool(
            getattr(self.config, "wolf_user_can_monitor_intraday", False)
        )

    def _build_hot_sector_selection(self) -> Optional[WolfHotSectorSelection]:
        if not bool(getattr(self.config, "wolf_daily_hot_sector_filter_enabled", True)):
            return None

        top_n = max(0, int(getattr(self.config, "wolf_daily_hot_sector_top_n", 12) or 0))
        min_change_pct = float(getattr(self.config, "wolf_daily_hot_sector_min_change_pct", 0.0) or 0.0)
        try:
            from src.services.alphasift_service import DsaEastMoneyHotspotProvider

            provider = DsaEastMoneyHotspotProvider()
            rows = provider.board_performance_rows(top=max(top_n, 50))
        except Exception as exc:
            logger.warning("Wolf hot sector selection failed to load board performance: %s", exc)
            return WolfHotSectorSelection(source_errors=[f"wolf_hot_sector_performance_failed: {exc}"])

        ranked_boards = _rank_hot_sector_rows(rows, top_n=top_n, min_change_pct=min_change_pct)
        if not ranked_boards:
            return WolfHotSectorSelection(source_errors=["wolf_hot_sector_performance_empty"])

        code_to_boards: Dict[str, List[str]] = {}
        source_errors: List[str] = []
        for board in ranked_boards:
            name = str(board.get("name") or "").strip()
            source = str(board.get("source") or "concept").strip()
            if not name:
                continue
            try:
                frame = (
                    provider.stock_board_industry_cons_em(name)
                    if source == "industry"
                    else provider.stock_board_concept_cons_em(name)
                )
                for code in _extract_constituent_codes(frame):
                    code_to_boards.setdefault(code, [])
                    if name not in code_to_boards[code]:
                        code_to_boards[code].append(name)
            except Exception as exc:
                logger.warning("Wolf hot sector constituent fetch failed for %s: %s", name, exc)
                source_errors.append(f"wolf_hot_sector_constituents_failed:{name}:{exc}")

        board_trends = {
            str(board.get("name") or "").strip(): float(board.get("change_60d") or 0.0)
            for board in ranked_boards
            if str(board.get("name") or "").strip()
        }
        return WolfHotSectorSelection(
            board_names=[str(board.get("name") or "").strip() for board in ranked_boards if str(board.get("name") or "").strip()],
            code_to_boards=code_to_boards,
            source_errors=source_errors,
            lookback_days=60,
            board_trends=board_trends,
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

        entry_zone = self._compute_entry_zone(frame)

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
            "planned_entry_zone": entry_zone,
        }

    def _stock_name(self, code: str) -> str:
        try:
            manager = self._get_fetcher_manager()
            name = manager.get_stock_name(code, allow_realtime=False)
            return str(name or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _compute_entry_zone(frame: pd.DataFrame) -> Dict[str, Any]:
        """Compute candidate support/resistance levels from daily K-line."""
        if frame.empty:
            return {}

        close = _safe_float(frame.iloc[-1].get("close"))
        if close is None:
            return {}

        supports: List[Dict[str, Any]] = []
        resistances: List[Dict[str, Any]] = []

        for label, key in (("MA5", "ma5"), ("MA10", "ma10"), ("MA20", "ma20"), ("MA60", "ma60")):
            val = _safe_float(frame.iloc[-1].get(key))
            if val is not None and val > 0:
                entry = {"level": label, "price": round(val, 4), "distance_pct": round((close - val) / val * 100, 2)}
                if val < close:
                    supports.append(entry)
                elif val > close:
                    resistances.append(entry)

        boll_lower = _safe_float(frame.iloc[-1].get("boll_lower"))
        boll_mid = _safe_float(frame.iloc[-1].get("boll_mid"))
        boll_upper = _safe_float(frame.iloc[-1].get("boll_upper"))
        if boll_lower is not None and boll_lower > 0 and boll_lower < close:
            supports.append({"level": "BOLL下轨", "price": round(boll_lower, 4), "distance_pct": round((close - boll_lower) / boll_lower * 100, 2)})
        if boll_mid is not None and boll_mid > 0:
            if boll_mid < close:
                supports.append({"level": "BOLL中轨", "price": round(boll_mid, 4), "distance_pct": round((close - boll_mid) / boll_mid * 100, 2)})
            elif boll_mid > close:
                resistances.append({"level": "BOLL中轨", "price": round(boll_mid, 4), "distance_pct": round((close - boll_mid) / boll_mid * 100, 2)})
        if boll_upper is not None and boll_upper > 0 and boll_upper > close:
            resistances.append({"level": "BOLL上轨", "price": round(boll_upper, 4), "distance_pct": round((close - boll_upper) / boll_upper * 100, 2)})

        recent = frame.tail(20)
        if "low" in recent.columns:
            recent_low = _safe_float(recent["low"].min())
            if recent_low is not None and recent_low > 0 and recent_low < close:
                supports.append({"level": "近20日低点", "price": round(recent_low, 4), "distance_pct": round((close - recent_low) / recent_low * 100, 2)})
        if "high" in recent.columns:
            recent_high = _safe_float(recent["high"].max())
            if recent_high is not None and recent_high > 0 and recent_high > close:
                resistances.append({"level": "近20日高点", "price": round(recent_high, 4), "distance_pct": round((close - recent_high) / recent_high * 100, 2)})

        prior_red = _safe_float(_previous_red_low(frame.iloc[:-1]))
        if prior_red is not None and prior_red > 0:
            if prior_red < close:
                supports.append({"level": "前红K低点", "price": round(prior_red, 4), "distance_pct": round((close - prior_red) / prior_red * 100, 2)})
            elif prior_red > close:
                resistances.append({"level": "前红K低点", "price": round(prior_red, 4), "distance_pct": round((close - prior_red) / prior_red * 100, 2)})

        supports.sort(key=lambda x: x["distance_pct"], reverse=True)
        resistances.sort(key=lambda x: x["distance_pct"])

        return {
            "supports": supports[:5],
            "resistances": resistances[:5],
            "current_close": close,
        }

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
    sorted_whitelist = _sort_picks(whitelist)
    sorted_stock_list = _sort_picks(stock_list)
    notes = list(dict.fromkeys([*result.warnings, *result.source_errors]))
    lines = [
        "# 🐺 狼哥日K盘后分析",
        "",
        f"> 白名单 {result.whitelist_count} 只 | STOCK_LIST {result.stock_list_count} 只 | 已评估 {len(result.picks)} 只",
        "> 怎么读：先看“动作”和“下一步”。Wolf 报告是日K盘后计划，不是盘中即时买卖指令。",
        "> 数据边界：本报告只使用日 K、均线、BOLL、量价和已有大盘摘要；不使用 15 分钟 K，不生成盘中即时买卖指令。",
        "",
    ]
    if result.whitelist_count == 0 and any("whitelist file not found" in note.lower() for note in notes):
        lines.extend([
            "> 白名单未分析：未找到白名单文件，也没有注入白名单内容。若要分析白名单，请配置 `WOLF_DAILY_WHITELIST_CONTENT` 或提交 `WOLF_DAILY_WHITELIST_FILE` 指向的文件。",
            "",
        ])
    if result.hot_sector_names:
        sector_text = "、".join(result.hot_sector_names[:8])
        suffix = "…" if len(result.hot_sector_names) > 8 else ""
        lines.extend([
            f"> 近60日强势板块 {result.hot_sector_count} 个：{sector_text}{suffix}",
            "",
        ])

    if result.picks:
        lines.extend(["## 快速结论", ""])
        lines.append("| 范围 | 股票 | 动作 | 位置 | 下一步 | 主要风险 |")
        lines.append("|---|---|---|---|---|---|")
        for pick in [*sorted_whitelist, *sorted_stock_list]:
            lines.append(_format_pick_summary_row(pick))
        lines.append("")

    common_reasons = _common_market_reasons(result.picks)
    if common_reasons:
        lines.extend(["## 共同约束", ""])
        for reason in common_reasons[:3]:
            lines.append(f"- {_compact_text(reason, 120)}")
        lines.append("")

    if whitelist:
        lines.extend(["## 白名单观察 / 入场候选", ""])
        for index, pick in enumerate(sorted_whitelist, 1):
            lines.extend(_format_pick(index, pick, holding_mode=False))
        lines.append("")

    if stock_list:
        lines.extend(["## STOCK_LIST 操作分析", ""])
        for index, pick in enumerate(sorted_stock_list, 1):
            lines.extend(_format_pick(index, pick, holding_mode=True))
        lines.append("")

    if not whitelist and not stock_list:
        lines.extend(["本次没有可评估的 A 股标的。", ""])

    if notes:
        lines.extend(["## 降级提示", ""])
        for note in notes[:8]:
            lines.append(f"- {_compact_text(_humanize_wolf_note(note), 180)}")
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


def select_wolf_scope_codes(
    codes: List[str],
    *,
    scope: str,
    max_codes: int,
    hot_sector_selection: Optional[WolfHotSectorSelection],
) -> tuple[List[str], List[str]]:
    warnings: List[str] = []
    normalized_max = max(1, int(max_codes or 1))
    unique_codes = list(dict.fromkeys(codes))
    if not unique_codes:
        return [], warnings

    if not hot_sector_selection or not hot_sector_selection.code_to_boards:
        selected = unique_codes[:normalized_max]
        if len(unique_codes) > len(selected):
            warnings.append(
                f"Wolf {scope} hot-sector data unavailable; capped {len(unique_codes)} codes to {len(selected)}"
            )
        return selected, warnings

    hot_codes = [code for code in unique_codes if code in hot_sector_selection.code_to_boards]
    if scope == "whitelist":
        if hot_codes:
            warnings.append(
                f"Wolf whitelist hot-sector filter selected {len(hot_codes)} of {len(unique_codes)} codes"
            )
            return hot_codes, warnings
        selected = unique_codes[:normalized_max]
        warnings.append(
            f"Wolf whitelist had no near-60d hot-sector matches; fallback capped {len(unique_codes)} codes to {len(selected)}"
        )
        return selected, warnings

    if len(unique_codes) <= normalized_max:
        return unique_codes, warnings

    if hot_codes:
        selected = list(hot_codes)
        for code in unique_codes:
            if code in hot_sector_selection.code_to_boards or len(selected) >= normalized_max:
                continue
            selected.append(code)
        warnings.append(
            f"Wolf {scope} kept {len(hot_codes)} hot-sector matches and capped total {len(unique_codes)} codes to {len(selected)}"
        )
        return selected, warnings

    selected = unique_codes[:normalized_max]
    warnings.append(f"Wolf {scope} had no hot-sector matches; capped {len(unique_codes)} codes to {len(selected)}")
    return selected, warnings


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


def _rank_hot_sector_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    top_n: int,
    min_change_pct: float,
) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or row.get("板块名称") or row.get("行业名称") or "").strip()
        if not name or name in seen:
            continue
        change_60d = _safe_float(row.get("change_60d") or row.get("60日涨跌幅"))
        if change_60d is None or change_60d < min_change_pct:
            continue
        seen.add(name)
        ranked.append({
            "name": name,
            "source": str(row.get("source") or "concept").strip() or "concept",
            "change_60d": change_60d,
        })
    ranked.sort(key=lambda item: float(item.get("change_60d") or 0.0), reverse=True)
    return ranked[:top_n] if top_n > 0 else ranked


def _extract_constituent_codes(frame: Any) -> List[str]:
    df = pd.DataFrame(frame)
    if df.empty:
        return []
    candidate_columns = (
        "code",
        "代码",
        "股票代码",
        "证券代码",
        "成分股代码",
        "f12",
    )
    codes: List[str] = []
    for column in candidate_columns:
        if column not in df.columns:
            continue
        for value in df[column].tolist():
            code = normalize_wolf_code(value)
            if code:
                codes.append(code)
        if codes:
            break
    if codes:
        return list(dict.fromkeys(codes))
    for row in df.to_dict(orient="records"):
        for value in row.values():
            code = normalize_wolf_code(value)
            if code:
                codes.append(code)
                break
    return list(dict.fromkeys(codes))


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
    stock_reasons = _stock_specific_reasons(reasons)
    if stock_reasons:
        lines.append(f"   - 为什么：{_compact_text('；'.join(stock_reasons[:3]), 180)}")
    if pick.hot_sectors:
        lines.append(f"   - 板块：{'、'.join(pick.hot_sectors[:4])}")
    entry_conditions = [str(item) for item in policy.get("entry_conditions") or [] if str(item).strip()]
    if entry_conditions:
        lines.append(f"   - 下一步：{_compact_text('；'.join(entry_conditions[:2]), 150)}")
    invalid_conditions = [str(item) for item in policy.get("invalid_conditions") or [] if str(item).strip()]
    if invalid_conditions:
        lines.append(f"   - 风险线：{_compact_text('；'.join(invalid_conditions[:2]), 150)}")
    hard_vetoes = [str(item) for item in policy.get("hard_vetoes") or [] if str(item).strip()]
    if hard_vetoes:
        lines.append(f"   - 硬否决：{', '.join(hard_vetoes[:4])}")
    alignment = str(policy.get("market_sector_stock_alignment") or "").strip()
    if alignment and alignment != "unknown":
        alignment_label = {
            "aligned": "大盘/板块/个股对齐",
            "mixed": "大盘/板块/个股部分冲突",
            "conflict": "大盘/板块/个股冲突",
        }.get(alignment, alignment)
        lines.append(f"   - 对齐：{alignment_label}")
    next_day_paths = [str(item) for item in policy.get("next_day_paths") or [] if str(item).strip()]
    if next_day_paths:
        for path in next_day_paths[:3]:
            lines.append(f"   - 路径：{_compact_text(path, 150)}")
    stop_ref = str(policy.get("stop_reference_type") or "").strip()
    if stop_ref and stop_ref != "none":
        stop_label = {
            "stock_close": "突破日收盘价",
            "sector_index": "板块指数",
            "ma5": "MA5",
            "ma20": "MA20",
            "ma60": "MA60",
            "prior_red_candle": "前红K低点",
        }.get(stop_ref, stop_ref)
        lines.append(f"   - 止损参考：{stop_label}")
    return lines


def _format_pick_summary_row(pick: WolfDailyPick) -> str:
    name_text = f"{pick.name}({pick.code})" if pick.name and pick.name != pick.code else pick.code
    scope_label = "白名单" if pick.scope == "whitelist" else "自选股"
    holding_mode = pick.scope == "stock_list"
    action_label = _holding_action_label(pick.action) if holding_mode else _entry_action_label(pick.action)
    metrics = pick.policy.get("metrics") if isinstance(pick.policy.get("metrics"), Mapping) else {}
    return (
        f"| {scope_label} | {_escape_table_cell(name_text)} | {_escape_table_cell(action_label)} "
        f"| {_escape_table_cell(_position_summary(metrics))} "
        f"| {_escape_table_cell(_next_step_summary(pick))} "
        f"| {_escape_table_cell(_risk_summary(pick))} |"
    )


def _position_summary(metrics: Mapping[str, Any]) -> str:
    close = _safe_float(metrics.get("close"))
    ma5 = _safe_float(metrics.get("ma5"))
    ma10 = _safe_float(metrics.get("ma10"))
    ma20 = _safe_float(metrics.get("ma20"))
    bias_ma5 = _safe_float(metrics.get("bias_ma5"))
    if close is None:
        return "价格数据不足"
    if ma20 is not None and close < ma20:
        return "跌破MA20"
    if bias_ma5 is not None and bias_ma5 > 5:
        return "离MA5过远"
    if ma5 is not None and abs(_calculate_bias(close, ma5) or 0) <= 2:
        return "贴近MA5"
    if ma10 is not None and close < ma10:
        return "低于MA10"
    if ma5 is not None and ma10 is not None and ma20 is not None and ma5 > ma10 > ma20:
        return "多头排列"
    return "结构待确认"


def _next_step_summary(pick: WolfDailyPick) -> str:
    policy = pick.policy
    entry_conditions = [str(item) for item in policy.get("entry_conditions") or [] if str(item).strip()]
    if entry_conditions:
        return _compact_text(entry_conditions[0], 46)
    action = pick.action
    if action in {"enter", "probe"}:
        return "只按回踩计划执行"
    if action in {"no_entry", "reduce", "exit"}:
        return "不加仓，先处理风险"
    return "等站回关键均线"


def _risk_summary(pick: WolfDailyPick) -> str:
    policy = pick.policy
    hard_vetoes = [str(item) for item in policy.get("hard_vetoes") or [] if str(item).strip()]
    if hard_vetoes:
        return _hard_veto_label(hard_vetoes[0])
    invalid_conditions = [str(item) for item in policy.get("invalid_conditions") or [] if str(item).strip()]
    if invalid_conditions:
        return _compact_text(invalid_conditions[0], 48)
    return "无明确硬否决"


def _hard_veto_label(value: str) -> str:
    return {
        "market_gate_block": "大盘阻断",
        "high_bias_no_chase": "MA5乖离过大",
        "boll_overstretch_no_chase": "偏离BOLL上轨",
        "high_volume_prior_red_break": "放量破红K低点",
        "black_k_break_ma5_reduce": "黑K放量破MA5",
        "below_ma20": "跌破MA20",
        "volume_overheated": "量能过热",
    }.get(value, value)


def _stock_specific_reasons(reasons: List[str]) -> List[str]:
    common_prefixes = ("大盘摘要", "大盘环境", "缺少大盘")
    return [reason for reason in reasons if not reason.startswith(common_prefixes)]


def _common_market_reasons(picks: List[WolfDailyPick]) -> List[str]:
    common: List[str] = []
    for pick in picks:
        reasons = [str(item) for item in pick.policy.get("reasons") or [] if str(item).strip()]
        for reason in reasons:
            if reason.startswith(("大盘摘要", "大盘环境", "缺少大盘")) and reason not in common:
                common.append(reason)
    return common


def _humanize_wolf_note(note: str) -> str:
    text = str(note or "").strip()
    if text.startswith("Wolf whitelist file not found:"):
        path = text.split(":", 1)[1].strip()
        return f"白名单文件不存在：{path}。如果不想提交文件，请在 GitHub Variables/Secrets 配置 WOLF_DAILY_WHITELIST_CONTENT 或 WOLF_DAILY_WHITELIST_CONTENT_B64。"
    if text == "wolf_hot_sector_performance_empty":
        return "近60日强势板块数据为空，本次无法按强势板块筛白名单。"
    return text


def _escape_table_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


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
