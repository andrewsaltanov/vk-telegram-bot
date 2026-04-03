# VK → Telegram Bot

Бот синхронизирует посты из VK-сообществ в Telegram-супергруппу с форум-топиками.  
Администраторы могут планировать публикацию постов в Telegram-каналы прямо из группы.

## Что умеет

- **Поллинг VK** — отслеживает новые посты на стене и в предложке каждые N секунд
- **Telegram-топики** — для каждого сообщества автоматически создаётся отдельный форум-топик
- **Планировщик** — кнопки под постом позволяют запланировать публикацию в канал (через 30 мин, 1 ч, … или своё время)
- **Уведомления** — после публикации в канал бот присылает ссылку на пост + кнопку удаления
- **Дедупликация** — пост из предложки, одобренный и опубликованный на стене, не отправляется дважды
- **Синхронизация удалений** — если пост удалён из VK, сообщение в Telegram тоже удаляется
- **Несколько сообществ** — каждое сообщество настраивается отдельно (свой токен, свой канал)

## Стек

- Python 3.11+
- [aiogram](https://github.com/aiogram/aiogram) 3.7 — Telegram Bot API
- [aiosqlite](https://github.com/omnilib/aiosqlite) — асинхронный SQLite
- [APScheduler](https://apscheduler.readthedocs.io/) 3.x — планировщик задач
- [aiohttp](https://docs.aiohttp.org/) — HTTP-клиент для VK API
- Docker + Docker Compose

## Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone https://github.com/andrewsaltanov/vk-telegram-bot.git
cd vk-telegram-bot
```

### 2. Создать `.env`

```bash
cp .env.example .env
# Заполнить значения (см. раздел Конфигурация)
```

### 3. Создать `communities.json`

```json
[
  {
    "group_id": 123456789,
    "name": "Моё сообщество",
    "token": "vk_community_token",
    "channel_id": -1001234567890,
    "user_token": "vk_user_admin_token"
  }
]
```

> ⚠️ **`communities.json` содержит токены VK. Не коммитить в публичный репозиторий.**

### 4. Запустить

```bash
docker compose up -d
docker compose logs -f bot
```

### Локально (без Docker)

```bash
pip install -r requirements.txt
python src/main.py
```

## Конфигурация

### `.env`

| Переменная | Описание |
|------------|----------|
| `BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather) |
| `GROUP_ID` | ID супергруппы Telegram (бот — администратор с `manage_topics`) |
| `ADMIN_IDS` | ID администраторов бота через запятую |
| `POLL_INTERVAL` | Интервал опроса VK в секундах (по умолчанию `300`) |
| `INITIAL_POSTS_COUNT` | Сколько постов загрузить при первом запуске (по умолчанию `10`) |
| `TIMEZONE` | Часовой пояс для планировщика (например `Europe/Moscow`) |

### `communities.json`

| Поле | Описание |
|------|----------|
| `group_id` | ID VK-сообщества (без минуса) |
| `name` | Отображаемое имя |
| `token` | Токен сообщества VK |
| `channel_id` | ID Telegram-канала для публикации |
| `user_token` | Токен пользователя-администратора VK (нужен для чтения стены и предложки). Получить: [vkhost.github.io](https://vkhost.github.io/) → `wall` + `groups` |

## Архитектура

```
src/
├── main.py          # Точка входа: запуск бота, БД, поллера, планировщика
├── config.py        # Конфиг из .env + communities.json
├── database.py      # Async SQLite (aiosqlite), 3 таблицы
├── vk_client.py     # VK API v5.131, dual-token, retry при rate limit
├── poller.py        # Цикл опроса VK, детект удалений
├── post_sender.py   # Форматирование и отправка постов в Telegram
├── scheduler.py     # APScheduler: запуск запланированных публикаций
├── handlers.py      # Callback-хендлеры (планирование, отмена, удаление)
├── keyboards.py     # Inline-клавиатуры
├── callbacks.py     # CallbackData-классы (aiogram best practice)
└── setup.py         # Создание форум-топиков при первом запуске
```

### Поток данных

```
VK wall/suggests
       │  (каждые POLL_INTERVAL сек)
       ▼
   poller.py ──► post_sender.py ──► Telegram GROUP topic
       │                                    │
       │                              inline keyboard
       │                                    │
       │                            handlers.py (_do_schedule)
       │                                    │
       │                            scheduler.py (APScheduler)
       │                                    │
       └── deletion check ──────────────────►  Telegram CHANNEL
```

### База данных (SQLite)

| Таблица | Назначение |
|---------|-----------|
| `communities` | VK-сообщество ↔ Telegram-топики, last_post_id |
| `posts` | Синхронизированные посты с ID Telegram-сообщений |
| `scheduled_channel_posts` | Запланированные публикации в канал |

## Права бота

**Telegram-группа:** администратор с правами `manage_topics`, `send_messages`, `delete_messages`

**Telegram-канал:** администратор с правами `send_messages`, `delete_messages`

## Лицензия

MIT
