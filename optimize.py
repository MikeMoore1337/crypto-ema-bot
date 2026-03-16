"""
optimize.py - Параллельная оптимизация параметров стратегии.

Запуск:
    python optimize.py

Что делает:
- загружает историю
- перебирает сетку параметров
- запускает бэктесты параллельно по CPU
- считает score
- выводит лучшие результаты
- сохраняет результаты в CSV

Важно:
- для optimize.py и его worker-процессов file logging отключён
- DEBUG-спам от strategy/backtest/exchange подавлен до WARNING/INFO
"""

from __future__ import annotations

import os
import time

# ВАЖНО:
# отключаем file logging ДО импорта модулей, которые создают логгеры
os.environ["BOT_DISABLE_FILE_LOGGING"] = "1"

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from itertools import product
from multiprocessing import freeze_support
from typing import Any

import logging
import pandas as pd

from backtest import Backtester
from bot import load_full_history
from config import Config
from config_validation import validate_config
from exchange import BybitExchange
from logger import get_console_logger

log = get_console_logger("optimizer")


@dataclass
class OptimizationResult:
    long_rsi_limit: float
    short_rsi_limit: float
    min_ema_spread_pct: float
    slope_lookback: int
    stop_loss_pct: float
    take_profit_pct: float
    soft_htf_filter: bool
    require_price_above_slow_for_long: bool
    require_price_below_slow_for_short: bool
    atr_trailing_mult: float
    atr_breakeven_trigger: float

    total_return_pct: float
    profit_factor: float
    max_drawdown_pct: float
    total_trades: int
    win_rate_pct: float
    sharpe_ratio: float
    final_balance: float

    score: float


def configure_optimize_log_levels() -> None:
    """
    Убираем лишний DEBUG-спам при оптимизации.
    """
    logging.getLogger("strategy").setLevel(logging.CRITICAL)
    logging.getLogger("backtest").setLevel(logging.CRITICAL)
    logging.getLogger("exchange").setLevel(logging.CRITICAL)
    logging.getLogger("optimizer").setLevel(logging.INFO)


def snapshot_config() -> dict[str, Any]:
    return {
        "long_rsi_limit": Config.strategy.long_rsi_limit,
        "short_rsi_limit": Config.strategy.short_rsi_limit,
        "min_ema_spread_pct": Config.strategy.min_ema_spread_pct,
        "slope_lookback": Config.strategy.slope_lookback,
        "stop_loss_pct": Config.risk.stop_loss_pct,
        "take_profit_pct": Config.risk.take_profit_pct,
        "soft_htf_filter": Config.strategy.soft_htf_filter,
        "require_price_above_slow_for_long": Config.strategy.require_price_above_slow_for_long,
        "require_price_below_slow_for_short": Config.strategy.require_price_below_slow_for_short,
        "atr_trailing_mult": Config.strategy.atr_trailing_mult,
        "atr_breakeven_trigger": Config.strategy.atr_breakeven_trigger,
    }


def restore_config(snapshot: dict[str, Any]) -> None:
    Config.strategy.long_rsi_limit = snapshot["long_rsi_limit"]
    Config.strategy.short_rsi_limit = snapshot["short_rsi_limit"]
    Config.strategy.min_ema_spread_pct = snapshot["min_ema_spread_pct"]
    Config.strategy.slope_lookback = snapshot["slope_lookback"]
    Config.risk.stop_loss_pct = snapshot["stop_loss_pct"]
    Config.risk.take_profit_pct = snapshot["take_profit_pct"]
    Config.strategy.soft_htf_filter = snapshot["soft_htf_filter"]
    Config.strategy.require_price_above_slow_for_long = snapshot["require_price_above_slow_for_long"]
    Config.strategy.require_price_below_slow_for_short = snapshot["require_price_below_slow_for_short"]
    Config.strategy.atr_trailing_mult = snapshot["atr_trailing_mult"]
    Config.strategy.atr_breakeven_trigger = snapshot["atr_breakeven_trigger"]


