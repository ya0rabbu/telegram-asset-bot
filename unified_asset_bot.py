"""
BangaliIcon Bot — v5.0 (modular)
=================================
Run locally:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN=...
    python unified_asset_bot.py

Deploy on Render:
    Set TELEGRAM_BOT_TOKEN and ADMIN_CHAT_ID in environment variables.
    RENDER_EXTERNAL_URL is auto-injected by Render.
"""

from __future__ import annotations

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

# ── Handlers ──────────────────────────────────────────────────────────────────
from modules.handlers.commands import (
    start,
    menu_command,
    cancel_command,
    help_command,
    admins_command,
    fivermessage_command,
    addword_command,
    mywords_command,
    delword_command,
    resetwords_command,
    stats_command,
    setlimit_command,
    blockuser_command,
    unblockuser_command,
    mylimit_command,
    requestlimit_command,
    qr_command,
    genpass_command,
    genpin_command,
    compress_command,
    watermark_command,
)
from modules.handlers.callbacks import (
    handle_menu_callback,
    handle_category_callback,
    handle_lummi_size_callback,
    handle_hugeicons_size_callback,
    handle_effect_toolbox_callback,
    handle_fx_toolback_callback,
    handle_fx_convert_callback,
    handle_effect_category_callback,
    handle_effect_back_callback,
    handle_effect_callback,
    handle_aspect_callback,
    handle_cropmode_callback,
    handle_resize_preset_callback,
    handle_resize_custom_callback,
    handle_fx_param_callback,
    handle_fx_param_reset_callback,
    handle_fx_noop_callback,
    handle_translate_lang_callback,
    handle_watermark_position_callback,
    handle_genpass_callback,
    handle_genpin_callback,
    handle_media_download_callback,
    handle_color_callback,
    handle_dev_callback,
    handle_case_callback,
    handle_limit_request_callback,
)
from modules.handlers.messages import (
    handle_photo,
    handle_document,
    handle_sticker,
    handle_gif,
    handle_text,
    on_error,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bangaliicon")


# ── Health check server ───────────────────────────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for Render health checks."""

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args) -> None:  # silence access logs
        pass


def _start_health_server(port: int = 8081) -> None:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health check server started on port %d → /health", port)


# ── Application builder ───────────────────────────────────────────────────────

