"""
telegram_notifier.py - Отправка уведомлений в Telegram.
"""

from __future__ import annotations

import requests

from logger import get_logger

log = get_logger("telegram")


class TelegramNotifier:
    """Простой отправщик сообщений в Telegram Bot API."""

    def __init__(self, token: str, chat_id: str):
        self.token = token.strip()
        self.chat_id = str(chat_id).strip()
        self.url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    def send(self, text: str) -> bool:
        """
        Отправить сообщение в Telegram.

        Returns:
            True  - если Telegram принял сообщение
            False - если произошла ошибка
        """
        if not self.token or not self.chat_id:
            log.warning("Telegram не настроен: пустой token или chat_id")
            return False

        try:
            response = requests.post(
                self.url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                },
                timeout=10,
            )

            if response.status_code != 200:
                log.error(
                    f"Telegram API вернул HTTP {response.status_code}: {response.text}"
                )
                return False

            data = response.json()

            if not data.get("ok", False):
                log.error(f"Telegram API error: {data}")
                return False

            log.debug("Сообщение успешно отправлено в Telegram")
            return True

        except requests.Timeout:
            log.error("Telegram timeout: запрос превысил лимит ожидания")
            return False

        except requests.RequestException as e:
            log.error(f"Ошибка сети при отправке в Telegram: {e}")
            return False

        except ValueError as e:
            log.error(f"Ошибка разбора ответа Telegram: {e}")
            return False

        except Exception as e:
            log.error(f"Неожиданная ошибка TelegramNotifier: {e}")
            return False