def set_config_values(
        *,
        long_rsi_limit: float,
        short_rsi_limit: float,
        min_ema_spread_pct: float,
        slope_lookback: int,
        stop_loss_pct: float,
        take_profit_pct: float,
        soft_htf_filter: bool,
        require_price_above_slow_for_long: bool,
        require_price_below_slow_for_short: bool,
        atr_trailing_mult: float,
        atr_breakeven_trigger: float,
) -> None:
    Config.strategy.long_rsi_limit = long_rsi_limit
    Config.strategy.short_rsi_limit = short_rsi_limit
    Config.strategy.min_ema_spread_pct = min_ema_spread_pct
    Config.strategy.slope_lookback = slope_lookback
    Config.risk.stop_loss_pct = stop_loss_pct
    Config.risk.take_profit_pct = take_profit_pct
    Config.strategy.soft_htf_filter = soft_htf_filter
    Config.strategy.require_price_above_slow_for_long = require_price_above_slow_for_long
    Config.strategy.require_price_below_slow_for_short = require_price_below_slow_for_short
    Config.strategy.atr_trailing_mult = atr_trailing_mult
    Config.strategy.atr_breakeven_trigger = atr_breakeven_trigger


def load_data() -> tuple[pd.DataFrame, pd.DataFrame | None]:
    exchange = BybitExchange()
    cfg = Config.trading
    bt_cfg = Config.backtest

    candles_per_day = (24 * 60) // int(cfg.interval)
    needed_candles = candles_per_day * bt_cfg.days

    log.info(f"Загрузка {needed_candles} свечей ({bt_cfg.days} дней) [{cfg.interval}m]...")
    df = load_full_history(
        exchange=exchange,
        symbol=cfg.symbol,
        interval=cfg.interval,
        needed_candles=needed_candles,
    )

    if df.empty:
        raise RuntimeError("Не удалось загрузить исторические данные")

    log.info(f"Загружено {len(df)} свечей [{cfg.interval}m]")

    htf_days = bt_cfg.days + 10
    htf_needed = htf_days * 24

    log.info("Загрузка часовых свечей для HTF фильтра...")
    htf_df = load_full_history(
        exchange=exchange,
        symbol=cfg.symbol,
        interval="60",
        needed_candles=htf_needed,
    )

    if htf_df.empty:
        log.warning("HTF данные не загружены - будет fallback без полноценного HTF")
        htf_df = None
    else:
        log.info(f"Загружено {len(htf_df)} часовых свечей [1h]")

    return df, htf_df


def compute_score(
        *,
        total_return_pct: float,
        profit_factor: float,
        max_drawdown_pct: float,
        total_trades: int,
        sharpe_ratio: float,
) -> float:
    score = 0.0

    score += total_return_pct * 1.0
    score += (profit_factor - 1.0) * 10.0
    score -= max_drawdown_pct * 0.8
    score += sharpe_ratio * 1.0

    if total_trades < 5:
        score -= 50.0
    elif total_trades < 10:
        score -= 30.0
    elif total_trades < 15:
        score -= 15.0
    elif total_trades < 20:
        score -= 5.0
    else:
        score += 5.0

    return round(score, 4)


def build_param_grid() -> list[dict[str, Any]]:
    long_rsi_values = [68.0, 70.0, 72.0]
    short_rsi_values = [35.0, 33.0, 32.0]

    spread_values = [0.0006, 0.0005, 0.0004]
    slope_values = [3, 4, 5]

    stop_loss_values = [0.015, 0.018, 0.020]
    take_profit_values = [0.024, 0.027, 0.030]

    soft_htf_values = [True]
    price_filter_values = [True]

    # Новые параметры — ATR trailing и breakeven
    # atr_trailing_mult: чем больше, тем шире trailing → держим тренд дольше
    # atr_breakeven_trigger: когда переводим стоп в безубыток (в единицах ATR)
    trailing_mult_values = [2.0, 2.5, 3.0]
    breakeven_trigger_values = [0.8, 1.0, 1.5]

    grid: list[dict[str, Any]] = []

    for (
            long_rsi,
            short_rsi,
            spread,
            slope,
            sl,
            tp,
            soft_htf,
            price_filter,
            trailing_mult,
            breakeven_trigger,
    ) in product(
        long_rsi_values,
        short_rsi_values,
        spread_values,
        slope_values,
        stop_loss_values,
        take_profit_values,
        soft_htf_values,
        price_filter_values,
        trailing_mult_values,
        breakeven_trigger_values,
    ):
        if tp <= sl:
            continue
        if long_rsi <= short_rsi:
            continue

        grid.append({
            "long_rsi_limit": long_rsi,
            "short_rsi_limit": short_rsi,
            "min_ema_spread_pct": spread,
            "slope_lookback": slope,
            "stop_loss_pct": sl,
            "take_profit_pct": tp,
            "soft_htf_filter": soft_htf,
            "require_price_above_slow_for_long": price_filter,
            "require_price_below_slow_for_short": price_filter,
            "atr_trailing_mult": trailing_mult,
            "atr_breakeven_trigger": breakeven_trigger,
        })

    return grid


