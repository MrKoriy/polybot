import asyncio
from datetime import datetime

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

log = structlog.get_logger()

# Main menu keyboard
MAIN_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📊 Status", callback_data="status"),
        InlineKeyboardButton("💰 Balance", callback_data="balance"),
    ],
    [
        InlineKeyboardButton("📈 Positions", callback_data="positions"),
        InlineKeyboardButton("📜 History", callback_data="history"),
    ],
    [
        InlineKeyboardButton("🎯 Calibration", callback_data="calibration"),
        InlineKeyboardButton("🔍 Verify", callback_data="verify"),
    ],
    [
        InlineKeyboardButton("🔬 Research", callback_data="research"),
        InlineKeyboardButton("⏸ Pause", callback_data="pause"),
    ],
    [
        InlineKeyboardButton("❓ Help", callback_data="help"),
    ],
])

BACK_BUTTON = InlineKeyboardMarkup([
    [InlineKeyboardButton("◀️ Menu", callback_data="menu")],
])


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, polling_enabled: bool = False) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._polling_enabled = polling_enabled
        self._bot: Bot | None = None
        self._app: Application | None = None
        # Callback functions registered from app.py
        self._get_status_fn = None
        self._get_balance_fn = None
        self._get_history_fn = None
        self._get_calibration_fn = None
        self._get_positions_fn = None
        self._get_verify_fn = None
        self._pause_fn = None
        self._get_research_fn = None

    def register_handlers(
        self,
        get_status=None,
        get_balance=None,
        get_history=None,
        get_calibration=None,
        get_positions=None,
        get_verify=None,
        pause_fn=None,
        get_research=None,
    ) -> None:
        self._get_status_fn = get_status
        self._get_balance_fn = get_balance
        self._get_history_fn = get_history
        self._get_calibration_fn = get_calibration
        self._get_positions_fn = get_positions
        self._get_verify_fn = get_verify
        self._pause_fn = pause_fn
        self._get_research_fn = get_research

    async def start(self) -> None:
        if not self._token:
            log.warning("telegram_disabled", reason="no bot token")
            return

        if not self._polling_enabled:
            self._bot = Bot(self._token)
            await self._bot.initialize()
            log.info("telegram_started", polling_enabled=False)
            return

        self._app = Application.builder().token(self._token).build()
        self._bot = self._app.bot

        # Command handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("menu", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("balance", self._cmd_balance))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("history", self._cmd_history))
        self._app.add_handler(CommandHandler("calibration", self._cmd_calibration))
        self._app.add_handler(CommandHandler("verify", self._cmd_verify))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("help", self._cmd_help))

        # Inline button handler
        self._app.add_handler(CallbackQueryHandler(self._button_handler))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram_started", polling_enabled=True)

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        elif self._bot:
            await self._bot.shutdown()

    async def send(self, text: str, parse_mode: str = "HTML", reply_markup=None) -> None:
        if not self._bot or not self._chat_id:
            log.debug("telegram_skip", reason="not configured")
            return
        try:
            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated)"
            await self._bot.send_message(
                chat_id=self._chat_id, text=text,
                parse_mode=parse_mode, reply_markup=reply_markup,
            )
        except Exception:
            log.exception("telegram_send_error")

    # ── Notification methods (called by pipeline) ──

    async def send_trade_alert(self, trade_info: dict) -> None:
        mode = "PAPER" if trade_info.get("paper_mode", True) else "LIVE"
        mode_emoji = "📝" if mode == "PAPER" else "💰"
        direction = trade_info.get("direction", "N/A")
        dir_emoji = "🟢" if direction == "BUY_YES" else "🔴"

        market_id = trade_info.get("market_id", "")
        polymarket_url = f"https://polymarket.com/event/{market_id}" if market_id else ""
        link_line = f'\n🔗 <a href="{polymarket_url}">View on Polymarket</a>' if polymarket_url else ""

        text = (
            f"{mode_emoji} <b>New Trade ({mode})</b>\n"
            f"{'━' * 28}\n\n"
            f"📌 <b>{trade_info.get('question', 'N/A')}</b>\n\n"
            f"{dir_emoji} Direction: <code>{direction}</code>\n"
            f"💵 Price: <code>${trade_info.get('price', 0):.3f}</code>\n"
            f"📦 Size: <code>${trade_info.get('size', 0):.2f}</code>\n"
            f"📐 Edge: <code>{trade_info.get('edge', 0) * 100:.1f}%</code>\n"
            f"🎯 Confidence: <code>{trade_info.get('confidence', 0) * 100:.0f}%</code>\n"
            f"📊 Kelly: <code>{trade_info.get('kelly_fraction', 0) * 100:.1f}%</code>\n"
            f"{link_line}\n\n"
            f"💬 <i>{trade_info.get('reasoning', '')[:300]}</i>"
        )
        await self.send(text, reply_markup=BACK_BUTTON)

    async def send_status_report(self, status: dict) -> None:
        mode = "PAPER" if status.get("paper_mode", True) else "LIVE"
        bankroll = status.get("bankroll", 0)
        peak = status.get("peak_bankroll", 0)
        daily_pnl = status.get("daily_pnl", 0)
        win_rate = status.get("win_rate", 0) * 100
        drawdown = (1 - bankroll / peak) * 100 if peak > 0 else 0

        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        wr_emoji = "🟢" if win_rate >= 55 else "🟡" if win_rate >= 45 else "🔴"

        # Progress bar for bankroll goal ($10 → $1000)
        progress = min(bankroll / 1000 * 100, 100)
        bar_filled = int(progress / 5)
        bar = "▓" * bar_filled + "░" * (20 - bar_filled)

        text = (
            f"📊 <b>Status Report</b> ({mode})\n"
            f"{'━' * 28}\n\n"
            f"💰 Bankroll: <code>${bankroll:.2f}</code>\n"
            f"🏔 Peak: <code>${peak:.2f}</code>\n"
            f"{pnl_emoji} Daily P&L: <code>${daily_pnl:+.2f}</code>\n"
            f"📉 Drawdown: <code>{drawdown:.1f}%</code>\n\n"
            f"📈 <b>Performance</b>\n"
            f"{wr_emoji} Win rate: <code>{win_rate:.0f}%</code>\n"
            f"🎲 Trades: <code>{status.get('total_trades', 0)}</code> "
            f"({status.get('wins', 0)}W / {status.get('losses', 0)}L)\n"
            f"📂 Open positions: <code>{status.get('open_positions', 0)}</code>\n\n"
            f"🎯 <b>Goal: $10 → $1,000</b>\n"
            f"[{bar}] {progress:.1f}%"
        )
        await self.send(text, reply_markup=BACK_BUTTON)

    async def send_risk_alert(self, event_type: str, details: str) -> None:
        text = (
            f"⚠️ <b>Risk Alert: {event_type}</b>\n"
            f"{'━' * 28}\n\n"
            f"{details}"
        )
        await self.send(text)

    # ── Button handler ──

    async def _button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        action = query.data
        handlers = {
            "menu": self._show_menu,
            "status": self._show_status,
            "balance": self._show_balance,
            "positions": self._show_positions,
            "history": self._show_history,
            "calibration": self._show_calibration,
            "verify": self._show_verify,
            "research": self._show_research,
            "pause": self._show_pause,
            "help": self._show_help,
        }

        handler = handlers.get(action)
        if handler:
            await handler(query)

    async def _show_menu(self, query) -> None:
        await query.edit_message_text(
            "🤖 <b>Polymarket Bot</b>\n\nВыбери действие:",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )

    async def _show_status(self, query) -> None:
        if self._get_status_fn:
            status = await self._get_status_fn()
            mode = "PAPER" if status.get("paper_mode", True) else "LIVE"
            bankroll = status.get("bankroll", 0)
            peak = status.get("peak_bankroll", 0)
            daily_pnl = status.get("daily_pnl", 0)
            win_rate = status.get("win_rate", 0) * 100
            drawdown = (1 - bankroll / peak) * 100 if peak > 0 else 0
            pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
            progress = min(bankroll / 1000 * 100, 100)
            bar_filled = int(progress / 5)
            bar = "▓" * bar_filled + "░" * (20 - bar_filled)

            text = (
                f"📊 <b>Status</b> ({mode})\n"
                f"{'━' * 28}\n\n"
                f"💰 Bankroll: <code>${bankroll:.2f}</code>\n"
                f"🏔 Peak: <code>${peak:.2f}</code>\n"
                f"{pnl_emoji} Daily P&L: <code>${daily_pnl:+.2f}</code>\n"
                f"📉 Drawdown: <code>{drawdown:.1f}%</code>\n\n"
                f"🎲 Trades: {status.get('total_trades', 0)} "
                f"({status.get('wins', 0)}W/{status.get('losses', 0)}L) | "
                f"WR: {win_rate:.0f}%\n"
                f"📂 Open: {status.get('open_positions', 0)}\n\n"
                f"🎯 [{bar}] {progress:.1f}%"
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=BACK_BUTTON)
        else:
            await query.edit_message_text("Status not available.", reply_markup=BACK_BUTTON)

    async def _show_balance(self, query) -> None:
        if self._get_balance_fn:
            balance = await self._get_balance_fn()
            text = (
                f"💰 <b>Balance</b>\n"
                f"{'━' * 28}\n\n"
                f"Available: <code>${balance:.2f}</code> USDC"
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=BACK_BUTTON)

    async def _show_positions(self, query) -> None:
        if self._get_positions_fn:
            positions = await self._get_positions_fn()
            if not positions:
                await query.edit_message_text(
                    "📈 <b>Open Positions</b>\n\nНет открытых позиций.",
                    parse_mode="HTML", reply_markup=BACK_BUTTON,
                )
                return

            lines = [f"📈 <b>Open Positions</b> ({len(positions)})\n{'━' * 28}\n"]
            for p in positions:
                pnl = p.get("pnl", 0)
                pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"\n📌 <b>{p['question'][:50]}</b>\n"
                    f"   {p['direction']} @ ${p['entry']:.3f} → ${p['current']:.3f}\n"
                    f"   {pnl_emoji} P&L: ${pnl:+.2f} ({p.get('pnl_pct', 0):+.1f}%)\n"
                    f"   Size: ${p.get('size', 0):.2f}"
                )
            await query.edit_message_text(
                "\n".join(lines), parse_mode="HTML", reply_markup=BACK_BUTTON,
            )
        else:
            await query.edit_message_text("Positions not available.", reply_markup=BACK_BUTTON)

    async def _show_history(self, query) -> None:
        if self._get_history_fn:
            history = await self._get_history_fn()
            await query.edit_message_text(history, parse_mode="HTML", reply_markup=BACK_BUTTON)
        else:
            await query.edit_message_text("History not available.", reply_markup=BACK_BUTTON)

    async def _show_calibration(self, query) -> None:
        if self._get_calibration_fn:
            cal = await self._get_calibration_fn()
            await query.edit_message_text(cal, parse_mode="HTML", reply_markup=BACK_BUTTON)
        else:
            await query.edit_message_text("Calibration not available.", reply_markup=BACK_BUTTON)

    async def _show_verify(self, query) -> None:
        if self._get_verify_fn:
            verify_text = await self._get_verify_fn()
            await query.edit_message_text(verify_text, parse_mode="HTML", reply_markup=BACK_BUTTON)
        else:
            text = (
                f"🔍 <b>Verification</b>\n"
                f"{'━' * 28}\n\n"
                f"Данные пока недоступны.\n"
                f"После первой сделки здесь будут:\n"
                f"• Ссылки на рынки Polymarket\n"
                f"• Точные цены на момент входа\n"
                f"• Reasoning LLM\n"
                f"• Timestamps из БД"
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=BACK_BUTTON)

    async def _show_research(self, query) -> None:
        if self._get_research_fn:
            report = await self._get_research_fn() if asyncio.iscoroutinefunction(self._get_research_fn) else self._get_research_fn()
            await query.edit_message_text(report, parse_mode="HTML", reply_markup=BACK_BUTTON)
        else:
            await query.edit_message_text(
                "🔬 AutoResearch not available.", parse_mode="HTML", reply_markup=BACK_BUTTON,
            )

    async def _show_pause(self, query) -> None:
        if self._pause_fn:
            await self._pause_fn()
            await query.edit_message_text(
                "⏸ Bot paused for 1 hour.", parse_mode="HTML", reply_markup=BACK_BUTTON,
            )

    async def _show_help(self, query) -> None:
        text = (
            "❓ <b>Help</b>\n"
            f"{'━' * 28}\n\n"
            "<b>Commands:</b>\n"
            "/menu — Main menu with buttons\n"
            "/status — Bankroll, P&L, performance\n"
            "/balance — Available USDC\n"
            "/positions — Open positions\n"
            "/history — Recent trades\n"
            "/calibration — Prediction accuracy\n"
            "/verify — Verify trades (links + raw data)\n"
            "/pause — Pause trading 1 hour\n\n"
            "<b>Как верифицировать:</b>\n"
            "Каждая сделка содержит ссылку на рынок\n"
            "Polymarket — ты можешь проверить цену\n"
            "в реальном времени. Все данные пишутся\n"
            "в SQLite БД: <code>data/trades.db</code>"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=BACK_BUTTON)

    # ── Command handlers (fallback for text commands) ──

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🤖 <b>Polymarket Bot</b>\n\nВыбери действие:",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._get_status_fn:
            status = await self._get_status_fn()
            await self.send_status_report(status)
        else:
            await update.message.reply_text("Status not available yet.")

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._get_balance_fn:
            balance = await self._get_balance_fn()
            await update.message.reply_text(
                f"💰 Balance: <code>${balance:.2f}</code> USDC",
                parse_mode="HTML",
            )

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._get_positions_fn:
            positions = await self._get_positions_fn()
            if not positions:
                await update.message.reply_text("No open positions.")
                return
            lines = [f"📈 <b>Open Positions</b> ({len(positions)})\n"]
            for p in positions:
                pnl = p.get("pnl", 0)
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"{emoji} {p['question'][:45]}\n"
                    f"   {p['direction']} ${p['entry']:.3f}→${p['current']:.3f} "
                    f"P&L: ${pnl:+.2f}"
                )
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._get_history_fn:
            history = await self._get_history_fn()
            await update.message.reply_text(history, parse_mode="HTML")

    async def _cmd_calibration(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._get_calibration_fn:
            cal = await self._get_calibration_fn()
            await update.message.reply_text(cal, parse_mode="HTML")

    async def _cmd_verify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._get_verify_fn:
            text = await self._get_verify_fn()
            await update.message.reply_text(text, parse_mode="HTML")
        else:
            await update.message.reply_text("No trades to verify yet.")

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._pause_fn:
            await self._pause_fn()
            await update.message.reply_text("⏸ Bot paused for 1 hour.")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Send /menu for the main menu with buttons.",
            parse_mode="HTML",
        )
