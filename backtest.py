"""
backtest.py - Движок бэктестинга на исторических данных.
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from config import Config
from logger import get_logger
from strategy import EMAStrategy, Signal

log = get_logger("backtest")


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    exit_reason: str


@dataclass
class BacktestReport:
    initial_balance: float
    final_balance: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trades: list = field(default_factory=list)

    def print(self) -> None:
        print("\n" + "═" * 55)
        print("           📊 РЕЗУЛЬТАТЫ БЭКТЕСТИНГА")
        print("═" * 55)
        print(f"  Начальный баланс:    ${self.initial_balance:.2f}")
        print(f"  Конечный баланс:     ${self.final_balance:.2f}")
        print(f"  Общая доходность:    {self.total_return_pct:+.2f}%")
        print("─" * 55)
        print(f"  Всего сделок:        {self.total_trades}")
        print(f"  Прибыльных:          {self.winning_trades} ({self.win_rate_pct:.1f}%)")
        print(f"  Убыточных:           {self.losing_trades}")
        print("─" * 55)
        print(f"  Средний выигрыш:     {self.avg_win_pct:+.2f}%")
        print(f"  Средний проигрыш:    {self.avg_loss_pct:+.2f}%")
        print(f"  Profit Factor:       {self.profit_factor:.2f}  (цель > 1.5)")
        print(f"  Макс. просадка:      {self.max_drawdown_pct:.2f}%  (цель < 20%)")
        print(f"  Sharpe Ratio:        {self.sharpe_ratio:.2f}  (цель > 1.0)")
        print("═" * 55)

        strong = (
                self.total_return_pct > 5
                and self.profit_factor > 1.5
                and self.max_drawdown_pct < 10
                and self.total_trades >= 20
        )

        acceptable = (
                self.total_return_pct > 0
                and self.profit_factor > 1.2
                and self.max_drawdown_pct < 15
                and self.total_trades >= 10
        )

        if strong:
            print("  ✅ Стратегия выглядит сильной")
            print("  ➡️  Дальше: multi-symbol + paper trading 2-4 недели")
        elif acceptable:
            print("  ⚠️  Есть edge, но нужна дополнительная проверка")
            print("  ➡️  Дальше: ETH/SOL/XRP + другой период + paper")
        else:
            print("  ❌ Результат слабый - пока не использовать в live")
        print("═" * 55 + "\n")


class Backtester:
    def __init__(self, symbol: str = ""):
        self.cfg = Config.backtest
        self.risk_cfg = Config.risk
        self.trade_cfg = Config.trading
        self.symbol = symbol or Config.trading.symbol
        # Стратегия создаётся с символом — применяет per-symbol параметры
        self.strategy = EMAStrategy(symbol=self.symbol)

    def _get_param(self, param: str):
        """Получить параметр с учётом per-symbol override."""
        return Config.get_symbol_param(self.symbol, param)

    def _update_trailing_stop(self, position: dict, row: pd.Series) -> None:
        if not Config.strategy.use_atr_trailing_stop:
            return

        atr = float(row.get("atr", 0))
        if atr <= 0:
            return

        mult = self._get_param("atr_trailing_mult")

        if position["side"] == "LONG":
            position["highest_close"] = max(position["highest_close"], float(row["close"]))
            candidate = position["highest_close"] - atr * mult

            if position["trailing_stop"] is None:
                position["trailing_stop"] = candidate
            else:
                position["trailing_stop"] = max(position["trailing_stop"], candidate)

        else:
            position["lowest_close"] = min(position["lowest_close"], float(row["close"]))
            candidate = position["lowest_close"] + atr * mult

            if position["trailing_stop"] is None:
                position["trailing_stop"] = candidate
            else:
                position["trailing_stop"] = min(position["trailing_stop"], candidate)

    def _update_breakeven_stop(self, position: dict, row: pd.Series) -> None:
        """
        Перенести стоп в безубыток после прохождения N×ATR в нашу сторону.

        Логика:
          - Смотрим сколько ATR прошла цена от точки входа
          - Если >= atr_breakeven_trigger → поднимаем floor стопа до entry_price
          - Это не отменяет trailing stop — просто не даём уйти в минус
            после того как позиция уже показала движение в нашу сторону

        Эффект для ETH: часть из 52 маленьких убытков (-1.5%) становится 0%.
        """
        if not self._get_param("use_breakeven_stop"):
            return
        if position.get("breakeven_set"):
            return  # уже установлен, не трогаем

        atr = float(row.get("atr", 0))
        if atr <= 0:
            return

        trigger_distance = atr * self._get_param("atr_breakeven_trigger")
        entry = position["entry_price"]
        price = float(row["close"])

        if position["side"] == "LONG":
            if price - entry >= trigger_distance:
                # Поднимаем floor стопа до entry (но не выше trailing)
                current_sl = position["stop_loss"]
                position["stop_loss"] = max(current_sl, entry)
                position["breakeven_set"] = True
                log.debug(
                    f"Breakeven установлен: entry={entry:.2f} "
                    f"price={price:.2f} (+{price - entry:.2f} >= {trigger_distance:.2f})"
                )
        else:
            if entry - price >= trigger_distance:
                current_sl = position["stop_loss"]
                position["stop_loss"] = min(current_sl, entry)
                position["breakeven_set"] = True
                log.debug(
                    f"Breakeven установлен: entry={entry:.2f} "
                    f"price={price:.2f} (-{entry - price:.2f} >= {trigger_distance:.2f})"
                )

    def run(self, df: pd.DataFrame, htf_df: Optional[pd.DataFrame] = None) -> BacktestReport:
        log.info(
            f"Бэктест на {len(df)} свечах | "
            f"{df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}"
        )

        df = self.strategy.add_indicators(df)
        commission = self.cfg.commission_pct

        balance = self.cfg.initial_balance
        equity_curve = [balance]
        trades: list[Trade] = []

        position: Optional[dict] = None
        start = Config.strategy.min_candles

        for i in range(start, len(df)):
            row = df.iloc[i]
            price = float(row["close"])
            low = float(row["low"])
            high = float(row["high"])

            if position is not None:
                exit_price = None
                exit_reason = None

                if position["side"] == "LONG":
                    active_stop = position["stop_loss"]
                    if position["trailing_stop"] is not None:
                        active_stop = max(active_stop, position["trailing_stop"])

                    if low <= active_stop:
                        exit_price = active_stop
                        exit_reason = "ATR-Trail/SL"

                elif position["side"] == "SHORT":
                    active_stop = position["stop_loss"]
                    if position["trailing_stop"] is not None:
                        active_stop = min(active_stop, position["trailing_stop"])

                    if high >= active_stop:
                        exit_price = active_stop
                        exit_reason = "ATR-Trail/SL"

                if exit_price is not None:
                    pnl = self._calculate_pnl(position, exit_price, commission)
                    balance += pnl
                    trades.append(Trade(
                        entry_time=position["entry_time"],
                        exit_time=row["timestamp"],
                        side=position["side"],
                        entry_price=position["entry_price"],
                        exit_price=exit_price,
                        qty=position["qty"],
                        pnl=pnl,
                        pnl_pct=(pnl / (position["qty"] * position["entry_price"])) * 100,
                        exit_reason=exit_reason,
                    ))
                    equity_curve.append(balance)
                    position = None
                    continue

            current_side = position["side"] if position else None
            signal_window = df.iloc[max(0, i - Config.strategy.min_candles):i + 1]

            htf_window = None
            if htf_df is not None:
                current_ts = row["timestamp"]
                htf_window = htf_df[htf_df["timestamp"] <= current_ts].tail(
                    Config.strategy.htf_ema_period + 10
                )

            result = self.strategy.get_signal(signal_window, current_side, htf_window)

            if result.signal == Signal.CLOSE and position is not None:
                pnl = self._calculate_pnl(position, price, commission)
                balance += pnl
                trades.append(Trade(
                    entry_time=position["entry_time"],
                    exit_time=row["timestamp"],
                    side=position["side"],
                    entry_price=position["entry_price"],
                    exit_price=price,
                    qty=position["qty"],
                    pnl=pnl,
                    pnl_pct=(pnl / (position["qty"] * position["entry_price"])) * 100,
                    exit_reason="Signal",
                ))
                equity_curve.append(balance)
                position = None
                continue

            if position is None and result.signal in (Signal.LONG, Signal.SHORT):
                position_usdt = balance * self.trade_cfg.position_size_pct
                qty = position_usdt / price
                side = result.signal.value

                sl_dist = price * self.risk_cfg.stop_loss_pct
                if side == "LONG":
                    sl = price - sl_dist
                else:
                    sl = price + sl_dist

                position = {
                    "side": side,
                    "entry_price": price,
                    "entry_time": row["timestamp"],
                    "qty": qty,
                    "stop_loss": sl,
                    "trailing_stop": None,
                    "highest_close": price,
                    "lowest_close": price,
                    "breakeven_set": False,
                }
                continue

            if position is not None:
                self._update_breakeven_stop(position, row)
                self._update_trailing_stop(position, row)

        return self._build_report(trades, balance, equity_curve)

    def _calculate_pnl(self, position: dict, exit_price: float, commission: float) -> float:
        qty = position["qty"]
        entry = position["entry_price"]
        side = position["side"]

        if side == "LONG":
            raw_pnl = (exit_price - entry) * qty
        else:
            raw_pnl = (entry - exit_price) * qty

        total_commission = (entry * qty + exit_price * qty) * commission
        return raw_pnl - total_commission

    def _build_report(
            self,
            trades: list[Trade],
            final_balance: float,
            equity_curve: list[float],
    ) -> BacktestReport:
        initial = self.cfg.initial_balance

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]

        total_win = sum(t.pnl for t in wins) if wins else 0
        total_loss = abs(sum(t.pnl for t in losses)) if losses else 1

        profit_factor = total_win / total_loss if total_loss > 0 else float("inf")

        equity = pd.Series(equity_curve)
        rolling_max = equity.cummax()
        drawdown = (equity - rolling_max) / rolling_max * 100
        max_drawdown = abs(drawdown.min())

        if len(trades) > 1:
            returns = pd.Series([t.pnl_pct for t in trades])
            sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() > 0 else 0
        else:
            sharpe = 0

        return BacktestReport(
            initial_balance=initial,
            final_balance=final_balance,
            total_return_pct=((final_balance - initial) / initial) * 100,
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate_pct=(len(wins) / len(trades) * 100) if trades else 0,
            avg_win_pct=(sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0,
            avg_loss_pct=(sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0,
            profit_factor=profit_factor,
            max_drawdown_pct=max_drawdown,
            sharpe_ratio=sharpe,
            trades=trades,
        )
