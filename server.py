import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

import bot as bot_module

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = (os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/") + WEBHOOK_PATH

telegram_bot = None
dispatcher = None


async def handle_webhook(request):
    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": telegram_bot})
        await dispatcher.feed_update(telegram_bot, update)
    except Exception as exc:
        logging.exception("Webhook error: %s", exc)
    return web.Response(text="ok")


async def health(request):
    return web.Response(text="OK")


async def main():
    global telegram_bot, dispatcher

    logging.basicConfig(level=logging.INFO)
    bot_module.init_db()
    bot_module.init_subscriptions()

    telegram_bot = Bot(token=bot_module.TOKEN, session=bot_module.session)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(bot_module.router)
    await bot_module.set_commands(telegram_bot)
    asyncio.create_task(bot_module.reminder_loop(telegram_bot))

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post(WEBHOOK_PATH, handle_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
    await site.start()

    await telegram_bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logging.info("Webhook set: %s", WEBHOOK_URL)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())