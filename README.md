# GoldenMiner Telegram Bot

Мониторинг майнинга на goldenminer.net (Nockchain pool). Каждый пользователь
подключает свой аккаунт (кош + пароль), бот периодически снимает статистику в
PostgreSQL и по команде показывает срез и доход за день. **Данные видит только
владелец аккаунта.**

## Что собирается
На каждый аккаунт раз в `POLL_MINUTES` пишется снапшот: `mined` (кумулятивно),
`locked`, `transferable`, `today_est`, `local_rate`, `real_rate`, `devices_online`.
Доход за день = `mined` сейчас минус `mined` на конец прошлого дня.

## Команды бота
- `/login` — подключить аккаунт (спросит кош, потом пароль; сообщение с паролем удаляется)
- `/stats` — текущий срез и доход за сегодня
- `/accounts` — список твоих аккаунтов
- `/logout [id]` — удалить аккаунт и все его данные

## Безопасность
- Пароли в БД хранятся зашифрованными (Fernet, ключ `FERNET_KEY` — только в env).
- Все запросы данных фильтруются по `tg_id` владельца (`db.py`).
- Сообщение с паролем удаляется из чата сразу после получения.

## Запуск через Docker (рекомендуется)
1. `cp .env.example .env`
2. Заполни `BOT_TOKEN` (от @BotFather) и сгенерируй `FERNET_KEY`:
   ```
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   `DATABASE_URL` для docker уже задан в compose.
3. `docker compose up -d --build`

## Запуск локально
1. PostgreSQL запущен, БД создана.
2. `pip install -r requirements.txt`
3. `cp .env.example .env` и заполни (`DATABASE_URL` под свой Postgres).
4. `python bot.py` — бот и collector стартуют в одном процессе.

## Структура
- `goldenminer.py` — клиент API (логин JWT, кеш токена, релогин)
- `db.py` — слой PostgreSQL (изоляция по tg_id)
- `crypto.py` — шифрование паролей
- `collector.py` — фоновый сбор снапшотов
- `bot.py` — бот + запуск collector
- `config.py` — конфиг из env

## Дальше (не в этой версии)
Уведомления: дневная сводка, «майнинг встал» (real_rate→0), уход устройств в офлайн.
Под рост — вынести collector в отдельный процесс/контейнер.
