"""
Telegram bot handlers, split by responsibility:
schedule.py — scheduling FSM, reschedule, cancel, autoqueue
manual.py    — manual-placement badges, channel-message deletion
admin.py     — status/queue/mute/help commands

Aggregated here into a single `router` for main.py.
"""
from aiogram import Router

from . import admin, manual, schedule

router = Router()
router.include_router(schedule.router)
router.include_router(manual.router)
router.include_router(admin.router)
