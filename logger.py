"""
logger.py - Настройка логирования.

Режимы:
1. Обычный бот:
   - консоль
   - bot.log

2. Оптимизация:
   - только консоль
   - без bot.log
   - это отключается через переменную окружения BOT_DISABLE_FILE_LOGGING=1

Дополнительно:
- в дочерних multiprocessing-процессах file logging тоже отключается,
  чтобы не было WinError 32 на Windows.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import colorlog

_SHARED_CONSOLE_HANDLER: logging.Handler | None = None
_SHARED_FILE_HANDLER: logging.Handler | None = None


def _build_console_handler() -> logging.Handler:
    handler: logging.Handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s%(reset)s",
            datefmt="%H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
        )
    )
    return handler


def _build_file_handler() -> logging.Handler:
    log_path = Path("bot.log")
    handler = RotatingFileHandler(
        log_path,
        maxBytes=10_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    return handler


def _should_use_file_logging() -> bool:
    """
    Логика включения file logging.

    File logging отключается если:
    - выставлен BOT_DISABLE_FILE_LOGGING=1
    - это не MainProcess (дочерний multiprocessing worker)
    """
    if os.getenv("BOT_DISABLE_FILE_LOGGING", "0") == "1":
        return False

    process_name = multiprocessing.current_process().name
    if process_name != "MainProcess":
        return False

    return True


def get_logger(name: str) -> logging.Logger:
    """
    Вернуть логгер.

    В обычном режиме:
    - консоль + файл

    В optimize/worker режиме:
    - только консоль
    """
    global _SHARED_CONSOLE_HANDLER, _SHARED_FILE_HANDLER

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        return logger

    if _SHARED_CONSOLE_HANDLER is None:
        _SHARED_CONSOLE_HANDLER = _build_console_handler()

    logger.addHandler(_SHARED_CONSOLE_HANDLER)

    if _should_use_file_logging():
        if _SHARED_FILE_HANDLER is None:
            _SHARED_FILE_HANDLER = _build_file_handler()
        logger.addHandler(_SHARED_FILE_HANDLER)

    return logger


def get_console_logger(name: str) -> logging.Logger:
    """
    Принудительно только консольный логгер.
    """
    global _SHARED_CONSOLE_HANDLER

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        return logger

    if _SHARED_CONSOLE_HANDLER is None:
        _SHARED_CONSOLE_HANDLER = _build_console_handler()

    logger.addHandler(_SHARED_CONSOLE_HANDLER)
    return logger
