"""
config_validation.py - Проверка настроек перед запуском бота.

Цель:
- поймать опасные или бессмысленные конфигурации ещё до старта торговли;
- упростить поддержку: все ключевые проверки лежат в одном месте.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import Config


@dataclass
class ValidationIssue:
    level: str  # "ERROR" или "WARNING"
    message: str


def validate_config(*, mode: str) -> list[ValidationIssue]:
    """
    Проверить консистентность и безопасность основных настроек.

    Args:
        mode: "live", "paper" или "backtest"
    """
    issues: list[ValidationIssue] = []

    exch = Config.exchange
    trading = Config.trading
    risk = Config.risk
    strat = Config.strategy
    bt = Config.backtest

    # --- Базовые проверки ---
    if not trading.symbol:
        issues.append(ValidationIssue("ERROR", "TradingConfig.symbol не задан"))

    try:
        int(trading.interval)
    except (TypeError, ValueError):
        issues.append(
            ValidationIssue(
                "ERROR",
                f"TradingConfig.interval должен быть числом в минутах, сейчас: {trading.interval!r}",
            )
        )

    if trading.position_size_pct <= 0 or trading.position_size_pct > 1:
        issues.append(
            ValidationIssue(
                "ERROR",
                "TradingConfig.position_size_pct должен быть в диапазоне (0, 1]",
            )
        )

    if risk.stop_loss_pct <= 0:
        issues.append(ValidationIssue("ERROR", "RiskConfig.stop_loss_pct должен быть > 0"))
    if risk.take_profit_pct <= 0:
        issues.append(ValidationIssue("ERROR", "RiskConfig.take_profit_pct должен быть > 0"))
    if risk.max_daily_loss_pct <= 0 or risk.max_daily_loss_pct > 1:
        issues.append(
            ValidationIssue(
                "ERROR",
                "RiskConfig.max_daily_loss_pct должен быть в диапазоне (0, 1]",
            )
        )

    if bt.initial_balance <= 0:
        issues.append(ValidationIssue("ERROR", "BacktestConfig.initial_balance должен быть > 0"))
    if bt.days <= 0:
        issues.append(ValidationIssue("ERROR", "BacktestConfig.days должен быть > 0"))

    # --- Связанные параметры стратегии ---
    if strat.fast_ema_period <= 0 or strat.slow_ema_period <= 0:
        issues.append(
            ValidationIssue(
                "ERROR",
                "StrategyConfig.fast_ema_period и slow_ema_period должны быть > 0",
            )
        )
    if strat.fast_ema_period >= strat.slow_ema_period:
        issues.append(
            ValidationIssue(
                "WARNING",
                "fast_ema_period >= slow_ema_period — пересечение EMA может работать некорректно",
            )
        )
    if strat.rsi_overbought <= strat.rsi_oversold:
        issues.append(
            ValidationIssue(
                "WARNING",
                "rsi_overbought <= rsi_oversold — RSI-фильтр не имеет смысла",
            )
        )

    # ATR trailing
    if strat.use_atr_trailing_stop and strat.atr_trailing_mult <= 0:
        issues.append(
            ValidationIssue("ERROR", "atr_trailing_mult должен быть > 0")
        )
    if strat.use_atr_trailing_stop and strat.atr_trailing_mult < 1.0:
        issues.append(
            ValidationIssue(
                "WARNING",
                f"atr_trailing_mult={strat.atr_trailing_mult} очень мал — "
                "trailing stop будет слишком плотным и вышибать позиции на шуме",
            )
        )

    # Breakeven
    if strat.use_breakeven_stop and strat.atr_breakeven_trigger <= 0:
        issues.append(
            ValidationIssue("ERROR", "atr_breakeven_trigger должен быть > 0")
        )
    if strat.use_breakeven_stop and strat.atr_breakeven_trigger > strat.atr_trailing_mult:
        issues.append(
            ValidationIssue(
                "WARNING",
                f"atr_breakeven_trigger ({strat.atr_breakeven_trigger}) > "
                f"atr_trailing_mult ({strat.atr_trailing_mult}) — "
                "breakeven никогда не сработает до trailing stop",
            )
        )

    # Per-symbol overrides
    from config import Config, SymbolOverride
    for sym, override in Config.symbol_overrides.items():
        if override.atr_trailing_mult is not None and override.atr_trailing_mult <= 0:
            issues.append(
                ValidationIssue("ERROR", f"symbol_overrides[{sym}].atr_trailing_mult должен быть > 0")
            )
        if override.atr_breakeven_trigger is not None and override.atr_breakeven_trigger <= 0:
            issues.append(
                ValidationIssue("ERROR", f"symbol_overrides[{sym}].atr_breakeven_trigger должен быть > 0")
            )

    # --- Режим LIVE: защита от глупых ошибок ---
    if mode == "live":
        if not exch.api_key or not exch.api_secret:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    "LIVE-режим: пустые BYBIT_API_KEY / BYBIT_API_SECRET. "
                    "Проверь .env или переменные окружения.",
                )
            )

        # Осознанная торговля на реальном рынке
        if exch.testnet:
            issues.append(
                ValidationIssue(
                    "WARNING",
                    "LIVE-режим, но ExchangeConfig.testnet=True — будет торговать на Testnet.",
                )
            )

    return issues
