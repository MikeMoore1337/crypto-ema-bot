"""
bot.py - Основной торговый бот.

Запуск:
    python bot.py --mode live
    python bot.py --mode backtest
    python bot.py --mode paper
"""

import argparse
import os
import threading
import time
from datetime import UTC, date, datetime

import pandas as pd

from backtest import Backtester
from config import Config
from exchange import BybitExchange
from logger import get_logger
from risk_manager import RiskManager
from strategy import EMAStrategy, Signal
from telegram_notifier import TelegramNotifier

log = get_logger("bot")


class TradingBot:
    def __init__(self, mode: str = "paper"):
        self.mode = mode
        self.cfg = Config.trading
        self.exchange = BybitExchange()
        self.strategy = EMAStrategy()
        self.risk_manager = RiskManager()

        self.symbols = self.cfg.symbols if self.cfg.symbols else [self.cfg.symbol]

        self.positions: dict[str, str | None] = {symbol: None for symbol in self.symbols}
        self.position_info: dict[str, dict] = {symbol: {} for symbol in self.symbols}

        self.paper_balance = 100.0
        self.paper_trades: list[dict] = []

        # Счётчики свечей после последнего закрытия позиции — для логики reentry
        self.bars_since_close: dict[str, int] = {symbol: 999 for symbol in self.symbols}

        self.last_report_day: date | None = None
        self.last_heartbeat = time.time()

        self.telegram = None
        if Config.telegram.enabled and Config.telegram.token and Config.telegram.chat_id:
            self.telegram = TelegramNotifier(
                Config.telegram.token,
                Config.telegram.chat_id,
            )

        symbols_str = ", ".join(self.symbols)
        log.info(
            f"Бот запущен в режиме [{mode.upper()}] | "
            f"symbols=[{symbols_str}] [{self.cfg.interval}m]"
        )

        if self.telegram:
            self.telegram.send(
                f"🤖 Бот запущен | режим: {mode.upper()} | "
                f"symbols: {symbols_str} | TF: {self.cfg.interval}m"
            )

        threading.Thread(target=self.watchdog, daemon=True).start()

    def _update_trailing_stop(self, symbol: str, signal_df: pd.DataFrame) -> None:
        if not Config.strategy.use_atr_trailing_stop:
            return

        info = self.position_info.get(symbol, {})
        if not info:
            return

        enriched = self.strategy.add_indicators(signal_df)
        last = enriched.iloc[-1]

        atr = float(last.get("atr", 0))
        if atr <= 0:
            return

        close_price = float(last["close"])
        mult = Config.strategy.atr_trailing_mult

        entry_price = info.get("entry", 0.0)

        if info["side"] == "LONG":
            info["highest_close"] = max(info["highest_close"], close_price)
            candidate = info["highest_close"] - atr * mult

            if info["trailing_stop"] is None:
                info["trailing_stop"] = candidate
            else:
                info["trailing_stop"] = max(info["trailing_stop"], candidate)

            # Breakeven: как только цена прошла trigger×ATR вверх → стоп минимум в точке входа
            if Config.strategy.use_breakeven_stop and entry_price > 0:
                trigger = Config.strategy.atr_breakeven_trigger
                if close_price >= entry_price + atr * trigger:
                    info["sl"] = max(info["sl"], entry_price)

        else:
            info["lowest_close"] = min(info["lowest_close"], close_price)
            candidate = info["lowest_close"] + atr * mult

            if info["trailing_stop"] is None:
                info["trailing_stop"] = candidate
            else:
                info["trailing_stop"] = min(info["trailing_stop"], candidate)

            # Breakeven: как только цена прошла trigger×ATR вниз → стоп максимум в точке входа
            if Config.strategy.use_breakeven_stop and entry_price > 0:
                trigger = Config.strategy.atr_breakeven_trigger
                if close_price <= entry_price - atr * trigger:
                    info["sl"] = min(info["sl"], entry_price)

    def run(self) -> None:
        log.info("▶️  Запуск торгового цикла...")
        log.info("   Ctrl+C для остановки")

        if self.mode == "live":
            for symbol in self.symbols:
                self.exchange.set_leverage(symbol, leverage=1)

        balance = self._get_balance()
        self.risk_manager.reset_daily_stats(balance)
        self.risk_manager.update_balance(balance)
        last_day = datetime.now().day

        while True:
            try:
                self.last_heartbeat = time.time()

                now_utc = datetime.now(UTC)
                today = now_utc.date()

                if self.last_report_day is None:
                    self.last_report_day = today
                elif today != self.last_report_day:
                    self.send_daily_report()
                    self.last_report_day = today

                current_day = datetime.now().day
                if current_day != last_day:
                    balance = self._get_balance()
                    self.risk_manager.reset_daily_stats(balance)
                    self.risk_manager.update_balance(balance)
                    last_day = current_day

                for symbol in self.symbols:
                    self._tick(symbol)

                sleep_seconds = int(self.cfg.interval) * 60
                log.debug(f"Ожидание {sleep_seconds} сек...")
                time.sleep(sleep_seconds)

            except KeyboardInterrupt:
                log.info("🛑 Бот остановлен пользователем")
                self._print_paper_summary()
                break
            except Exception as e:
                log.error(f"Ошибка в цикле: {e}")
                time.sleep(60)

    def _tick(self, symbol: str) -> None:
        df = self.exchange.get_candles(
            symbol=symbol,
            interval=self.cfg.interval,
            limit=max(Config.strategy.min_candles + 20, 120),
        )

        if df.empty or len(df) < Config.strategy.min_candles + 2:
            log.warning(f"{symbol}: недостаточно данных для анализа")
            return

        htf_df = self.exchange.get_candles(
            symbol=symbol,
            interval="60",
            limit=max(Config.strategy.htf_ema_period + 20, 100),
        )

        if htf_df.empty:
            log.warning(f"{symbol}: не удалось получить HTF данные")
            return

        signal_df = df.iloc[:-1].copy()
        current_bar = df.iloc[-1]
        htf_signal_df = htf_df.iloc[:-1].copy()

        if self.positions[symbol] is not None:
            self._update_trailing_stop(symbol, signal_df)

        if (
            self.mode == "paper"
            and self.positions[symbol] is not None
            and self._check_paper_exit_by_stops(symbol, current_bar)
        ):
            return

        balance = self._get_balance()
        self.risk_manager.update_balance(balance)

        can_trade, reason = self.risk_manager.can_trade(balance)
        current_position = self.positions[symbol]

        result = self.strategy.get_signal(
            signal_df,
            current_position,
            htf_signal_df,
        )
        log.info(f"{symbol}: Сигнал: {result.signal.value} | {result.reason}")

        exec_price = float(current_bar["open"])

        # Увеличиваем счётчик свечей с последнего закрытия
        if self.positions[symbol] is None:
            self.bars_since_close[symbol] = self.bars_since_close.get(symbol, 999) + 1

        if result.signal == Signal.CLOSE and current_position is not None:
            self._close_position(symbol, exec_price, exit_reason="Signal")
            self.bars_since_close[symbol] = 0

        elif result.signal in (Signal.LONG, Signal.SHORT) and current_position is None:
            if not can_trade:
                log.warning(f"{symbol}: ⛔ Торговля заблокирована: {reason}")
                return
            self._open_position(symbol, result.signal.value, exec_price, balance, adx=result.adx)

        # Повторный вход (reentry) — если нет кроссовера, но тренд продолжается
        elif (
            result.signal == Signal.HOLD
            and current_position is None
            and can_trade
            and Config.strategy.allow_trend_reentry
            and self.bars_since_close.get(symbol, 999)
            >= Config.strategy.reentry_min_bars_after_close
        ):
            self._try_reentry(symbol, signal_df, htf_signal_df, exec_price, balance)

    def _open_position(
        self, symbol: str, side: str, price: float, balance: float, adx: float = 0.0
    ) -> None:
        params = self.risk_manager.calculate_position(
            balance=balance,
            entry_price=price,
            side=side,
            signal_context={"adx": result.adx},
        )

        if self.mode == "live":
            bybit_side = "Buy" if side == "LONG" else "Sell"
            order_id = self.exchange.place_market_order(
                symbol=symbol,
                side=bybit_side,
                qty=params.qty,
                stop_loss=params.stop_loss,
                take_profit=params.take_profit,
            )
            if order_id:
                self.positions[symbol] = side
                self.position_info[symbol] = {
                    "side": side,
                    "entry": price,
                    "qty": params.qty,
                    "order_id": order_id,
                    "sl": params.stop_loss,
                    "tp": params.take_profit,
                    "trailing_stop": None,
                    "highest_close": price,
                    "lowest_close": price,
                }

                log.info(
                    f"✅ [LIVE] {symbol} | Открыт {side} @ {price:.2f} | "
                    f"qty={params.qty:.4f} SL={params.stop_loss:.2f}"
                )

                if self.telegram:
                    self.telegram.send(
                        f"🚀 {symbol} {side}\n"
                        f"Цена: {price:.2f}\n"
                        f"SL: {params.stop_loss:.2f}\n"
                        f"Qty: {params.qty:.4f}"
                    )

        elif self.mode == "paper":
            self.positions[symbol] = side
            self.position_info[symbol] = {
                "side": side,
                "entry": price,
                "qty": params.qty,
                "sl": params.stop_loss,
                "tp": params.take_profit,
                "trailing_stop": None,
                "highest_close": price,
                "lowest_close": price,
            }

            log.info(
                f"📝 [PAPER] {symbol} | Открыт {side} @ {price:.2f} | "
                f"qty={params.qty:.4f} SL={params.stop_loss:.2f}"
            )

            if self.telegram:
                self.telegram.send(
                    f"🚀 {symbol} {side}\n"
                    f"Цена: {price:.2f}\n"
                    f"SL: {params.stop_loss:.2f}\n"
                    f"Qty: {params.qty:.4f}"
                )

    def _close_position(self, symbol: str, price: float, exit_reason: str = "Signal") -> None:
        info = self.position_info.get(symbol, {})
        if not info:
            return

        position_side = self.positions[symbol]

        if self.mode == "live":
            bybit_side = "Buy" if position_side == "LONG" else "Sell"
            self.exchange.close_position(
                symbol,
                bybit_side,
                info["qty"],
            )

            if self.telegram:
                self.telegram.send(
                    f"❌ {symbol} | Закрыта {position_side}\n"
                    f"Цена выхода: {price:.2f}\n"
                    f"Причина: {exit_reason}"
                )

        elif self.mode == "paper":
            entry = info["entry"]
            qty = info["qty"]

            if position_side == "LONG":
                pnl = (price - entry) * qty
            else:
                pnl = (entry - price) * qty

            commission = (entry + price) * qty * Config.backtest.commission_pct
            net_pnl = pnl - commission

            self.paper_balance += net_pnl
            self.paper_trades.append(
                {
                    "symbol": symbol,
                    "pnl": net_pnl,
                }
            )

            self.risk_manager.record_trade_result(
                net_pnl,
                balance_after_trade=self.paper_balance,
            )

            emoji = "✅" if net_pnl > 0 else "❌"
            log.info(
                f"📝 [PAPER] {symbol} | Закрыт {position_side} @ {price:.2f} | "
                f"Причина: {exit_reason} | P&L: {emoji} ${net_pnl:+.4f} | "
                f"Баланс: ${self.paper_balance:.4f}"
            )

            if self.telegram:
                self.telegram.send(
                    f"❌ {symbol} | Закрыта {position_side}\n"
                    f"Цена выхода: {price:.2f}\n"
                    f"Причина: {exit_reason}\n"
                    f"P&L: {net_pnl:+.4f}$\n"
                    f"Баланс: {self.paper_balance:.4f}$"
                )

        self.positions[symbol] = None
        self.position_info[symbol] = {}

    def _check_paper_exit_by_stops(self, symbol: str, bar: pd.Series) -> bool:
        info = self.position_info.get(symbol, {})
        if not info:
            return False

        side = info["side"]
        sl = info["sl"]
        trailing = info.get("trailing_stop")
        high = float(bar["high"])
        low = float(bar["low"])

        if side == "LONG":
            active_stop = sl
            if trailing is not None:
                active_stop = max(active_stop, trailing)

            if low <= active_stop:
                reason = (
                    "ATR-Trail"
                    if trailing is not None and active_stop == max(sl, trailing) and trailing > sl
                    else "SL"
                )
                self._close_position(symbol, active_stop, exit_reason=reason)
                self.bars_since_close[symbol] = 0
                return True

        elif side == "SHORT":
            active_stop = sl
            if trailing is not None:
                active_stop = min(active_stop, trailing)

            if high >= active_stop:
                reason = (
                    "ATR-Trail"
                    if trailing is not None and active_stop == min(sl, trailing) and trailing < sl
                    else "SL"
                )
                self._close_position(symbol, active_stop, exit_reason=reason)
                self.bars_since_close[symbol] = 0
                return True

        return False

    def _try_reentry(
        self,
        symbol: str,
        signal_df: pd.DataFrame,
        htf_df: pd.DataFrame,
        exec_price: float,
        balance: float,
    ) -> None:
        """
        Повторный вход в тренд без нового кроссовера EMA.

        Условия для SHORT reentry:
          - fast EMA < slow EMA (тренд вниз)
          - HTF подтверждает медвежий тренд
          - ADX выше порога (тренд сильный)
          - ATR выше минимального порога (достаточная волатильность)
          - RSI не перепродан (> short_rsi_limit)

        Зеркально для LONG reentry.
        """
        df = self.strategy.add_indicators(signal_df)
        last = df.iloc[-1]

        fast_ema = float(last["ema_fast"])
        slow_ema = float(last["ema_slow"])
        adx = float(last["adx"])
        rsi = float(last["rsi"])
        atr_pct = float(last["atr_pct"])

        cfg = Config.strategy

        # Определяем текущее направление тренда по EMA
        trend_short = fast_ema < slow_ema
        trend_long = fast_ema > slow_ema

        # HTF фильтр
        htf_bearish = False
        htf_bullish = False
        if len(htf_df) >= cfg.htf_ema_period:
            htf_enriched = self.strategy.add_indicators(htf_df)
            htf_close = float(htf_enriched["close"].iloc[-1])
            htf_ema_now = float(htf_enriched["ema_htf"].iloc[-1])
            htf_bearish = htf_close < htf_ema_now
            htf_bullish = htf_close > htf_ema_now

        reentry_side = None

        if (
            trend_short
            and htf_bearish
            and adx >= cfg.adx_threshold
            and atr_pct >= cfg.min_atr_pct
            and rsi > cfg.short_rsi_limit
        ):
            reentry_side = "SHORT"

        elif (
            trend_long
            and htf_bullish
            and adx >= cfg.adx_threshold
            and atr_pct >= cfg.min_atr_pct
            and rsi < cfg.long_rsi_limit
        ):
            reentry_side = "LONG"

        if reentry_side:
            log.info(
                f"{symbol}: 🔄 Reentry {reentry_side} | "
                f"ADX={adx:.1f} RSI={rsi:.1f} ATR%={atr_pct:.4f}"
            )
            self._open_position(symbol, reentry_side, exec_price, balance, adx=adx)

    def _get_balance(self) -> float:
        if self.mode == "paper":
            return self.paper_balance
        return self.exchange.get_balance("USDT")

    def _print_paper_summary(self) -> None:
        if not self.paper_trades:
            return

        wins = [t for t in self.paper_trades if t["pnl"] > 0]
        losses = [t for t in self.paper_trades if t["pnl"] <= 0]

        print("\n" + "═" * 55)
        print("             ИТОГИ PAPER TRADING")
        print("═" * 55)
        print("  Начальный баланс:  $100.00")
        print(f"  Конечный баланс:   ${self.paper_balance:.4f}")
        print(f"  Всего сделок:      {len(self.paper_trades)}")
        print(f"  Прибыльных:        {len(wins)}")
        print(f"  Убыточных:         {len(losses)}")
        if self.paper_trades:
            wr = len(wins) / len(self.paper_trades) * 100
            print(f"  Win Rate:          {wr:.1f}%")
        print("─" * 55)

        by_symbol: dict[str, list[float]] = {}
        for trade in self.paper_trades:
            by_symbol.setdefault(trade["symbol"], []).append(trade["pnl"])

        for symbol, pnls in by_symbol.items():
            total = sum(pnls)
            count = len(pnls)
            print(f"  {symbol:<10} сделок={count:<3} pnl={total:+.4f}$")

        print("═" * 55)

    def send_daily_report(self) -> None:
        if not self.telegram:
            return

        stats = self.risk_manager.daily_stats
        winrate = (stats["wins"] / stats["trades"] * 100) if stats["trades"] > 0 else 0.0

        msg = (
            "📊 Daily Report\n\n"
            f"Баланс: {self.risk_manager.balance:.2f} USDT\n"
            f"Дневной P&L: {stats['pnl_usdt']:+.2f}$\n"
            f"Дневной P&L %: {stats['pnl_pct']:+.2f}%\n\n"
            f"Сделок: {stats['trades']}\n"
            f"Побед: {stats['wins']}\n"
            f"Поражений: {stats['losses']}\n"
            f"Winrate: {winrate:.1f}%"
        )

        self.telegram.send(msg)

    def watchdog(self) -> None:
        while True:
            time.sleep(60)

            if time.time() - self.last_heartbeat > 600:
                log.error("Watchdog: бот завис, завершаю процесс для перезапуска systemd")
                if self.telegram:
                    try:
                        self.telegram.send("⚠️ Watchdog: бот завис, выполняю перезапуск")
                    except Exception:
                        pass
                os._exit(1)