def evaluate_combination(
        params: dict[str, Any],
        df: pd.DataFrame,
        htf_df: pd.DataFrame | None,
) -> dict[str, Any]:
    """Worker-функция для одного набора параметров."""
    configure_optimize_log_levels()
    set_config_values(**params)

    backtester = Backtester(symbol=Config.trading.symbol)
    report = backtester.run(
        df.copy(),
        htf_df.copy() if htf_df is not None else None,
    )

    score = compute_score(
        total_return_pct=report.total_return_pct,
        profit_factor=report.profit_factor,
        max_drawdown_pct=report.max_drawdown_pct,
        total_trades=report.total_trades,
        sharpe_ratio=report.sharpe_ratio,
    )

    result = OptimizationResult(
        long_rsi_limit=params["long_rsi_limit"],
        short_rsi_limit=params["short_rsi_limit"],
        min_ema_spread_pct=params["min_ema_spread_pct"],
        slope_lookback=params["slope_lookback"],
        stop_loss_pct=params["stop_loss_pct"],
        take_profit_pct=params["take_profit_pct"],
        soft_htf_filter=params["soft_htf_filter"],
        require_price_above_slow_for_long=params["require_price_above_slow_for_long"],
        require_price_below_slow_for_short=params["require_price_below_slow_for_short"],
        atr_trailing_mult=params["atr_trailing_mult"],
        atr_breakeven_trigger=params["atr_breakeven_trigger"],
        total_return_pct=report.total_return_pct,
        profit_factor=report.profit_factor,
        max_drawdown_pct=report.max_drawdown_pct,
        total_trades=report.total_trades,
        win_rate_pct=report.win_rate_pct,
        sharpe_ratio=report.sharpe_ratio,
        final_balance=report.final_balance,
        score=score,
    )

    return asdict(result)


def run_optimization_parallel(max_workers: int | None = None) -> pd.DataFrame:
    original = snapshot_config()

    try:
        configure_optimize_log_levels()

        # Базовая проверка конфигурации перед длительным запуском оптимизатора
        issues = validate_config(mode="backtest")
        for issue in issues:
            if issue.level == "ERROR":
                log.error(f"CONFIG ERROR: {issue.message}")
            else:
                log.warning(f"CONFIG WARNING: {issue.message}")

        if any(i.level == "ERROR" for i in issues):
            raise RuntimeError("Конфигурация содержит ошибки, оптимизация прервана.")

        df, htf_df = load_data()
        param_grid = build_param_grid()

        log.info(f"Комбинаций к тесту: {len(param_grid)}")

        if max_workers is None:
            cpu_count = os.cpu_count() or 2
            max_workers = max(1, cpu_count - 1)

        log.info(f"Параллельных процессов: {max_workers}")

        results: list[dict[str, Any]] = []
        completed = 0
        total = len(param_grid)
        started_at = time.time()

        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(evaluate_combination, params, df, htf_df)
                for params in param_grid
            ]

            for future in as_completed(futures):
                completed += 1

                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    log.error(f"Ошибка в worker-процессе: {e}")
                    continue

                if completed % 5 == 0 or completed == total:
                    elapsed = time.time() - started_at
                    rate = completed / elapsed if elapsed > 0 else 0
                    remaining = total - completed
                    eta_sec = remaining / rate if rate > 0 else 0

                    log.info(
                        f"Готово: {completed}/{total} | "
                        f"{completed / total * 100:.1f}% | "
                        f"ETA: {eta_sec / 60:.1f} мин"
                    )

        result_df = pd.DataFrame(results)

        if result_df.empty:
            raise RuntimeError("Не удалось получить результаты оптимизации")

        filtered = result_df[
            (result_df["profit_factor"] > 0.95) &
            (result_df["max_drawdown_pct"] < 20.0) &
            (result_df["total_trades"] >= 15)
            ].copy()

        if filtered.empty:
            log.warning("После фильтра не осталось результатов - показываю все.")
            filtered = result_df.copy()

        filtered = filtered.sort_values(
            by=["score", "total_return_pct", "profit_factor", "max_drawdown_pct"],
            ascending=[False, False, False, True],
        ).reset_index(drop=True)

        return filtered

    finally:
        restore_config(original)


