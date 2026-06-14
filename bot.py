import logging
import os
import asyncio
import threading
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from telegram.request import HTTPXRequest
from config import BOT_TOKEN
from handlers import (
    start, get_username, pick_letter, handle_range_page, get_quantity, cancel,
    USERNAME, LETTER, RANGE_PAGE, QUANTITY,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

RENDER_URL = os.environ.get("RENDER_URL", "https://lamix-tg-bot.onrender.com")


# ── HTTP server (keeps Render awake) ─────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP access logs


def run_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server started on port %s", port)
    server.serve_forever()


# ── Self-ping (prevents Render free tier sleep) ───────────────────────────────
def keep_alive():
    time.sleep(30)  # Wait for server to start
    while True:
        try:
            requests.get(RENDER_URL, timeout=10)
            logger.info("Keep-alive ping sent")
        except Exception as e:
            logger.warning("Keep-alive ping failed: %s", e)
        time.sleep(180)  # Ping every 3 minutes


# ── Bot ───────────────────────────────────────────────────────────────────────
def build_app() -> Application:
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            USERNAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_username)],
            LETTER:     [MessageHandler(filters.TEXT & ~filters.COMMAND, pick_letter)],
            RANGE_PAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_range_page)],
            QUANTITY:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_quantity)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    return app


async def run_bot():
    app = build_app()
    async with app:
        await app.start()
        logger.info("✅ Bot started!")
        await app.updater.start_polling(drop_pending_updates=True)
        # Run forever until stopped
        await asyncio.Event().wait()


def main():
    while True:
        try:
            asyncio.run(run_bot())
        except Exception as e:
            logger.error("Bot crashed: %s — restarting in 5s", e)
            time.sleep(5)


if __name__ == "__main__":
    # Start HTTP health server in background
    threading.Thread(target=run_server, daemon=True).start()
    # Start keep-alive pinger in background
    threading.Thread(target=keep_alive, daemon=True).start()
    # Run bot (with auto-restart on crash)
    main()
