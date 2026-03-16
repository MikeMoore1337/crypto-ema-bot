"""
multi_backtest.py - Массовый бэктест стратегии по нескольким символам и окнам.

Запуск:
    python multi_backtest.py

Что делает:
- гоняет один и тот же набор параметров стратегии
- по BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, TRXUSDT
- на двух окнах:
    1) последние N дней
    2) предыдущие N дней
- выводит итоговую таблицу
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

from backtest import Backtester
from bot import load_full_history
from config import Config
from exchange import BybitExchange
from logger import get_logger

log = get_logger("multi-backtest")


@dataclass
class SymbolBacktestResult:
    symbol: str
    window_name: str
    start_ts: str
    end_ts: str
    total_return_pct: float
    profit_factor: float
    max_drawdown_pct: float
    total_trades: int
    win_rate_pct: float
    sharpe_ratio: float
    final_balance: float


def shift_df_window(df: pd.DataFrame, candles_needed: int, window_index: int) -> pd.DataFrame:
    """
    window_index:
      0 - последние candles_needed свечей
      1 - предыдущие candles_needed свечей
    """
    if len(df) < candles_needed * (window_index + 1):
        return pd.DataFrame()

    end_idx = len(df) - candles_needed * window_index
    start_idx = end_idx - candles_needed

    return df.iloc[start_idx:end_idx].copy().reset_index(drop=True)


def load_symbol_history(
    exchange: BybitExchange,
    symbol: str,
    interval: str,
    needed_candles: int,
    multiplier: int = 3,
) -> pd.DataFrame:
    total_needed = needed_candles * multiplier
    return load_full_history(
        exchange=exchange,
        symbol=symbol,
        interval=interval,
        needed_candles=total_needed,
    )


def load_symbol_htf_history(
    exchange: BybitExchange,
    symbol: str,
    needed_hours: int,
    multiplier: int = 3,
) -> pd.DataFrame:
    total_needed = needed_hours * multiplier
    return load_full_history(
        exchange=exchange,
        symbol=symbol,
        interval="60",
        needed_candles=total_needed,
    )


def align_htf_window(htf_df: pd.DataFrame, ltf_window: pd.DataFrame) -> pd.DataFrame:
    if htf_df.empty or ltf_window.empty:
        return pd.DataFrame()

    start_ts = ltf_window["timestamp"].iloc[0]
    end_ts = ltf_window["timestamp"].iloc[-1]

    warmup_hours = Config.strategy.htf_ema_period + 10
    aligned = htf_df[htf_df["timestamp"] <= end_ts].copy()

    if aligned.empty:
        return pd.DataFrame()

    before_start = aligned[aligned["timestamp"] < start_ts]
    inside = aligned[(aligned["timestamp"] >= start_ts) & (aligned["timestamp"] <= end_ts)]

    warmup_tail = before_start.tail(warmup_hours)
    result = pd.concat([warmup_tail, inside]).drop_duplicates("timestamp").reset_index(drop=True)
    return result


def run_single_backtest(
    exchange: BybitExchange,
    symbol: str,
    window_index: int,
    window_name: str,
) -> Optional[SymbolBacktestResult]:
    cfg = Config.trading
    bt_cfg = Config.backtest
    backtester = Backtester(symbol=symbol)

    candles_per_day = (24 * 60) // int(cfg.interval)
    needed_candles = candles_per_day * bt_cfg.days

    ltf_history = load_symbol_history(
        exchange=exchange,
        symbol=symbol,
        interval=cfg.interval,
        needed_candles=needed_candles,
        multiplier=3,
    )

    if ltf_history.empty:
        log.warning(f"{symbol}: не удалось загрузить LTF историю")
        return None

    htf_needed = (bt_cfg.days + 10) * 24
    htf_history = load_symbol_htf_history(
        exchange=exchange,
        symbol=symbol,
        needed_hours=htf_needed,
        multiplier=3,
    )

    ltf_window = shift_df_window(ltf_history, needed_candles, window_index)
    if ltf_window.empty:
        log.warning(f"{symbol}: недостаточно данных для окна {window_name}")
        return None

    htf_window = pd.DataFrame()
    if not htf_history.empty:
        htf_window = align_htf_window(htf_history, ltf_window)

    log.info(
        f"{symbol} | {window_name} | "
        f"{ltf_window['timestamp'].iloc[0]} → {ltf_window['timestamp'].iloc[-1]}"
    )

    report = backtester.run(ltf_window, htf_window if not htf_window.empty else None)

    return SymbolBacktestResult(
        symbol=symbol,
        window_name=window_name,
        start_ts=str(ltf_window["timestamp"].iloc[0]),
        end_ts=str(ltf_window["timestamp"].iloc[-1]),
        total_return_pct=report.total_return_pct,
        profit_factor=report.profit_factor,
        max_drawdown_pct=report.max_drawdown_pct,
        total_trades=report.total_trades,
        win_rate_pct=report.win_rate_pct,
        sharpe_ratio=report.sharpe_ratio,
        final_balance=report.final_balance,
    )


def print_summary(df: pd.DataFrame) -> None:
    if df.empty:
        print("Нет результатов")
        return

    print("\n" + "═" * 150)
    print("МУЛЬТИ-БЭКТЕСТ ПО СИМВОЛАМ")
    print("═" * 150)
    print(
        f"{'Symbol':<10} "
        f"{'Window':<15} "
        f"{'Return %':<10} "
        f"{'PF':<8} "
        f"{'DD %':<8} "
        f"{'Trades':<8} "
        f"{'WR %':<8} "
        f"{'Sharpe':<8} "
        f"{'Start':<20} "
        f"{'End':<20}"
    )
    print("─" * 150)

    for _, row in df.iterrows():
        print(
            f"{row['symbol']:<10} "
            f"{row['window_name']:<15} "
            f"{row['total_return_pct']:<10.2f} "
            f"{row['profit_factor']:<8.2f} "
            f"{row['max_drawdown_pct']:<8.2f} "
            f"{int(row['total_trades']):<8} "
            f"{row['win_rate_pct']:<8.1f} "
            f"{row['sharpe_ratio']:<8.2f} "
            f"{row['start_ts']:<20} "
            f"{row['end_ts']:<20}"
        )

    print("═" * 150)

    grouped = df.groupby("symbol").agg({
        "total_return_pct": "mean",
        "profit_factor": "mean",
        "max_drawdown_pct": "mean",
        "total_trades": "sum",
        "win_rate_pct": "mean",
        "sharpe_ratio": "mean",
    }).reset_index()

    print("\n" + "═" * 90)
    print("СРЕДНИЕ ПО СИМВОЛАМ")
    print("═" * 90)
    print(
        f"{'Symbol':<10} "
        f"{'Avg Return':<12} "
        f"{'Avg PF':<10} "
        f"{'Avg DD':<10} "
        f"{'Trades Sum':<12} "
        f"{'Avg WR':<10} "
        f"{'Avg Sharpe':<10}"
    )
    print("─" * 90)

    for _, row in grouped.iterrows():
        print(
            f"{row['symbol']:<10} "
            f"{row['total_return_pct']:<12.2f} "
            f"{row['profit_factor']:<10.2f} "
            f"{row['max_drawdown_pct']:<10.2f} "
            f"{int(row['total_trades']):<12} "
            f"{row['win_rate_pct']:<10.1f} "
            f"{row['sharpe_ratio']:<10.2f}"
        )

    print("═" * 90)


def print_active_params() -> None:
    """
    Показать активные параметры для каждого символа перед запуском.
    Помогает быстро диагностировать почему символ даёт 0 сделок.
    """
    from strategy import EMAStrategy

    symbols_to_check = list(Config.symbol_overrides.keys()) or ["BTCUSDT"]
    print("\n" + "─" * 75)
    print("АКТИВНЫЕ ПАРАМЕТРЫ СТРАТЕГИИ ПО СИМВОЛАМ")
    print("─" * 75)
    print(
        f"{'Symbol':<12} "
        f"{'spread':<10} "
        f"{'atr_pct':<10} "
        f"{'trailing':<10} "
        f"{'breakeven':<10} "
        f"{'rsi L/S':<10}"
    )
    print("─" * 75)
    for sym in symbols_to_check:
        s = EMAStrategy(symbol=sym)
        print(
            f"{sym:<12} "
            f"{s.min_ema_spread_pct:<10.4f} "
            f"{s.min_atr_pct:<10.4f} "
            f"{s.atr_trailing_mult:<10.1f} "
            f"{Config.get_symbol_param(sym, 'atr_breakeven_trigger'):<10.1f} "
            f"{s.long_rsi_limit:.0f}/{s.short_rsi_limit:.0f}"
        )
    print("─" * 75 + "\n")


def main() -> None:
    symbols = ["BTCUSDT", "ETHUSDT"]
    windows = [
        (0, "last_90d"),
        (1, "prev_90d"),
    ]

    # Показываем параметры до запуска — сразу видно если что-то не так
    print_active_params()

    exchange = BybitExchange()
    results: list[dict] = []

    for symbol in symbols:
        for window_index, window_name in windows:
            result = run_single_backtest(
                exchange=exchange,
                symbol=symbol,
                window_index=window_index,
                window_name=window_name,
            )
            if result is not None:
                # Предупреждение если символ дал 0 сделок — помогает диагностировать фильтры
                if result.total_trades == 0:
                    log.warning(
                        f"⚠️  {symbol} | {window_name}: 0 сделок — "
                        f"проверь min_atr_pct={Config.get_symbol_param(symbol, 'min_atr_pct'):.4f} "
                        f"и min_ema_spread_pct={Config.get_symbol_param(symbol, 'min_ema_spread_pct'):.4f}"
                    )
                results.append(asdict(result))

    df = pd.DataFrame(results)
    if df.empty:
        print("Не удалось получить результаты")
        return

    # Фильтруем строки с 0 сделок из итоговой таблицы — они только мусорят
    df_display = df[df["total_trades"] > 0].copy()
    print_summary(df_display)

    # В CSV сохраняем все, включая нулевые — для отладки
    out_file = "multi_backtest_results.csv"
    df.to_csv(out_file, index=False)
    print(f"\nРезультаты сохранены в: {out_file}")


if __name__ == "__main__":
    main()