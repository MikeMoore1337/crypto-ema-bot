"""
exchange.py - Обёртка над Bybit API.

Принцип: вся работа с биржей только через этот класс.
Остальной код не знает о HTTP-запросах и деталях API.
"""

from typing import Optional

import pandas as pd
from pybit.unified_trading import HTTP

from config import Config
from logger import get_logger

log = get_logger("exchange")


class BybitExchange:
    """
    Клиент для работы с Bybit Unified Trading API.
    """

    def __init__(self):
        cfg = Config.exchange
        self.session = HTTP(
            testnet=cfg.testnet,
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
        )
        mode = "TESTNET" if cfg.testnet else "MAINNET"
        log.info(f"Подключение к Bybit [{mode}]")

    def get_candles(
            self,
            symbol: str,
            interval: str,
            limit: int = 200,
            end_ms: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Получить свечи с биржи Bybit.

        symbol: BTCUSDT
        interval: "5", "15", "60"
        limit: количество свечей
        """

        kwargs = {
            "category": Config.trading.category,
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }

        if end_ms is not None:
            kwargs["end"] = end_ms

        for attempt in range(3):
            try:
                response = self.session.get_kline(**kwargs)
                result = response.get("result", {}).get("list", [])

                if not result:
                    log.warning(f"Пустой ответ для {symbol} [{interval}m]")
                    return pd.DataFrame()

                df = pd.DataFrame(result, columns=[
                    "timestamp", "open", "high", "low", "close", "volume", "turnover"
                ])

                df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
                df["timestamp"] = df["timestamp"].dt.tz_localize(None)

                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = df[col].astype(float)

                df = df.sort_values("timestamp").reset_index(drop=True)
                df = df.drop(columns=["turnover"])

                log.debug(f"Получено {len(df)} свечей {symbol} [{interval}m]")
                return df

            except Exception as e:
                log.error(f"Ошибка получения свечей (попытка {attempt + 1}/3): {e}")

                if attempt < 2:
                    import time
                    time.sleep(2)
                else:
                    return pd.DataFrame()

    def get_ticker(self, symbol: str) -> Optional[float]:
        """Текущая цена символа."""
        try:
            response = self.session.get_tickers(
                category=Config.trading.category,
                symbol=symbol,
            )
            price = float(response["result"]["list"][0]["lastPrice"])
            return price
        except Exception as e:
            log.error(f"Ошибка получения цены {symbol}: {e}")
            return None

    def get_balance(self, coin: str = "USDT") -> float:
        """Доступный баланс в указанной монете."""
        try:
            response = self.session.get_wallet_balance(accountType="UNIFIED")
            coins = response["result"]["list"][0]["coin"]
            for c in coins:
                if c["coin"] == coin:
                    return float(c["availableToWithdraw"])
            return 0.0
        except Exception as e:
            log.error(f"Ошибка получения баланса: {e}")
            return 0.0

    def get_positions(self, symbol: str) -> list:
        """Список открытых позиций по символу."""
        try:
            response = self.session.get_positions(
                category=Config.trading.category,
                symbol=symbol,
            )
            return response["result"]["list"]
        except Exception as e:
            log.error(f"Ошибка получения позиций: {e}")
            return []

    def place_market_order(
            self,
            symbol: str,
            side: str,
            qty: float,
            stop_loss: Optional[float] = None,
            take_profit: Optional[float] = None,
    ) -> Optional[str]:
        """
        Открыть позицию по рынку с встроенным стоп-лоссом и тейк-профитом.

        Returns:
            order_id если успешно, None при ошибке
        """
        try:
            params: dict = {
                "category": Config.trading.category,
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": str(qty),
                "timeInForce": "IOC",
            }

            if stop_loss is not None:
                params["stopLoss"] = str(round(stop_loss, 2))
            if take_profit is not None:
                params["takeProfit"] = str(round(take_profit, 2))

            response = self.session.place_order(**params)
            order_id = response["result"]["orderId"]

            log.info(f"✅ Ордер {side} {qty} {symbol} | SL: {stop_loss} | TP: {take_profit}")
            return order_id

        except Exception as e:
            log.error(f"Ошибка создания ордера: {e}")
            return None

    def close_position(self, symbol: str, side: str, qty: float) -> bool:
        """Закрыть позицию по рынку (reduceOnly=True)."""
        close_side = "Sell" if side == "Buy" else "Buy"
        try:
            self.session.place_order(
                category=Config.trading.category,
                symbol=symbol,
                side=close_side,
                orderType="Market",
                qty=str(qty),
                reduceOnly=True,
                timeInForce="IOC",
            )
            log.info(f"❌ Закрыта позиция {symbol} {side} {qty}")
            return True
        except Exception as e:
            log.error(f"Ошибка закрытия позиции: {e}")
            return False

    def set_leverage(self, symbol: str, leverage: int = 1) -> None:
        """Установить кредитное плечо."""
        try:
            self.session.set_leverage(
                category=Config.trading.category,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            log.info(f"Плечо {leverage}x установлено для {symbol}")
        except Exception as e:
            log.debug(f"set_leverage: {e}")
