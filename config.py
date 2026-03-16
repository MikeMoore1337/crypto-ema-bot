"""
config.py - Централизованная конфигурация бота.

Все настройки в одном месте. Не хардкодим ничего в логике.
API ключи читаются из .env файла - никогда не коммитим их в git!
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class ExchangeConfig:
    """Настройки подключения к бирже."""
    api_key: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""))
    testnet: bool = True


@dataclass
class TradingConfig:
    """Параметры торговли."""
    symbol: str = "BTCUSDT"
    symbols: list[str] = field(default_factory=lambda: [
        "BTCUSDT",
        "ETHUSDT",
    ])
    interval: str = "5"
    category: str = "linear"
    position_size_pct: float = 0.10


@dataclass
class RiskConfig:
    """Параметры риск-менеджмента."""
    stop_loss_pct: float = 0.02

    # Фиксированный тейк-профит отключён
    use_take_profit: bool = False
    take_profit_pct: float = 0.03

    max_daily_loss_pct: float = 0.05
    max_open_positions: int = 1


@dataclass
class StrategyConfig:
    """Параметры стратегии EMA trend-following."""
    fast_ema_period: int = 21
    slow_ema_period: int = 55
    htf_ema_period: int = 50

    rsi_period: int = 14
    rsi_overbought: float = 75.0
    rsi_oversold: float = 25.0

    adx_period: int = 14
    adx_threshold: float = 15.0

    bb_period: int = 20
    bb_std: float = 2.0
    bb_min_width_pct: float = 0.01

    volume_period: int = 20
    volume_multiplier: float = 0.0

    min_candles: int = 60

    long_rsi_limit: float = 72.0
    short_rsi_limit: float = 35.0
    min_ema_spread_pct: float = 0.0006
    slope_lookback: int = 5

    soft_htf_filter: bool = True
    require_price_above_slow_for_long: bool = True
    require_price_below_slow_for_short: bool = True

    # ATR / volatility
    atr_period: int = 14
    use_volatility_filter: bool = True
    min_atr_pct: float = 0.0035

    # Выходы
    use_ema_exit: bool = False
    use_atr_trailing_stop: bool = True
    atr_trailing_mult: float = 2.5

    # Breakeven stop: после прохождения N×ATR переносим стоп в безубыток.
    # Зачем: 52 убыточных ETH-сделки с avg -1.5% → часть станет 0%.
    # Механизм: как только цена прошла atr_breakeven_trigger × ATR в нашу
    # сторону — стоп переносится на entry_price (без комиссии, т.к. позиция
    # уже "бесплатна"). Это не мешает trailing stop — просто поднимает пол.
    use_breakeven_stop: bool = True
    atr_breakeven_trigger: float = 1.0  # После 1× ATR прибыли → в безубыток


@dataclass
class SymbolOverride:
    """
    Переопределение параметров стратегии для конкретного символа.

    Зачем: BTC (23 сделки, WR 39%) и ETH (62 сделки, WR 16%) ведут себя
    по-разному. ETH более волатилен и требует более строгих фильтров входа,
    чтобы сократить 62 → ~30 сделок с лучшим WR.

    Используется только если задан для символа в symbol_overrides.
    Незаданные поля берутся из StrategyConfig.
    """
    min_ema_spread_pct: float | None = None
    min_atr_pct: float | None = None
    atr_trailing_mult: float | None = None
    atr_breakeven_trigger: float | None = None
    long_rsi_limit: float | None = None
    short_rsi_limit: float | None = None


@dataclass
class BacktestConfig:
    """Параметры бэктестинга."""
    initial_balance: float = 100.0
    days: int = 90
    commission_pct: float = 0.00055


@dataclass
class TelegramConfig:
    """Настройки Telegram уведомлений."""
    enabled: bool = True
    token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))



class Config:
    exchange = ExchangeConfig()
    trading = TradingConfig()
    risk = RiskConfig()
    strategy = StrategyConfig()
    backtest = BacktestConfig()
    telegram = TelegramConfig()

    # Per-symbol переопределения параметров стратегии.
    # ETH нужны более строгие фильтры входа: у него 62 сделки vs 23 у BTC.
    # Цель: сократить ETH до ~30 сделок с WR ~30%+ при сохранении trailing-winners.
    # Config — обычный класс (не @dataclass), поэтому инициализируем dict напрямую,
    # без field(default_factory=...) — это работает только внутри @dataclass.
    symbol_overrides: dict = {
        "ETHUSDT": SymbolOverride(
            min_ema_spread_pct=0.0012,
            min_atr_pct=0.005,
            atr_trailing_mult=3.0,
            atr_breakeven_trigger=1.2,
        ),
        "BTCUSDT": SymbolOverride(
            atr_trailing_mult=2.5,
            atr_breakeven_trigger=1.0,
        ),
    }

    @classmethod
    def get_symbol_param(cls, symbol: str, param: str):
        """
        Получить параметр стратегии с учётом per-symbol переопределения.

        Пример:
            mult = Config.get_symbol_param("ETHUSDT", "atr_trailing_mult")
            # вернёт 3.0 для ETH, 2.5 для BTC, дефолт для остальных
        """
        override = cls.symbol_overrides.get(symbol)
        if override is not None:
            val = getattr(override, param, None)
            if val is not None:
                return val
        return getattr(cls.strategy, param)