def build_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app   = Application.builder().token(token).build()

    # ── Commands ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("menu",           menu_command))
    app.add_handler(CommandHandler("cancel",         cancel_command))
    app.add_handler(CommandHandler("help",           help_command))
    app.add_handler(CommandHandler("admins",         admins_command))
    app.add_handler(CommandHandler("FiverMessage",   fivermessage_command))
    app.add_handler(CommandHandler("addword",        addword_command))
    app.add_handler(CommandHandler("mywords",        mywords_command))
    app.add_handler(CommandHandler("delword",        delword_command))
    app.add_handler(CommandHandler("resetwords",     resetwords_command))
    app.add_handler(CommandHandler("stats",          stats_command))
    app.add_handler(CommandHandler("setlimit",       setlimit_command))
    app.add_handler(CommandHandler("blockuser",      blockuser_command))
    app.add_handler(CommandHandler("unblockuser",    unblockuser_command))
    app.add_handler(CommandHandler("mylimit",        mylimit_command))
    app.add_handler(CommandHandler("requestlimit",   requestlimit_command))
    app.add_handler(CommandHandler("qr",             qr_command))
    app.add_handler(CommandHandler("genpass",        genpass_command))
    app.add_handler(CommandHandler("genpin",         genpin_command))
    app.add_handler(CommandHandler("compress",       compress_command))
    app.add_handler(CommandHandler("watermark",      watermark_command))

    # ── Callback queries ──────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(
        handle_effect_toolbox_callback,   pattern=r"^fxtool\|"))
    app.add_handler(CallbackQueryHandler(
        handle_fx_toolback_callback,      pattern=r"^fxtoolback\|"))
    app.add_handler(CallbackQueryHandler(
        handle_fx_convert_callback,       pattern=r"^fxconvert\|"))
    app.add_handler(CallbackQueryHandler(
        handle_aspect_callback,           pattern=r"^fxaspect\|"))
    app.add_handler(CallbackQueryHandler(
        handle_cropmode_callback,         pattern=r"^fxcropmode\|"))
    app.add_handler(CallbackQueryHandler(
        handle_resize_preset_callback,    pattern=r"^fxresize\|"))
    app.add_handler(CallbackQueryHandler(
        handle_resize_custom_callback,    pattern=r"^fxresizecustom\|"))
    app.add_handler(CallbackQueryHandler(
        handle_fx_param_callback,         pattern=r"^fxparam\|"))
    app.add_handler(CallbackQueryHandler(
        handle_fx_param_reset_callback,   pattern=r"^fxparamreset\|"))
    app.add_handler(CallbackQueryHandler(
        handle_fx_noop_callback,          pattern=r"^fxnoop$"))
    app.add_handler(CallbackQueryHandler(
        handle_effect_category_callback,  pattern=r"^fxcat\|"))
    app.add_handler(CallbackQueryHandler(
        handle_effect_back_callback,      pattern=r"^fxback\|"))
    app.add_handler(CallbackQueryHandler(
        handle_effect_callback,           pattern=r"^fx\|"))
    app.add_handler(CallbackQueryHandler(
        handle_lummi_size_callback,       pattern=r"^lmsz\|"))
    app.add_handler(CallbackQueryHandler(
        handle_hugeicons_size_callback,   pattern=r"^hisz\|"))
    app.add_handler(CallbackQueryHandler(
        handle_media_download_callback,   pattern=r"^mediadl\|"))
    app.add_handler(CallbackQueryHandler(
        handle_menu_callback,             pattern=r"^menu\|"))
    app.add_handler(CallbackQueryHandler(
        handle_category_callback,         pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(
        handle_color_callback,            pattern=r"^color\|"))
    app.add_handler(CallbackQueryHandler(
        handle_case_callback,             pattern=r"^case\|"))
    app.add_handler(CallbackQueryHandler(
        handle_dev_callback,              pattern=r"^dev\|"))
    app.add_handler(CallbackQueryHandler(
        handle_translate_lang_callback,   pattern=r"^trlang\|"))
    app.add_handler(CallbackQueryHandler(
        handle_watermark_position_callback, pattern=r"^wmpos\|"))
    app.add_handler(CallbackQueryHandler(
        handle_genpass_callback,          pattern=r"^genpass\|"))
    app.add_handler(CallbackQueryHandler(
        handle_genpin_callback,           pattern=r"^genpin\|"))
    app.add_handler(CallbackQueryHandler(
        handle_limit_request_callback,    pattern=r"^limitreq\|"))

    # ── Message handlers ──────────────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.PHOTO,                    handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL,             handle_document))
    app.add_handler(MessageHandler(filters.Sticker.ALL,              handle_sticker))
    app.add_handler(MessageHandler(filters.ANIMATION,                handle_gif))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,                             handle_text))

    # ── Error handler ─────────────────────────────────────────────────────────
    app.add_error_handler(on_error)

    return app


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # Start health check server (background thread, Render needs this)
    _start_health_server(port=8081)

    app = build_application()

    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    bot_token  = os.environ["TELEGRAM_BOT_TOKEN"]
    port       = int(os.environ.get("PORT", 8080))

    if render_url:
        # ── Webhook mode (Render deployment) ──────────────────────────────
        webhook_url = f"{render_url}/{bot_token}"
        logger.info("Starting in WEBHOOK mode")
        logger.info("Webhook URL → %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=bot_token,
            webhook_url=webhook_url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
    else:
        # ── Polling mode (local development) ─────────────────────────────
        logger.info("RENDER_EXTERNAL_URL not set — starting in POLLING mode")
        app.run_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    main()
