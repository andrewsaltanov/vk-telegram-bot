from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import (
    CancelSchedCallback,
    DelChannelCallback,
    ManualDoneCallback,
    ManualInfoCallback,
    ScheduleCallback,
    SchedInfoCallback,
)

# Schedule options: (label, minutes)
SCHEDULE_OPTIONS = [
    ("30 мин",     30),
    ("1 ч",        60),
    ("1.5 ч",      90),
    ("2 ч",        120),
    ("2.5 ч",      150),
    ("3 ч",        180),
    ("3.5 ч",      210),
    ("4 ч",        240),
    ("4.5 ч",      270),
    ("5 ч",        300),
]


def _schedule_rows(builder: InlineKeyboardBuilder, post_db_id: int) -> None:
    """Add schedule time option buttons (4 per row) and a custom-time row."""
    # Full-width «Сейчас» button first
    builder.row(
        InlineKeyboardButton(
            text="⚡️ Опубликовать сейчас",
            callback_data=ScheduleCallback(post_db_id=post_db_id, option="0").pack(),
        )
    )
    # Time-grid: 4 per row
    row: list[InlineKeyboardButton] = []
    for label, minutes in SCHEDULE_OPTIONS:
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=ScheduleCallback(
                    post_db_id=post_db_id, option=str(minutes)
                ).pack(),
            )
        )
        if len(row) == 4:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(
            text="🕐 Своё время",
            callback_data=ScheduleCallback(
                post_db_id=post_db_id, option="custom"
            ).pack(),
        )
    )


def get_schedule_keyboard(post_db_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _schedule_rows(builder, post_db_id)
    return builder.as_markup()


def get_manual_post_keyboard(post_db_id: int) -> InlineKeyboardMarkup:
    """Клавиатура расписания + кнопка 'Размещён вручную' для oversized-постов."""
    builder = InlineKeyboardBuilder()
    _schedule_rows(builder, post_db_id)
    builder.row(
        InlineKeyboardButton(
            text="🔲 Размещён вручную",
            callback_data=ManualDoneCallback(post_db_id=post_db_id).pack(),
        )
    )
    return builder.as_markup()


def get_manually_placed_badge(
    post_db_id: int, unix_ts: int, timezone: str = "Europe/Moscow"
) -> InlineKeyboardMarkup:
    """Бейдж ручного размещения; при нажатии показывает алерт с датой."""
    dt = datetime.fromtimestamp(unix_ts, tz=ZoneInfo(timezone))
    time_str = dt.strftime("%d.%m %H:%M")
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Размещён вручную {time_str}",
        callback_data=ManualInfoCallback(post_db_id=post_db_id, unix_ts=unix_ts).pack(),
    )
    return builder.as_markup()


def get_scheduled_badge(
    post_db_id: int,
    unix_ts: int,
    timezone: str = "Europe/Moscow",
    record_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Бейдж запланированной публикации; опционально — кнопка отмены."""
    dt = datetime.fromtimestamp(unix_ts, tz=ZoneInfo(timezone))
    time_str = dt.strftime("%d.%m %H:%M")
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Запланировано на {time_str}",
        callback_data=SchedInfoCallback(post_db_id=post_db_id, unix_ts=unix_ts).pack(),
    )
    if record_id is not None:
        builder.button(
            text="❌ Отменить публикацию",
            callback_data=CancelSchedCallback(record_id=record_id).pack(),
        )
        builder.adjust(1)
    return builder.as_markup()


def get_published_notification_keyboard(record_id: int) -> InlineKeyboardMarkup:
    """Клавиатура уведомления о публикации с кнопкой удаления из канала."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🗑 Удалить из канала",
        callback_data=DelChannelCallback(record_id=record_id).pack(),
    )
    return builder.as_markup()
