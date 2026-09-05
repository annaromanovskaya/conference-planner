from aiogram.fsm.state import State, StatesGroup


class TopicFlow(StatesGroup):
    waiting_text = State()
    waiting_confirmation = State()


class SourceFlow(StatesGroup):
    waiting_value = State()
