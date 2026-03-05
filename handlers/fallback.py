from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

router = Router(name="fallback")


@router.message(F.text | F.photo | F.document | F.audio | F.voice | F.video | F.sticker)
async def fallback_message_handler(message: Message, state: FSMContext) -> None:
    if message.text and message.text.strip().startswith("/"):
        return
    if await state.get_state():
        return
    await message.answer("Xizmat tanlash uchun /menu ni bosing.")
