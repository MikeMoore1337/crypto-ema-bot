# 📈 Crypto EMA Trend Bot

Алгоритмический торговый бот для Bybit на основе **EMA trend-following стратегии**.

Бот поддерживает:

- 📊 Бэктест на исторических данных
- 🔬 Мульти-бэктест по нескольким символам и временным окнам
- 🧪 Paper trading (виртуальные сделки, реальные данные)
- 🤖 Live trading
- 📩 Telegram уведомления с поддержкой нескольких инстансов
- 🔧 Параллельная оптимизация параметров

---

## ⚙️ Стратегия

Бот ищет начало тренда и удерживает позицию пока тренд не закончится.

### Вход LONG

- EMA(21) пересекает EMA(55) снизу вверх
- Цена выше EMA(55)
- EMA slope положительный
- EMA spread достаточный (`min_ema_spread_pct`)
- HTF (1h) тренд подтверждает направление
- RSI ниже `long_rsi_limit`
- ATR выше минимального порога (рынок достаточно волатилен)

### Вход SHORT — зеркальные условия

### Выход из позиции

Используется **ATR trailing stop** вместо фиксированного тейк-профита — это позволяет удерживать длинные тренды и
забирать большие движения.

Дополнительно работает **breakeven stop**: как только позиция прошла `1×ATR` в нашу сторону — стоп переносится в точку
входа. Убыточная сделка превращается в нулевую.

Также есть фиксированный **Stop Loss** как защита от резких движений против позиции.

---

## 📊 Результаты бэктеста (180 дней, BTC + ETH)

| Символ      | Return | Profit Factor | Max DD | Trades | Win Rate |
|-------------|--------|---------------|--------|--------|----------|
| BTCUSDT avg | +48.7% | 13.9          | 1.5%   | 67     | 35.8%    |
| ETHUSDT avg | +62.9% | 9.7           | 2.0%   | 66     | 20.0%    |

ETH: низкий WR компенсируется высоким RR — стратегия ловит несколько крупных трендов на фоне мелких потерь.

---

## 🗂️ Структура проекта

```
├── bot.py                 — основной цикл (live / paper / backtest)
├── strategy.py            — EMA стратегия с фильтрами
├── backtest.py            — движок бэктестинга с ATR trailing stop
├── multi_backtest.py      — массовый бэктест по символам и окнам
├── optimize.py            — параллельная оптимизация параметров
├── risk_manager.py        — риск-менеджмент, дневные лимиты
├── exchange.py            — Bybit API
├── config.py              — все настройки + per-symbol overrides
├── config_validation.py   — проверка конфига перед запуском
├── logger.py              — цветные логи + запись в файл
├── telegram_notifier.py   — Telegram уведомления с BOT_NAME
├── requirements.txt
└── .env                   — API ключи (не коммитить!)
```

---

## 🔧 Установка

```bash
git clone https://github.com/MikeMoore1337/crypto-ema-bot.git
cd crypto-ema-bot

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

Создай `.env`:

```env
BYBIT_API_KEY=xxx
BYBIT_API_SECRET=xxx
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx
```

---

## 📊 Режимы работы

```bash
# Бэктест одного символа (из config.py)
python bot.py --mode backtest

# Мульти-бэктест BTC + ETH по двум временным окнам
python multi_backtest.py

# Paper trading (виртуальные сделки)
python bot.py --mode paper

# Оптимизация параметров
python optimize.py

# Live trading (только после тестирования!)
python bot.py --mode live
```

---

## ⚙️ Основные настройки (`config.py`)

```python
# Таймфрейм
interval = "5"  # минуты

# Риск на сделку
position_size_pct = 0.10  # 10% баланса
stop_loss_pct = 0.02  # 2% фиксированный стоп

# ATR trailing stop
use_atr_trailing_stop = True
atr_trailing_mult = 2.5  # стоп = max_price - ATR × 2.5

# Breakeven stop
use_breakeven_stop = True
atr_breakeven_trigger = 1.0  # после 1×ATR прибыли → стоп в безубыток

# Фильтры входа
long_rsi_limit = 72.0
short_rsi_limit = 35.0
min_ema_spread_pct = 0.0006
slope_lookback = 5
use_volatility_filter = True
min_atr_pct = 0.0035
```

### Per-symbol параметры

Каждый символ может иметь свои параметры стратегии — это позволяет не менять глобальный конфиг и точечно настраивать под
поведение конкретного актива:

```python
symbol_overrides = {
    "BTCUSDT": SymbolOverride(
        min_atr_pct=0.0015,
        min_ema_spread_pct=0.0004,
        atr_trailing_mult=2.5,
        atr_breakeven_trigger=1.0,
    ),
    "ETHUSDT": SymbolOverride(
        min_atr_pct=0.005,
        min_ema_spread_pct=0.0012,
        atr_trailing_mult=3.0,
        atr_breakeven_trigger=1.2,
    ),
}
```

---

## 📩 Telegram уведомления

Бот отправляет:

- открытие и закрытие позиции
- ежедневный отчёт
- watchdog (бот завис)
---

## 🖥️ Запуск на сервере (systemd)

Создай `/etc/systemd/system/crypto-bot.service`:

```ini
[Unit]
Description = Crypto Trading Bot
After = network-online.target

[Service]
Type = simple
User = root
WorkingDirectory = /root/bot_2
ExecStart = /root/bot_2/.venv/bin/python bot.py --mode paper
Restart = always
RestartSec = 30
StandardOutput = journal
StandardError = journal
SyslogIdentifier = crypto-bot

[Install]
WantedBy = multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable crypto-bot
systemctl start crypto-bot

# Логи
journalctl -u crypto-bot -f
```

---

## 🧪 Рекомендуемый workflow

1. **Мульти-бэктест** — проверить стратегию на истории

```bash
python multi_backtest.py
```

2. **Оптимизация** — подобрать параметры (опционально)

```bash
python optimize.py
```

3. **Paper trading** — минимум 2–4 недели на реальных данных

```bash
python bot.py --mode paper
```

4. **Live trading** — только если paper показал стабильный результат

```bash
# Сменить testnet=False в config.py
python bot.py --mode live
```

---

## ⚠️ Предупреждение

Криптовалютная торговля связана с высоким риском потери капитала.

Проект предназначен для обучения и экспериментов. Использование на реальных средствах — на ваш риск.

Никогда не давай API ключу права на **вывод средств**.
