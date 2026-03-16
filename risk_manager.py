"""
risk_manager.py - Управление рисками.
"""

from dataclasses import dataclass
from typing import Optional

from config import Config
from logger import get_logger

log = get_logger("risk")


@dataclass
class PositionParams:
    """Параметры для открытия позиции."""
    qty: float
    stop_loss: float
    take_profit: Optional[float]
    risk_usdt: float


class RiskManager:
    """
    Рассчитывает размер позиции и уровни SL/TP.
    Хранит дневную статистику и проверяет дневной лимит убытка.
    """

    def __init__(self):
        self.cfg = Config.risk
        self.trade_cfg = Config.trading

        self._daily_loss = 0.0
        self._daily_start_balance = 0.0
        self._current_balance = 0.0

        self._daily_stats = {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl_usdt": 0.0,
            "pnl_pct": 0.0,
        }

    @property
    def balance(self) -> float:
        """Текущий баланс, известный риск-менеджеру."""
        return self._current_balance

    @property
    def daily_stats(self) -> dict:
        """Дневная статистика для отчётов."""
        return self._daily_stats.copy()

    def reset_daily_stats(self, balance: float) -> None:
        """Сбросить дневную статистику (вызывать в начале дня)."""
        self._daily_loss = 0.0
        self._daily_start_balance = balance
        self._current_balance = balance

        self._daily_stats = {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl_usdt": 0.0,
            "pnl_pct": 0.0,
        }

        log.info(f"Дневная статистика сброшена. Баланс: ${balance:.2f}")

    def update_balance(self, balance: float) -> None:
        """Обновить текущий баланс."""
        self._current_balance = balance

    def record_trade_result(self, pnl: float, balance_after_trade: float | None = None) -> None:
        """
        Записать результат сделки для отслеживания дневного лимита и отчётов.

        Args:
            pnl: результат сделки в USDT
            balance_after_trade: баланс после сделки, если уже известен
        """
        self._daily_stats["trades"] += 1
        self._daily_stats["pnl_usdt"] += pnl

        if pnl > 0:
            self._daily_stats["wins"] += 1
        else:
            self._daily_stats["losses"] += 1

        if pnl < 0:
            self._daily_loss += abs(pnl)
            log.info(
                f"Зафиксирован убыток ${abs(pnl):.4f} | "
                f"Накопленный дневной убыток: ${self._daily_loss:.4f}"
            )

        if balance_after_trade is not None:
            self._current_balance = balance_after_trade

        if self._daily_start_balance > 0:
            self._daily_stats["pnl_pct"] = (
                self._daily_stats["pnl_usdt"] / self._daily_start_balance
            ) * 100
        else:
            self._daily_stats["pnl_pct"] = 0.0

    def can_trade(self, balance: float) -> tuple[bool, str]:
        """
        Проверить, можно ли открывать новую позицию.

        Returns:
            (True, "") если можно торговать
            (False, причина) если нельзя
        """
        self._current_balance = balance

        if balance <= 0:
            return False, "Нулевой или отрицательный баланс"

        if self._daily_start_balance > 0:
            daily_loss_pct = self._daily_loss / self._daily_start_balance
            if daily_loss_pct >= self.cfg.max_daily_loss_pct:
                return False, (
                    f"Достигнут дневной лимит потерь: "
                    f"{daily_loss_pct * 100:.1f}% >= {self.cfg.max_daily_loss_pct * 100:.1f}%"
                )

        return True, ""

    def calculate_position(
        self,
        balance: float,
        entry_price: float,
        side: str,
        min_qty: float = 0.001,
        qty_step: float = 0.001,
    ) -> PositionParams:
        """
        Рассчитать параметры позиции.

        Args:
            balance: текущий баланс
            entry_price: цена входа
            side: "LONG" или "SHORT"
            min_qty: минимальный размер лота
            qty_step: шаг изменения лота
        """
        self._current_balance = balance

        position_usdt = balance * self.trade_cfg.position_size_pct

        raw_qty = position_usdt / entry_price
        qty = max(min_qty, round(raw_qty / qty_step) * qty_step)

        sl_distance = entry_price * self.cfg.stop_loss_pct

        if side == "LONG":
            stop_loss = entry_price - sl_distance
        else:
            stop_loss = entry_price + sl_distance

        take_profit = None
        if self.cfg.use_take_profit:
            tp_distance = entry_price * self.cfg.take_profit_pct
            if side == "LONG":
                take_profit = entry_price + tp_distance
            else:
                take_profit = entry_price - tp_distance

        risk_usdt = qty * sl_distance

        log.info(
            f"Позиция [{side}] qty={qty} | "
            f"Entry={entry_price:.2f} SL={stop_loss:.2f} | "
            f"Риск=${risk_usdt:.2f} ({self.cfg.stop_loss_pct * 100:.1f}%)"
        )

        return PositionParams(
            qty=qty,
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2) if take_profit is not None else None,
            risk_usdt=round(risk_usdt, 4),
        )