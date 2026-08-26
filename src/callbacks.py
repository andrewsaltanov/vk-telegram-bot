"""
Typed CallbackData classes for all inline keyboard callbacks.
Centralises callback data structure so keyboards and handlers share the same types.
"""
from aiogram.filters.callback_data import CallbackData


class ScheduleCallback(CallbackData, prefix="sched"):
    post_db_id: int
    option: str  # minutes as string (e.g. "0", "30") or "custom"


class ManualDoneCallback(CallbackData, prefix="manual_done"):
    post_db_id: int


class ManualInfoCallback(CallbackData, prefix="manual_info"):
    post_db_id: int
    unix_ts: int


class SchedInfoCallback(CallbackData, prefix="sched_info"):
    post_db_id: int
    unix_ts: int


class CancelSchedCallback(CallbackData, prefix="cancel_sched"):
    record_id: int


class DelChannelCallback(CallbackData, prefix="del_channel"):
    record_id: int


class RescheduleCallback(CallbackData, prefix="reschedule"):
    post_db_id: int
    record_id: int