def load_full_history(
    exchange: BybitExchange,
    symbol: str,
    interval: str,
    needed_candles: int,
) -> pd.DataFrame:
    all_dfs = []
    end_ms = int(datetime.now(UTC).timestamp() * 1000)

    while True:
        batch = exchange.get_candles(
            symbol=symbol,
            interval=interval,
            limit=1000,
            end_ms=end_ms,
        )

        if batch.empty:
            break

        all_dfs.append(batch)

        if sum(len(x) for x in all_dfs) >= needed_candles:
            break

        earliest_ts = batch["timestamp"].iloc[0]
        end_ms = int(earliest_ts.timestamp() * 1000) - 1

        if len(batch) < 1000:
            break

    if not all_dfs:
        return pd.DataFrame()

    df = (
        pd.concat(all_dfs)
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    if len(df) > needed_candles:
        df = df.tail(needed_candles).reset_index(drop=True)

    return df


def run_backtest() -> None:
    log.info("Запуск бэктестинга...")

    exchange = BybitExchange()
    backtester = Backtester()

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
        log.error("Не удалось загрузить исторические данные")
        return

    log.info(f"Загружено {len(df)} свечей [{cfg.interval}m]")

    htf_days = bt_cfg.days + 10
    htf_needed = htf_days * 24

    log.info("Загрузка часовых свечей для HTF фильтра...")
    htf_df: pd.DataFrame | None = load_full_history(
        exchange=exchange,
        symbol=cfg.symbol,
        interval="60",
        needed_candles=htf_needed,
    )

    if htf_df is not None and htf_df.empty:
        log.warning("Не удалось загрузить HTF данные - бэктест пойдёт без полноценного HTF фильтра")
        htf_df = None
    elif htf_df is not None:
        log.info(f"Загружено {len(htf_df)} часовых свечей [1h]")

    report = backtester.run(df, htf_df)
    report.print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit Trading Bot")
    parser.add_argument(
        "--mode",
        choices=["live", "paper", "backtest"],
        default="paper",
        help="Режим работы: live / paper / backtest",
    )
    args = parser.parse_args()

    if args.mode == "backtest":
        run_backtest()
    else:
        bot = TradingBot(mode=args.mode)
        bot.run()


if __name__ == "__main__":
    main()
