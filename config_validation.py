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

