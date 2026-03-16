"""
strategy.py - Стратегия EMA Crossover с фильтрацией.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from config import Config
from logger import get_logger

log = get_logger("strategy")


class Signal(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


@dataclass
class StrategyResult:
    signal: Signal
    price: float
    fast_ema: float
    slow_ema: float
    rsi: float
    adx: float
    reason: str


class EMAStrategy:
    def __init__(self, symbol: str = ""):
        """
        Args:
            symbol: Торговый символ (напр. "ETHUSDT").
                    Если передан — per-symbol параметры из Config.symbol_overrides
                    заменяют глобальные значения из StrategyConfig.
                    Это позволяет ETH иметь строже min_ema_spread_pct/min_atr_pct
                    не затрагивая BTC и не дублируя стратегию.
        """
        cfg = Config.strategy
        self.symbol = symbol

        def _p(param: str):
            """Получить параметр с учётом per-symbol override."""
            if symbol:
                return Config.get_symbol_param(symbol, param)
            return getattr(cfg, param)

        self.fast_period = cfg.fast_ema_period
        self.slow_period = cfg.slow_ema_period
        self.htf_period = cfg.htf_ema_period
        self.rsi_period = cfg.rsi_period
        self.rsi_overbought = cfg.rsi_overbought
        self.rsi_oversold = cfg.rsi_oversold
        self.adx_period = cfg.adx_period
        self.adx_threshold = cfg.adx_threshold
        self.bb_period = cfg.bb_period
        self.bb_std = cfg.bb_std
        self.bb_min_width = cfg.bb_min_width_pct
        self.volume_mult = cfg.volume_multiplier
        self.volume_period = cfg.volume_period
        self.min_candles = cfg.min_candles

        self.slope_lookback = cfg.slope_lookback
        self.soft_htf_filter = cfg.soft_htf_filter
        self.require_price_above_slow_for_long = cfg.require_price_above_slow_for_long
        self.require_price_below_slow_for_short = cfg.require_price_below_slow_for_short
        self.atr_period = cfg.atr_period
        self.use_volatility_filter = cfg.use_volatility_filter
        self.use_ema_exit = cfg.use_ema_exit
        self.use_atr_trailing_stop = cfg.use_atr_trailing_stop

        # Per-symbol параметры: читаются через _p() — берут override если есть
        self.long_rsi_limit = _p("long_rsi_limit")
        self.short_rsi_limit = _p("short_rsi_limit")
        self.min_ema_spread_pct = _p("min_ema_spread_pct")  # ETH: 0.0012, BTC: 0.0006
        self.min_atr_pct = _p("min_atr_pct")  # ETH: 0.005,  BTC: 0.0035
        self.atr_trailing_mult = _p("atr_trailing_mult")  # ETH: 3.0,    BTC: 2.5

    @staticmethod
    def _ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        return (100 - 100 / (1 + rs)).fillna(50)

    @staticmethod
    def _adx(df: pd.DataFrame, period: int) -> pd.Series:
        high = df["high"]
        low = df["low"]
        close = df["close"]

        prev_close = close.shift(1)
        prev_high = high.shift(1)
        prev_low = low.shift(1)

        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=df.index,
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=df.index,
        )

        alpha = 1.0 / period
        atr = tr.ewm(alpha=alpha, adjust=False).mean()

        plus_di = (plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)) * 100
        minus_di = (minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)) * 100

        di_sum = (plus_di + minus_di).replace(0, np.nan)
        dx = ((plus_di - minus_di).abs() / di_sum) * 100
        adx = dx.ewm(alpha=alpha, adjust=False).mean().fillna(0)

        return adx

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)

        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        return tr.ewm(alpha=1 / period, adjust=False).mean()

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = df["close"]

        df["ema_fast"] = self._ema(close, self.fast_period)
        df["ema_slow"] = self._ema(close, self.slow_period)
        df["ema_htf"] = self._ema(close, self.htf_period)
        df["rsi"] = self._rsi(close, self.rsi_period)
        df["adx"] = self._adx(df, self.adx_period)
        df["atr"] = self._atr(df, self.atr_period)
        df["atr_pct"] = (df["atr"] / close.replace(0, np.nan)).fillna(0)

        bb_mid = close.rolling(self.bb_period).mean()
        bb_std = close.rolling(self.bb_period).std()

        df["bb_upper"] = bb_mid + self.bb_std * bb_std
        df["bb_lower"] = bb_mid - self.bb_std * bb_std
        df["bb_mid"] = bb_mid
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / bb_mid.replace(0, np.nan)
        df["bb_width"] = df["bb_width"].fillna(0)
        df["bb_ok"] = df["bb_width"] > self.bb_min_width

        df["vol_ma"] = df["volume"].rolling(self.volume_period).mean()
        if self.volume_mult > 0:
            df["vol_ok"] = df["volume"] > (df["vol_ma"] * self.volume_mult)
        else:
            df["vol_ok"] = True

        df["cross_dir"] = np.where(df["ema_fast"] > df["ema_slow"], 1, -1)
        df["crossover"] = df["cross_dir"].diff()

        return df

    def get_signal(
            self,
            df: pd.DataFrame,
            current_position: Optional[str] = None,
            htf_df: Optional[pd.DataFrame] = None,
    ) -> StrategyResult:
        if len(df) < self.min_candles:
            price = float(df["close"].iloc[-1]) if not df.empty else 0.0
            return StrategyResult(
                signal=Signal.HOLD,
                price=price,
                fast_ema=0.0,
                slow_ema=0.0,
                rsi=50.0,
                adx=0.0,
                reason=f"Недостаточно свечей ({len(df)} < {self.min_candles})",
            )

        df = self.add_indicators(df)
        last = df.iloc[-1]

        price = float(last["close"])
        fast_ema = float(last["ema_fast"])
        slow_ema = float(last["ema_slow"])
        htf_ema = float(last["ema_htf"])
        rsi = float(last["rsi"])
        adx = float(last["adx"])
        atr_pct = float(last["atr_pct"])
        vol_ok = bool(last["vol_ok"])
        bb_ok = bool(last["bb_ok"])
        bb_width = float(last["bb_width"])
        crossover = float(last["crossover"])

        lookback = min(self.slope_lookback, len(df) - 1)
        ema_fast_slope = float(df["ema_fast"].iloc[-1] - df["ema_fast"].iloc[-1 - lookback])
        ema_slow_slope = float(df["ema_slow"].iloc[-1] - df["ema_slow"].iloc[-1 - lookback])
        ema_spread_pct = abs(fast_ema - slow_ema) / price if price else 0.0

        if htf_df is not None and len(htf_df) >= self.htf_period:
            htf_df = self.add_indicators(htf_df)
            htf_close = float(htf_df["close"].iloc[-1])
            htf_ema_now = float(htf_df["ema_htf"].iloc[-1])

            if len(htf_df) > lookback:
                htf_ema_prev = float(htf_df["ema_htf"].iloc[-1 - lookback])
            else:
                htf_ema_prev = float(htf_df["ema_htf"].iloc[0])

            if self.soft_htf_filter:
                htf_bullish = htf_close > htf_ema_now
                htf_bearish = htf_close < htf_ema_now
            else:
                htf_bullish = (htf_close > htf_ema_now) and (htf_ema_now > htf_ema_prev)
                htf_bearish = (htf_close < htf_ema_now) and (htf_ema_now < htf_ema_prev)
        else:
            htf_bullish = price > htf_ema
            htf_bearish = price < htf_ema

        if self.use_ema_exit:
            if current_position == "LONG" and price < fast_ema:
                return StrategyResult(
                    signal=Signal.CLOSE,
                    price=price,
                    fast_ema=fast_ema,
                    slow_ema=slow_ema,
                    rsi=rsi,
                    adx=adx,
                    reason="EMA-exit: close < ema_fast -> закрываем LONG",
                )

            if current_position == "SHORT" and price > fast_ema:
                return StrategyResult(
                    signal=Signal.CLOSE,
                    price=price,
                    fast_ema=fast_ema,
                    slow_ema=slow_ema,
                    rsi=rsi,
                    adx=adx,
                    reason="EMA-exit: close > ema_fast -> закрываем SHORT",
                )

        if crossover > 0:
            filters = {
                "HTF не бычий": not htf_bullish,
                "BB флэт": not bb_ok,
                "Объём слабый": not vol_ok,
                "RSI высоковат": rsi >= self.long_rsi_limit,
                "fast EMA не растёт": ema_fast_slope <= 0,
                "slow EMA не растёт": ema_slow_slope <= 0,
                "EMA spread мал": ema_spread_pct <= self.min_ema_spread_pct,
            }

            if self.require_price_above_slow_for_long:
                filters["Цена ниже slow EMA"] = price <= slow_ema

            if self.use_volatility_filter:
                filters["ATR волатильность мала"] = atr_pct < self.min_atr_pct

            failed = [name for name, blocked in filters.items() if blocked]

            if not failed:
                return StrategyResult(
                    signal=Signal.LONG,
                    price=price,
                    fast_ema=fast_ema,
                    slow_ema=slow_ema,
                    rsi=rsi,
                    adx=adx,
                    reason=(
                        f"LONG ✅ | ADX={adx:.1f} BB_w={bb_width:.3f} "
                        f"RSI={rsi:.1f} spread={ema_spread_pct:.4f} ATR%={atr_pct:.4f}"
                    ),
                )

            log.debug(
                f"LONG заблокирован: {', '.join(failed)} | "
                f"ADX={adx:.1f} BB_w={bb_width:.3f} RSI={rsi:.1f} ATR%={atr_pct:.4f}"
            )

        if crossover < 0:
            filters = {
                "HTF не медвежий": not htf_bearish,
                "BB флэт": not bb_ok,
                "Объём слабый": not vol_ok,
                "RSI низковат": rsi <= self.short_rsi_limit,
                "fast EMA не падает": ema_fast_slope >= 0,
                "slow EMA не падает": ema_slow_slope >= 0,
                "EMA spread мал": ema_spread_pct <= self.min_ema_spread_pct,
            }

            if self.require_price_below_slow_for_short:
                filters["Цена выше slow EMA"] = price >= slow_ema

            if self.use_volatility_filter:
                filters["ATR волатильность мала"] = atr_pct < self.min_atr_pct

            failed = [name for name, blocked in filters.items() if blocked]

            if not failed:
                return StrategyResult(
                    signal=Signal.SHORT,
                    price=price,
                    fast_ema=fast_ema,
                    slow_ema=slow_ema,
                    rsi=rsi,
                    adx=adx,
                    reason=(
                        f"SHORT ✅ | ADX={adx:.1f} BB_w={bb_width:.3f} "
                        f"RSI={rsi:.1f} spread={ema_spread_pct:.4f} ATR%={atr_pct:.4f}"
                    ),
                )

            log.debug(
                f"SHORT заблокирован: {', '.join(failed)} | "
                f"ADX={adx:.1f} BB_w={bb_width:.3f} RSI={rsi:.1f} ATR%={atr_pct:.4f}"
            )

        return StrategyResult(
            signal=Signal.HOLD,
            price=price,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            rsi=rsi,
            adx=adx,
            reason=(
                f"HOLD | EMA {fast_ema:.0f}/{slow_ema:.0f} "
                f"ADX={adx:.1f} RSI={rsi:.1f} ATR%={atr_pct:.4f}"
            ),
        )
