# -*- coding: utf-8 -*-
"""Daily AlphaSift recommendations for a user-maintained stock pool."""

from __future__ import annotations

import csv
import importlib
import inspect
import logging
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class AlphaSiftDailyPick:
    code: str
    name: str = ""
    score: Optional[float] = None
    screen_score: Optional[float] = None
    strategy: str = ""
    strategies: List[str] = field(default_factory=list)
    reason: str = ""
    risk_level: str = ""
    risk_flags: List[str] = field(default_factory=list)
    llm_thesis: str = ""
    llm_catalysts: List[str] = field(default_factory=list)
    llm_risks: List[str] = field(default_factory=list)
    price: Optional[float] = None
    change_pct: Optional[float] = None
    industry: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AlphaSiftDailyPoolResult:
    enabled: bool
    pool_file: str = ""
    pool_count: int = 0
    top_n: int = 0
    strategies: List[str] = field(default_factory=list)
    strategy_runs: List[Dict[str, Any]] = field(default_factory=list)
    picks: List[AlphaSiftDailyPick] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source_errors: List[str] = field(default_factory=list)

    @property
    def candidate_count(self) -> int:
        return len(self.picks)


_SNAPSHOT_PATCH_LOCK = threading.RLock()


class AlphaSiftDailyPoolService:
    """Run AlphaSift against a DSA-owned pool file without DSA deep analysis."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def run(self) -> AlphaSiftDailyPoolResult:
        if not bool(getattr(self.config, "alphasift_daily_notify", False)):
            return AlphaSiftDailyPoolResult(enabled=False)
        if not bool(getattr(self.config, "alphasift_enabled", False)):
            return AlphaSiftDailyPoolResult(
                enabled=False,
                warnings=["AlphaSift daily pool skipped because ALPHASIFT_ENABLED=false"],
            )

        pool_file = str(getattr(self.config, "alphasift_daily_pool_file", "") or "").strip()
        strategies = _resolve_daily_strategies(self.config)
        top_n = max(1, int(getattr(self.config, "alphasift_daily_top_n", 5) or 5))
        codes = load_pool_codes(pool_file)
        result = AlphaSiftDailyPoolResult(
            enabled=True,
            pool_file=pool_file,
            pool_count=len(codes),
            top_n=top_n,
            strategies=strategies,
        )

        if not codes:
            result.warnings.append(f"AlphaSift daily pool file has no valid stock codes: {pool_file}")
            return result
        if not strategies:
            result.warnings.append("AlphaSift daily pool has no configured strategies")
            return result

        alphasift_service = _alphasift_service()
        try:
            alphasift_service._ensure_alphasift_available_for_use()
        except Exception as exc:
            result.source_errors.append(f"AlphaSift unavailable: {exc}")
            return result

        use_llm = bool(getattr(self.config, "alphasift_daily_use_llm", True))
        merged: Dict[str, AlphaSiftDailyPick] = {}
        ordered_codes: List[str] = []
        for strategy in strategies:
            try:
                raw_run = self._screen_strategy(
                    strategy=strategy,
                    codes=codes,
                    top_n=top_n,
                    use_llm=use_llm,
                )
            except Exception as exc:
                logger.warning("AlphaSift daily pool strategy %s failed: %s", strategy, exc)
                result.source_errors.append(f"{strategy}: {exc}")
                continue

            candidates = alphasift_service._normalize_candidates(raw_run)
            selected = candidates[:top_n]
            result.strategy_runs.append({
                "strategy": raw_run.get("strategy") or strategy,
                "run_id": raw_run.get("run_id"),
                "candidate_count": len(selected),
                "snapshot_count": raw_run.get("snapshot_count"),
                "after_filter_count": raw_run.get("after_filter_count"),
                "llm_ranked": raw_run.get("llm_ranked"),
                "warnings": alphasift_service._list_text_values(raw_run.get("warnings")),
                "source_errors": alphasift_service._list_text_values(raw_run.get("source_errors")),
            })
            result.warnings.extend(
                f"{strategy}: {item}" for item in alphasift_service._list_text_values(raw_run.get("warnings"))
            )
            result.source_errors.extend(
                f"{strategy}: {item}" for item in alphasift_service._list_text_values(raw_run.get("source_errors"))
            )

            for candidate in selected:
                code = normalize_pool_code(candidate.get("code"))
                if not code:
                    continue
                pick = _candidate_to_pick(candidate, strategy=strategy)
                existing = merged.get(code)
                if existing is None:
                    pick.strategies = [strategy]
                    merged[code] = pick
                    ordered_codes.append(code)
                    continue
                if strategy not in existing.strategies:
                    existing.strategies.append(strategy)
                if _pick_score(pick) > _pick_score(existing):
                    pick.strategies = existing.strategies
                    merged[code] = pick

        result.picks = sorted(
            (merged[code] for code in ordered_codes),
            key=lambda item: (len(item.strategies), _pick_score(item)),
            reverse=True,
        )
        return result

    def _screen_strategy(
        self,
        *,
        strategy: str,
        codes: List[str],
        top_n: int,
        use_llm: bool,
    ) -> Dict[str, Any]:
        pipeline = importlib.import_module("alphasift.pipeline")
        alphasift_service = _alphasift_service()
        screen = getattr(pipeline, "screen")
        with (
            _pool_snapshot_filter(codes),
            alphasift_service._alphasift_runtime_env(self.config, max_results=top_n),
            alphasift_service._alphasift_litellm_headers(self.config),
        ):
            raw = _call_screen(
                screen,
                strategy=strategy,
                market="cn",
                max_output=top_n,
                use_llm=use_llm,
                context={},
            )
        data = alphasift_service._to_plain(raw)
        if not isinstance(data, dict):
            data = {"candidates": data}
        return alphasift_service._remove_non_finite_json_values(data)


def format_daily_pool_markdown(result: AlphaSiftDailyPoolResult) -> str:
    if not result.enabled:
        return ""
    lines = [
        "# 🔎 AlphaSift 股票池入场候选",
        "",
        f"> 股票池 **{result.pool_count}** 只 | 策略 **{', '.join(result.strategies) or '-'}** | 每策略 Top {result.top_n} | 去重后 **{len(result.picks)}** 只",
        "",
    ]
    if result.picks:
        for index, pick in enumerate(result.picks, 1):
            score_text = _format_score(pick.score if pick.score is not None else pick.screen_score)
            strategy_text = ", ".join(pick.strategies or [pick.strategy])
            name_text = f"{pick.name}({pick.code})" if pick.name else pick.code
            price_parts = []
            if pick.price is not None:
                price_parts.append(f"现价 {pick.price:g}")
            if pick.change_pct is not None:
                price_parts.append(f"涨跌幅 {pick.change_pct:g}%")
            if pick.industry:
                price_parts.append(f"行业 {pick.industry}")
            lines.extend([
                f"{index}. **{name_text}** | 分数 {score_text} | 策略 {strategy_text}",
            ])
            if price_parts:
                lines.append(f"   - {' | '.join(price_parts)}")
            reason = pick.llm_thesis or pick.reason
            if reason:
                lines.append(f"   - 理由：{_compact_text(reason, 120)}")
            if pick.llm_catalysts:
                lines.append(f"   - 催化：{_compact_text('；'.join(pick.llm_catalysts[:2]), 120)}")
            risks = pick.llm_risks or pick.risk_flags
            if risks:
                lines.append(f"   - 风险：{_compact_text('；'.join(str(item) for item in risks[:2]), 120)}")
        lines.append("")
    else:
        lines.extend(["本次股票池未筛出符合条件的候选。", ""])

    if result.warnings or result.source_errors:
        notes = list(dict.fromkeys([*result.warnings, *result.source_errors]))
        if notes:
            lines.extend(["## 降级提示", ""])
            for note in notes[:6]:
                lines.append(f"- {_compact_text(note, 160)}")
            lines.append("")
    return "\n".join(lines).strip()


def load_pool_codes(pool_file: str) -> List[str]:
    path = Path(pool_file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        logger.warning("AlphaSift daily pool file not found: %s", path)
        return []
    except UnicodeDecodeError:
        content = path.read_text(encoding="gbk", errors="ignore")

    codes: List[str] = []
    for token in _pool_tokens(content):
        code = normalize_pool_code(token)
        if code:
            codes.append(code)
    return list(dict.fromkeys(codes))


def normalize_pool_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = text.replace("SH.", "SH").replace("SZ.", "SZ")
    text = re.sub(r"^(SH|SZ)", "", text)
    text = re.sub(r"\.(SH|SZ)$", "", text)
    match = re.search(r"\b\d{6}\b", text)
    if match:
        return match.group(0)
    return ""


def _pool_tokens(content: str) -> List[str]:
    tokens: List[str] = []
    try:
        sample = content[:4096]
        dialect = csv.Sniffer().sniff(sample)
        rows = csv.reader(content.splitlines(), dialect)
        for row in rows:
            tokens.extend(row)
    except Exception:
        tokens = re.split(r"[\s,;，；|]+", content)
    expanded: List[str] = []
    for token in tokens:
        expanded.extend(re.split(r"[\s,;，；|]+", str(token)))
    return expanded


def _resolve_daily_strategies(config: Config) -> List[str]:
    configured = list(getattr(config, "alphasift_daily_strategies", []) or [])
    if not configured:
        single = str(getattr(config, "alphasift_daily_strategy", "") or "").strip()
        configured = [single] if single else []
    return list(dict.fromkeys(item.strip() for item in configured if item and item.strip()))


@contextmanager
def _pool_snapshot_filter(codes: List[str]) -> Iterator[None]:
    pipeline = importlib.import_module("alphasift.pipeline")
    snapshot_module = importlib.import_module("alphasift.snapshot")
    original_pipeline_fetch = getattr(pipeline, "fetch_snapshot_with_fallback")
    original_snapshot_fetch = getattr(snapshot_module, "fetch_snapshot_with_fallback", None)
    allowed = {normalize_pool_code(code) for code in codes if normalize_pool_code(code)}

    def fetch_pool_snapshot(*args: Any, **kwargs: Any) -> Any:
        df = original_pipeline_fetch(*args, **kwargs)
        return _filter_snapshot_df(df, allowed)

    with _SNAPSHOT_PATCH_LOCK:
        setattr(pipeline, "fetch_snapshot_with_fallback", fetch_pool_snapshot)
        if callable(original_snapshot_fetch):
            setattr(snapshot_module, "fetch_snapshot_with_fallback", fetch_pool_snapshot)
        try:
            yield
        finally:
            setattr(pipeline, "fetch_snapshot_with_fallback", original_pipeline_fetch)
            if callable(original_snapshot_fetch):
                setattr(snapshot_module, "fetch_snapshot_with_fallback", original_snapshot_fetch)


def _filter_snapshot_df(df: Any, allowed: set[str]) -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty or not allowed:
        return df
    code_col = next((col for col in ("code", "symbol", "stock_code", "代码") if col in df.columns), None)
    if not code_col:
        return df
    mask = df[code_col].map(normalize_pool_code).isin(allowed)
    filtered = df.loc[mask].copy()
    filtered.attrs.update(getattr(df, "attrs", {}) or {})
    filtered.attrs["pool_filter_count"] = len(filtered)
    filtered.attrs["pool_filter_total"] = len(allowed)
    return filtered


def _call_screen(screen: Any, **kwargs: Any) -> Any:
    signature = inspect.signature(screen)
    params = signature.parameters
    call_kwargs = dict(kwargs)
    if "max_output" not in params and "max_results" in params:
        call_kwargs["max_results"] = call_kwargs.pop("max_output")
    if "context" not in params and not any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in params.values()
    ):
        call_kwargs.pop("context", None)
    return screen(call_kwargs.pop("strategy"), **call_kwargs)


def _candidate_to_pick(candidate: Dict[str, Any], *, strategy: str) -> AlphaSiftDailyPick:
    return AlphaSiftDailyPick(
        code=normalize_pool_code(candidate.get("code")),
        name=str(candidate.get("name") or ""),
        score=_safe_float(candidate.get("score")),
        screen_score=_safe_float(candidate.get("screen_score")),
        strategy=strategy,
        reason=str(candidate.get("reason") or ""),
        risk_level=str(candidate.get("risk_level") or ""),
        risk_flags=[str(item) for item in candidate.get("risk_flags") or []],
        llm_thesis=str(candidate.get("llm_thesis") or ""),
        llm_catalysts=[str(item) for item in candidate.get("llm_catalysts") or []],
        llm_risks=[str(item) for item in candidate.get("llm_risks") or []],
        price=_safe_float(candidate.get("price")),
        change_pct=_safe_float(candidate.get("change_pct")),
        industry=str(candidate.get("industry") or ""),
        raw=dict(candidate),
    )


def _pick_score(pick: AlphaSiftDailyPick) -> float:
    if pick.score is not None:
        return pick.score
    if pick.screen_score is not None:
        return pick.screen_score
    return 0.0


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_score(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _compact_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _alphasift_service() -> Any:
    from src.services import alphasift_service

    return alphasift_service
