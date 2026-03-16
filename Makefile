# Makefile — удобные команды для разработки
# Использование: make <команда>

.PHONY: install lint format typecheck check fix clean

# Установка зависимостей включая dev-инструменты
install:
	pip install -e ".[dev]"
	pre-commit install

# Линтинг без исправлений (только отчёт)
lint:
	ruff check .

# Форматирование кода
format:
	ruff format .
	ruff check --fix .

# Проверка типов
typecheck:
	mypy . --ignore-missing-imports

# Полная проверка: линтинг + типы (без изменения файлов)
check:
	ruff check .
	ruff format --check .
	mypy . --ignore-missing-imports

# Автоисправление всего что можно
fix:
	ruff check --fix .
	ruff format .

# Очистка кэша
clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
