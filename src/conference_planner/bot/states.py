from aiogram.fsm.state import State, StatesGroup


class TopicFlow(StatesGroup):
    waiting_text = State()
    waiting_confirmation = State()


class SourceFlow(StatesGroup):
    waiting_value = State()


class SearchFlow(StatesGroup):
    waiting_query = State()


class ModelFlow(StatesGroup):
    choosing_profile = State()
    choosing_model = State()


class TestReminderFlow(StatesGroup):
    waiting_name = State()
    waiting_type = State()
    waiting_deadline = State()
    waiting_send_at = State()
    waiting_confirmation = State()