def print_top_results(df: pd.DataFrame, top_n: int = 15) -> None:
    if df.empty:
        print("Нет результатов")
        return

    top = df.head(top_n)

    print("\n" + "═" * 180)
    print("ТОП РЕЗУЛЬТАТОВ ОПТИМИЗАЦИИ")
    print("═" * 180)
    print(
        f"{'№':<3} "
        f"{'Score':<8} "
        f"{'RSI L/S':<12} "
        f"{'Spread':<10} "
        f"{'Slope':<7} "
        f"{'SL':<8} "
        f"{'Trail':<7} "
        f"{'BE':<6} "
        f"{'Return %':<10} "
        f"{'PF':<8} "
        f"{'DD %':<8} "
        f"{'Trades':<8} "
        f"{'WR %':<8} "
        f"{'Sharpe':<8}"
    )
    print("─" * 180)

    for i, row in top.iterrows():
        print(
            f"{i + 1:<3} "
            f"{row['score']:<8.2f} "
            f"{row['long_rsi_limit']:.0f}/{row['short_rsi_limit']:.0f}{'':<5} "
            f"{row['min_ema_spread_pct']:<10.4f} "
            f"{int(row['slope_lookback']):<7} "
            f"{row['stop_loss_pct']:<8.3f} "
            f"{row['atr_trailing_mult']:<7.1f} "
            f"{row['atr_breakeven_trigger']:<6.1f} "
            f"{row['total_return_pct']:<10.2f} "
            f"{row['profit_factor']:<8.2f} "
            f"{row['max_drawdown_pct']:<8.2f} "
            f"{int(row['total_trades']):<8} "
            f"{row['win_rate_pct']:<8.1f} "
            f"{row['sharpe_ratio']:<8.2f}"
        )

    print("═" * 180)

    best = top.iloc[0]
    print("\nЛУЧШАЯ КОНФИГУРАЦИЯ:")
    print(f"  long_rsi_limit                     = {best['long_rsi_limit']}")
    print(f"  short_rsi_limit                    = {best['short_rsi_limit']}")
    print(f"  min_ema_spread_pct                 = {best['min_ema_spread_pct']}")
    print(f"  slope_lookback                     = {int(best['slope_lookback'])}")
    print(f"  stop_loss_pct                      = {best['stop_loss_pct']}")
    print(f"  take_profit_pct                    = {best['take_profit_pct']}")
    print(f"  soft_htf_filter                    = {bool(best['soft_htf_filter'])}")
    print(f"  require_price_above_slow_for_long  = {bool(best['require_price_above_slow_for_long'])}")
    print(f"  require_price_below_slow_for_short = {bool(best['require_price_below_slow_for_short'])}")
    print(f"  atr_trailing_mult                  = {best['atr_trailing_mult']}")
    print(f"  atr_breakeven_trigger              = {best['atr_breakeven_trigger']}")
    print(f"  total_return_pct                   = {best['total_return_pct']:.2f}%")
    print(f"  profit_factor                      = {best['profit_factor']:.2f}")
    print(f"  max_drawdown_pct                   = {best['max_drawdown_pct']:.2f}%")
    print(f"  total_trades                       = {int(best['total_trades'])}")
    print(f"  win_rate_pct                       = {best['win_rate_pct']:.1f}%")
    print(f"  sharpe_ratio                       = {best['sharpe_ratio']:.2f}")
    print(f"  score                              = {best['score']:.2f}")


def save_results(df: pd.DataFrame) -> None:
    all_file = "optimization_results_full.csv"
    top_file = "optimization_results_top20.csv"

    df.to_csv(all_file, index=False)
    df.head(20).to_csv(top_file, index=False)

    print(f"\nВсе результаты сохранены в: {all_file}")
    print(f"Топ-20 результатов сохранены в: {top_file}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Strategy optimizer")
    parser.add_argument("--workers", type=int, default=6, help="Количество процессов")
    args = parser.parse_args()

    df = run_optimization_parallel(max_workers=args.workers)
    print_top_results(df, top_n=15)
    save_results(df)


if __name__ == "__main__":
    freeze_support()
    main()
