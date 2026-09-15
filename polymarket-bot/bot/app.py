import asyncio
import signal
from datetime import datetime, timezone

import structlog
import uvicorn

from analysis.calibration_tracker import CalibrationTracker
from analysis.edge_detector import EdgeDetector
from analysis.llm_client import LLMClient
from analysis.market_filter import MarketFilter
from analysis.probability_estimator import EnsembleProbabilityEstimator
from bot.circuit_breakers import CircuitBreakerManager
from bot.complete_set_maker import (
    CompleteSetScanner,
    CompleteSetShadowMaker,
    MakerScannerConfig,
)
from bot.health import HealthChecker
from bot.pipeline import TradingPipeline
from bot.reconciler import BalanceReconciler
from bot.spread_capture import SpreadCaptureTrader
from bot.threshold_trader import ThresholdTrader
from bot.weather_trader import WeatherTrader
from config.constants import LLMProvider
from config.logging_config import setup_logging
from config.settings import Settings
from core.event_bus import EventBus
from core.scheduler import BotScheduler
from data.binance_ws import BinanceWSFeed
from data.brave_search import BraveSearchSource
from data.espn_source import ESPNSource
from data.finnhub_source import FinnhubSource
from data.gdelt_source import GdeltSource
from data.google_news_source import GoogleNewsSource
from data.newsapi_source import NewsAPISource
from data.polymarket_rest import PolymarketClient
from data.polymarket_ws import MarketWebSocket
from data.reddit_source import RedditSource
from data.twitter_source import TwitterSource
from notifications.telegram import TelegramNotifier
from research.runner import AutoResearchRunner
from storage.database import close_db, init_db
from storage.repository import (
    DailyStatRepository,
    MarketRepository,
    PredictionRepository,
    RiskEventRepository,
    TradeRepository,
)
from trading.executor import OrderExecutor
from trading.kelly import KellyCalculator
from trading.portfolio import Portfolio, Position
from trading.risk_manager import RiskManager
from web.server import create_app

log = structlog.get_logger()


class Application:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._shutdown_event = asyncio.Event()

    async def run(self) -> None:
        setup_logging(self._settings.log_level)

        mode = "PAPER" if self._settings.paper_trading else "LIVE"
        log.info("bot_starting", mode=mode)

        # Initialize database
        await init_db(self._settings.db_path)

        # Create repositories
        trade_repo = TradeRepository()
        prediction_repo = PredictionRepository()
        daily_stat_repo = DailyStatRepository()
        risk_repo = RiskEventRepository()
        market_repo = MarketRepository()

        # Event bus
        event_bus = EventBus()

        # Polymarket client
        polymarket = PolymarketClient(
            private_key=self._settings.polymarket_private_key,
            chain_id=self._settings.polymarket_chain_id,
            clob_url=self._settings.polymarket_clob_url,
            gamma_url=self._settings.polymarket_gamma_url,
            clob_proxy=self._settings.polymarket_clob_proxy,
            signature_type=self._settings.polymarket_signature_type,
            funder_address=self._settings.polymarket_funder_address,
        )

        if not self._settings.paper_trading:
            # LIVE mode preflight — fail loud before any code spins up that assumes wiring.
            if not self._settings.polymarket_private_key:
                log.error("live_abort_no_private_key",
                          hint="set POLYMARKET_PRIVATE_KEY in .env or switch to paper")
                raise SystemExit(1)
            await polymarket.init_clob()
            if polymarket._clob_client is None:
                log.error("live_abort_clob_init_failed",
                          hint="py_clob_client failed to initialise; check private key + network")
                raise SystemExit(1)
            bal = await polymarket.get_balance()
            if bal < 5.0:
                log.error("live_abort_low_balance", usdc=round(bal, 4),
                          hint="need ≥ $5 USDC on Polygon wallet before going live")
                raise SystemExit(1)
            allow = await polymarket.get_allowance()
            if allow < 1000.0:
                log.error("live_abort_low_allowance", allowance=round(allow, 2),
                          hint="run scripts/setup_approvals.py once to approve USDC + CTF")
                raise SystemExit(1)

            # LIVE GATE: require a validated honest-paper track record before
            # real money is at stake. The -$20/100 loss happened because a
            # fake-oracle paper WR (14/14) justified going live. Gate: ≥30
            # closed paper trades resolved by ACTUAL market outcomes with
            # positive expectancy, or explicit override via LIVE_ACK_NO_PAPER=1.
            import os as _os
            await self._validate_live_readiness(trade_repo, _os)

            log.info("live_preflight_ok", balance_usdc=round(bal, 2),
                     allowance_usdc=round(allow, 2))

        # WebSocket
        ws = MarketWebSocket(self._settings.polymarket_ws_url, event_bus)

        # News sources
        news_sources = self._build_news_sources()

        # LLM client
        llm_client = LLMClient(
            anthropic_key=self._settings.anthropic_api_key,
            openai_key=self._settings.openai_api_key,
            openrouter_key=self._settings.openrouter_api_key,
            primary_model=self._settings.primary_llm_model,
            secondary_model=self._settings.secondary_llm_model,
            primary_provider=self._settings.primary_llm_provider,
            secondary_provider=self._settings.secondary_llm_provider,
        )

        # Determine provider enums
        provider_map = {
            "openrouter": LLMProvider.OPENROUTER,
            "claude": LLMProvider.CLAUDE,
            "openai": LLMProvider.OPENAI,
        }
        primary_prov = provider_map.get(
            self._settings.primary_llm_provider, LLMProvider.OPENROUTER
        )
        secondary_prov = provider_map.get(
            self._settings.secondary_llm_provider, LLMProvider.OPENROUTER
        )

        # Ensemble: use if secondary model differs from primary
        use_ensemble = (
            self._settings.primary_llm_model != self._settings.secondary_llm_model
            or self._settings.primary_llm_provider != self._settings.secondary_llm_provider
        )

        # Calibration tracker (created early so estimator can use it)
        calibration = CalibrationTracker(prediction_repo)

        # Analysis
        estimator = EnsembleProbabilityEstimator(
            llm_client=llm_client,
            bias_correction=self._settings.llm_bias_correction,
            min_edge=self._settings.min_edge,
            min_confidence=self._settings.min_confidence,
            use_ensemble=use_ensemble,
            primary_provider=primary_prov,
            secondary_provider=secondary_prov,
            primary_model=self._settings.primary_llm_model,
            secondary_model=self._settings.secondary_llm_model,
            calibration=calibration,
        )

        edge_detector = EdgeDetector(
            min_edge=self._settings.min_edge,
            min_confidence=self._settings.min_confidence,
        )

        market_filter = MarketFilter(
            min_volume=self._settings.min_market_volume,
            max_days_to_resolution=self._settings.max_days_to_resolution,
            max_hours_to_resolution=self._settings.max_hours_to_resolution,
            top_n=self._settings.top_markets_to_analyze,
        )

        # Trading
        kelly = KellyCalculator(
            default_fraction=self._settings.kelly_fraction,
            max_position_pct=self._settings.max_position_pct,
        )

        risk_manager = RiskManager(
            max_position_pct=self._settings.max_position_pct,
            max_open_positions_small=self._settings.max_open_positions_small,
            max_open_positions_large=self._settings.max_open_positions_large,
            daily_loss_limit=self._settings.daily_loss_limit,
            drawdown_pause=self._settings.drawdown_pause,
            min_edge=self._settings.min_edge,
            min_confidence=self._settings.min_confidence,
            min_market_volume=self._settings.min_market_volume,
            risk_repo=risk_repo,
        )

        # Get initial balance
        if not self._settings.paper_trading:
            balance = await polymarket.get_balance()
            initial_bankroll = balance if balance > 0 else 10.0
        else:
            initial_bankroll = self._settings.paper_bankroll

        portfolio = Portfolio(
            initial_bankroll=initial_bankroll,
            trade_repo=trade_repo,
            daily_stat_repo=daily_stat_repo,
            paper_mode=self._settings.paper_trading,
        )
        await portfolio.sync_from_db(restore_positions=self._settings.paper_trading)

        executor = OrderExecutor(
            polymarket=polymarket,
            trade_repo=trade_repo,
            paper_mode=self._settings.paper_trading,
            realistic_paper_execution=self._settings.realistic_paper_execution,
            # GTC callbacks wired after pipeline is created (see below)
        )

        # Telegram
        telegram = TelegramNotifier(
            bot_token=self._settings.telegram_bot_token,
            chat_id=self._settings.telegram_chat_id,
            polling_enabled=self._settings.telegram_polling_enabled,
        )

        # Wire telegram + risk_manager into executor for H4 alerts/backoff.
        executor._telegram = telegram
        executor._risk_manager = risk_manager

        # Circuit breakers — halt strategies on decay/drawdown/consecutive losses.
        # Traders check is_halted() before each cycle; scheduler runs check_all()
        # every 5 min. State persists to data/breakers_state.json.
        breakers = CircuitBreakerManager(
            portfolio=portfolio,
            trade_repo=trade_repo,
            telegram=telegram,
            state_file=self._settings.breakers_state_file,
            drawdown_pct=self._settings.drawdown_pause,
            daily_loss_pct=self._settings.daily_loss_limit,
            strategies=("complete_set", "weather", "threshold", "spread"),
        )

        maker_scanner = CompleteSetScanner(
            polymarket=polymarket,
            bankroll_getter=lambda: portfolio.bankroll,
            gamma_url=self._settings.polymarket_gamma_url,
            config=MakerScannerConfig(
                quote_shares=self._settings.maker_quote_shares,
                min_gross_edge=self._settings.maker_min_gross_edge,
                max_emergency_loss_usd=self._settings.maker_max_emergency_loss_usd,
                max_market_reserve_pct=self._settings.maker_max_market_reserve_pct,
                max_total_reserve_pct=self._settings.maker_max_total_reserve_pct,
                max_markets=self._settings.maker_max_markets,
                max_resolution_hours=self._settings.maker_max_resolution_hours,
                min_volume_24h=self._settings.maker_min_volume_24h,
                event_pages=self._settings.maker_event_pages,
                scan_interval_seconds=self._settings.maker_scan_interval_seconds,
                request_timeout_seconds=self._settings.maker_request_timeout_seconds,
                max_candidates_per_scan=self._settings.maker_max_candidates_per_scan,
            ),
        )
        complete_set_shadow = CompleteSetShadowMaker(maker_scanner)

        # AutoResearch: autonomous parameter/prompt/strategy optimization
        autoresearch = AutoResearchRunner(
            settings=self._settings,
            portfolio=portfolio,
            calibration=calibration,
            trade_repo=trade_repo,
            market_filter=market_filter,
            cycles_per_experiment=10,  # ~30 min per experiment at 3-min intervals
        )
        # NOTE: autoresearch.start() is called AFTER pipeline is fully created (below)

        # Health checker
        health = HealthChecker(polymarket, news_sources)

        # Pipeline
        pipeline = TradingPipeline(
            settings=self._settings,
            polymarket=polymarket,
            ws=ws,
            news_sources=news_sources,
            estimator=estimator,
            edge_detector=edge_detector,
            kelly=kelly,
            risk_manager=risk_manager,
            executor=executor,
            portfolio=portfolio,
            calibration=calibration,
            telegram=telegram,
            market_filter=market_filter,
            autoresearch=autoresearch,
            event_bus=event_bus,
            market_repo=market_repo,
            health_checker=health,
        )

        # Binance WebSocket — real-time crypto prices (leading indicator)
        binance_ws = BinanceWSFeed()

        # NOTE: btc_trader and copy_trader were removed (moved to attic/).
        # Both were net-negative, paper-oracle-validated, and had no honest
        # live execution path (btc_trader could open fictional live
        # positions; copy_trader had no exit logic at all).

        # Spread capture + last-second — fee-aware, uses Binance WS
        spread_trader = SpreadCaptureTrader(
            portfolio=portfolio,
            binance_feed=binance_ws,
            paper_mode=self._settings.paper_trading,
            trade_repo=trade_repo,
            executor=executor,
            breakers=breakers,
        )

        # Threshold trader — "Will BTC be above $X?" deep liquidity markets
        threshold_trader = ThresholdTrader(
            portfolio=portfolio,
            binance_feed=binance_ws,
            paper_mode=self._settings.paper_trading,
            trade_repo=trade_repo,
            calibration=calibration,
            executor=executor,
            breakers=breakers,
        )

        # Weather trader — temperature/rain markets via Open-Meteo
        weather_trader = WeatherTrader(
            portfolio=portfolio,
            paper_mode=self._settings.paper_trading,
            trade_repo=trade_repo,
            executor=executor,
            llm_client=llm_client,
            breakers=breakers,
        )

        # LIVE safeguard: every enabled trader MUST have executor in live mode.
        if not self._settings.paper_trading:
            for _trader in (threshold_trader, weather_trader, spread_trader):
                if getattr(_trader, "_executor", None) is None:
                    raise RuntimeError(
                        f"LIVE mode but {_trader.__class__.__name__} has no executor — "
                        "refusing to start"
                    )
            await self._sync_live_portfolio(portfolio, polymarket, trade_repo)

        async def _gtc_fill_dispatch(trade_id, fill_price, fill_size_usd, proposal_data):
            metadata = proposal_data.get("metadata") or {}
            strategy = metadata.get("strategy")
            if strategy == "threshold":
                await threshold_trader.on_live_fill(
                    trade_id=trade_id,
                    fill_price=fill_price,
                    fill_size=fill_size_usd,
                    proposal_data=proposal_data,
                )
                return
            if strategy == "weather":
                await weather_trader.on_live_fill(
                    trade_id=trade_id,
                    fill_price=fill_price,
                    fill_size=fill_size_usd,
                    proposal_data=proposal_data,
                )
                return
            await pipeline.on_gtc_fill(trade_id, fill_price, fill_size_usd, proposal_data)

        async def _gtc_cancel_dispatch(trade_id, proposal_data):
            log.info(
                "gtc_order_cancelled_callback",
                trade_id=trade_id,
                market_id=proposal_data.get("market_id"),
                strategy=(proposal_data.get("metadata") or {}).get("strategy"),
            )

        executor._on_gtc_fill = _gtc_fill_dispatch
        executor._on_gtc_cancel = _gtc_cancel_dispatch

        # Start services — autoresearch starts after pipeline so first experiment has valid state
        if self._settings.enable_autoresearch:
            autoresearch.start()
        await telegram.start()
        await ws.start()
        binance_enabled = (
            self._settings.enable_spread_trader
            or self._settings.enable_threshold_trader
        )
        if binance_enabled:
            await binance_ws.start()
        # SPREAD CAPTURE — ARB-ONLY mode (last-second call commented out inside
        # spread_capture.py). Only triggers when Up+Down+fees<$1 → guaranteed
        # profit both legs. Rare but free money when found.
        if self._settings.enable_spread_trader:
            await spread_trader.start()
        if self._settings.enable_threshold_trader:
            await threshold_trader.start()
        # WEATHER — validation phase:
        #  - Paper PnL now resolves via ACTUAL market outcome (Gamma), METAR is
        #    diagnostic-only (was: own-oracle $1/$0 → inflated paper WR)
        #  - Nowcast keeps per-city station bias + residual σ (was: discarded)
        #  - Per-city nowcast timezones (was: hardcoded ET)
        # Circuit breakers + honest paper stats decide if this survives.
        if self._settings.enable_weather_trader:
            await weather_trader.start()
        if self._settings.enable_complete_set_shadow:
            await complete_set_shadow.start()
        await self._refresh_position_marks(portfolio=portfolio, polymarket=polymarket, ws=ws)

        # LIVE mode: start balance reconciler (every 5 min, auto-alert on drift).
        reconciler = None
        if not self._settings.paper_trading:
            reconciler = BalanceReconciler(
                polymarket, portfolio, telegram=telegram, risk_repo=risk_repo
            )
            await reconciler.start()

        # Health check on startup
        health_result = await health.check_all()
        if not health_result["healthy"]:
            log.warning("unhealthy_startup", **health_result)
            await telegram.send_risk_alert(
                "Startup Warning",
                f"Not all systems healthy: {health_result}",
            )

        # Scheduler — main pipeline disabled (31.5% WR, -$3.5K loss)
        # Keep pipeline object alive for health checks and event bus, but don't schedule cycles
        scheduler = BotScheduler()
        # scheduler.add_job(
        #     pipeline.run_cycle,
        #     interval_seconds=self._settings.scan_interval_seconds,
        #     job_id="trading_cycle",
        # )

        # Stats + health: save daily stats and run health checks independently
        async def _stats_and_health():
            pipeline._cycle_count += 1  # Increment cycle for dashboard
            await portfolio.save_daily_stats()
            if pipeline._health_checker and pipeline._cycle_count % 10 == 0:
                await pipeline._run_health_check()

        scheduler.add_job(
            _stats_and_health,
            interval_seconds=180,  # every 3 min, same as pipeline was
            job_id="stats_health",
        )
        async def _job_refresh_positions():
            await self._refresh_position_marks(portfolio=portfolio, polymarket=polymarket, ws=ws)

        scheduler.add_job(
            _job_refresh_positions,
            interval_seconds=60,
            job_id="position_marks",
        )
        # Circuit breakers — every 5 min check drawdown, daily loss, WR, and
        # consecutive losses. Traders enforce halts on their own cycle.
        scheduler.add_job(
            breakers.check_all,
            interval_seconds=300,
            job_id="circuit_breakers",
        )
        scheduler.start()

        await telegram.send(
            f"🚀 <b>Bot Started ({mode} mode)</b>\n"
            f"Bankroll: ${portfolio.bankroll:.2f}\n"
            f"Scan interval: {self._settings.scan_interval_seconds}s\n"
            f"Kelly: {self._settings.kelly_fraction*100:.0f}%\n"
            f"Min edge: {self._settings.min_edge*100:.0f}%\n"
            f"Complete-set maker: "
            f"{'SHADOW' if self._settings.enable_complete_set_shadow else 'OFF'}\n"
            f"AutoResearch: {'ON' if self._settings.enable_autoresearch else 'OFF'}"
        )

        log.info("bot_running", mode=mode, bankroll=portfolio.bankroll)

        # Register autoresearch status handler
        telegram.register_handlers(
            get_status=self._make_async(lambda: portfolio.summary()),
            get_balance=self._make_async(lambda: portfolio.bankroll),
            get_history=lambda: self._format_history(trade_repo),
            get_calibration=lambda: calibration.format_report(),
            get_positions=self._make_async(lambda: self._format_positions(portfolio)),
            get_verify=lambda: self._format_verify(trade_repo),
            pause_fn=self._make_async(lambda: risk_manager.pause(1.0)),
            get_research=lambda: autoresearch.status_report(),
        )

        # Start web dashboard
        web_app = create_app(
            portfolio=portfolio,
            pipeline=pipeline,
            settings=self._settings,
            autoresearch=autoresearch,
            health=health,
        )
        web_config = uvicorn.Config(
            web_app, host="0.0.0.0", port=8050, log_level="warning",
        )
        web_server = uvicorn.Server(web_config)
        web_task = asyncio.create_task(web_server.serve())
        log.info("dashboard_started", url="http://localhost:8050")

        # Wait for shutdown signal
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown_event.set)

        await self._shutdown_event.wait()

        # Graceful shutdown — stop autoresearch first to revert any active experiment
        log.info("bot_shutting_down")
        if reconciler is not None:
            await reconciler.stop()
        if self._settings.enable_complete_set_shadow:
            await complete_set_shadow.stop()
        else:
            await maker_scanner.close()
        if self._settings.enable_weather_trader:
            await weather_trader.stop()
        if self._settings.enable_threshold_trader:
            await threshold_trader.stop()
        if self._settings.enable_spread_trader:
            await spread_trader.stop()
        if binance_enabled:
            await binance_ws.stop()
        scheduler.stop()  # Stop scheduling new cycles before reverting experiments
        if self._settings.enable_autoresearch:
            autoresearch.stop()  # Reverts active param/prompt experiments to safe state
        web_server.should_exit = True
        await web_task
        await ws.stop()
        await executor.cancel_pending_gtc_tasks()
        await telegram.send(f"Bot stopped. Final bankroll: ${portfolio.bankroll:.2f}")
        await telegram.stop()
        await polymarket.close()
        for source in news_sources:
            if hasattr(source, "close"):
                await source.close()
        await close_db()
        log.info("bot_stopped")

    async def _validate_live_readiness(self, trade_repo, os_module) -> None:
        """Live gate: honest-paper track record must exist before real money.

        Counts CLOSED paper trades (all strategies are paper-first). The old
        flow validated on a fabricated oracle WR; this gate requires:
          - ≥30 closed paper trades
          - positive expectancy: sum(pnl) > 0 and win_rate ≥ 40%
        Override: LIVE_ACK_NO_PAPER=1 env var (explicit, logged).
        """
        try:
            closed = await trade_repo.get_closed_trades(paper_mode=True)
        except Exception:
            log.exception("live_gate_query_failed")
            closed = []

        n = len(closed)
        total_pnl = sum((t.pnl or 0.0) for t in closed)
        wins = sum(1 for t in closed if (t.pnl or 0.0) > 0)

        if os_module.environ.get("LIVE_ACK_NO_PAPER") == "1":
            log.warning(
                "live_gate_override_active",
                paper_trades=n,
                paper_pnl=round(total_pnl, 2),
                hint="LIVE_ACK_NO_PAPER=1 — proceeding without paper validation",
            )
            return

        if n < 30:
            log.error(
                "live_abort_no_paper_validation",
                paper_trades=n,
                needed=30,
                hint="Run paper mode until ≥30 trades close via ACTUAL market "
                     "resolutions (honest paper). Or set LIVE_ACK_NO_PAPER=1 "
                     "to override knowingly.",
            )
            raise SystemExit(1)
        if total_pnl <= 0 or (n and wins / n < 0.40):
            log.error(
                "live_abort_paper_negative_expectancy",
                paper_trades=n,
                paper_pnl=round(total_pnl, 2),
                paper_win_rate=round(wins / n, 3) if n else 0.0,
                hint="Paper expectancy is not positive — a live run would "
                     "repeat the -$20/100 loss. Fix the strategy first.",
            )
            raise SystemExit(1)

        log.info(
            "live_gate_passed",
            paper_trades=n,
            paper_pnl=round(total_pnl, 2),
            paper_win_rate=round(wins / n, 3),
        )

    async def _sync_live_portfolio(
        self,
        portfolio: Portfolio,
        polymarket: PolymarketClient,
        trade_repo: TradeRepository,
    ) -> None:
        real_balance = await polymarket.get_balance()
        active_trades = await trade_repo.get_active_entry_trades(paper_mode=False)
        if not active_trades:
            portfolio.restore_live_positions([], real_balance)
            return

        open_orders = await polymarket.get_open_orders()
        open_order_ids = {
            str(o.get("id") or o.get("orderID") or "")
            for o in open_orders
            if (o.get("id") or o.get("orderID"))
        }
        token_ids = sorted({t.token_id for t in active_trades if t.token_id})
        wallet_positions = {
            p["token_id"]: p for p in await polymarket.get_positions(token_ids)
        }

        restored_by_market: dict[str, Position] = {}
        # Wallet shares are only knowable per TOKEN, not per trade — allocate
        # them trade-by-trade (oldest first) so restored positions never
        # exceed what the wallet actually holds. Previously the DB size was
        # restored blindly: a partially-filled order restored at full size
        # made close_live SELL fail forever on insufficient balance.
        wallet_shares_remaining: dict[str, float] = {
            p["token_id"]: float(p.get("shares", 0) or 0)
            for p in wallet_positions.values()
        }
        for trade in sorted(
            active_trades,
            key=lambda t: (
                (
                    t.created_at.replace(tzinfo=timezone.utc)
                    if t.created_at and t.created_at.tzinfo is None
                    else t.created_at
                )
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
        ):
            shares = float(wallet_positions.get(trade.token_id, {}).get("shares", 0) or 0)
            order_id = trade.order_id or ""
            fill_price = trade.price
            fill_size = trade.size

            if shares > 0 or wallet_shares_remaining.get(trade.token_id, 0.0) > 0:
                if trade.status == "open" and order_id:
                    order = await polymarket.get_order(order_id)
                    status = (order.get("status", "") or "").upper()
                    if status in ("MATCHED", "FILLED"):
                        size_matched = float(order.get("size_matched", 0) or 0)
                        fill_price = float(order.get("price", trade.price) or trade.price)
                        fill_size = (
                            size_matched * fill_price
                            if size_matched > 0
                            else trade.size
                        )
                        await trade_repo.update_trade_status(
                            trade.id,
                            status="filled",
                            fill_price=fill_price,
                            fill_size=fill_size,
                        )
                    elif status in ("CANCELLED", "CANCELED"):
                        size_matched = float(order.get("size_matched", 0) or 0)
                        if size_matched > 0:
                            fill_price = float(order.get("price", trade.price) or trade.price)
                            fill_size = size_matched * fill_price
                            await trade_repo.update_trade_status(
                                trade.id, status="filled",
                                fill_price=fill_price, fill_size=fill_size,
                            )
                        else:
                            await trade_repo.update_trade_status(trade.id, status="cancelled")
                            continue
                    else:
                        await trade_repo.update_trade_status(trade.id, status="cancelled")
                        continue

                if fill_price <= 0:
                    continue
                # Cap this trade's position at the shares the wallet still
                # has unassigned (handles partial fills + duplicates).
                remaining = wallet_shares_remaining.get(trade.token_id, 0.0)
                trade_shares = fill_size / fill_price if fill_price > 0 else 0.0
                take_shares = min(remaining, trade_shares)
                if take_shares <= 0:
                    log.warning(
                        "startup_trade_no_wallet_shares",
                        trade_id=trade.id,
                        market_id=trade.market_id,
                        hint="wallet shares already allocated to earlier trades",
                    )
                    continue
                wallet_shares_remaining[trade.token_id] = remaining - take_shares
                take_usd = take_shares * fill_price

                existing = restored_by_market.get(trade.market_id)
                if existing is None:
                    restored_by_market[trade.market_id] = Position(
                        trade_id=trade.id,
                        market_id=trade.market_id,
                        token_id=trade.token_id,
                        question=trade.question,
                        direction=trade.direction,
                        entry_price=fill_price,
                        size=take_usd,
                        current_price=fill_price,
                        opened_at=trade.created_at,
                    )
                elif existing.token_id == trade.token_id:
                    # Multiple entries on the same market (lifetime dedup was
                    # removed) — merge into one Position with a weighted
                    # average entry. Previously these trades were silently
                    # dropped and their shares orphaned.
                    total_usd = existing.size + take_usd
                    total_shares = (
                        existing.size / existing.entry_price + take_shares
                        if existing.entry_price > 0
                        else take_shares
                    )
                    merged_entry = total_usd / total_shares if total_shares > 0 else fill_price
                    restored_by_market[trade.market_id] = Position(
                        trade_id=existing.trade_id,
                        market_id=trade.market_id,
                        token_id=trade.token_id,
                        question=existing.question,
                        direction=existing.direction,
                        entry_price=merged_entry,
                        size=total_usd,
                        current_price=merged_entry,
                        opened_at=existing.opened_at,
                    )
                else:
                    # Same market, different token (e.g. hedged YES+NO) — the
                    # single-position-per-market portfolio cannot represent
                    # this; log loudly rather than corrupt PnL.
                    log.warning(
                        "startup_hedged_same_market_unsupported",
                        market_id=trade.market_id,
                        token_id=trade.token_id,
                        existing_token=existing.token_id,
                    )
                continue

            if trade.status == "open" and order_id:
                if order_id in open_order_ids:
                    cancelled = await polymarket.cancel_order(order_id)
                    await trade_repo.update_trade_status(trade.id, status="cancelled")
                    log.warning(
                        "startup_gtc_cancelled",
                        trade_id=trade.id,
                        market_id=trade.market_id,
                        order_id=order_id,
                        cancelled=cancelled,
                    )
                    continue

                order = await polymarket.get_order(order_id)
                status = (order.get("status", "") or "").upper()
                if status in ("MATCHED", "FILLED"):
                    size_matched = float(order.get("size_matched", 0) or 0)
                    fill_price = float(order.get("price", trade.price) or trade.price)
                    if size_matched > 0:
                        fill_size = size_matched * fill_price
                        await trade_repo.update_trade_status(
                            trade.id,
                            status="filled",
                            fill_price=fill_price,
                            fill_size=fill_size,
                        )
                    else:
                        await trade_repo.update_trade_status(trade.id, status="cancelled")
                else:
                    await trade_repo.update_trade_status(trade.id, status="cancelled")
                continue

            if trade.status == "filled":
                log.warning(
                    "startup_missing_wallet_position",
                    trade_id=trade.id,
                    market_id=trade.market_id,
                    token_id=trade.token_id,
                )

        restored_positions = list(restored_by_market.values())

        portfolio.restore_live_positions(restored_positions, real_balance)
        log.info(
            "live_portfolio_synced",
            open_positions=len(restored_positions),
            active_db_trades=len(active_trades),
            wallet_positions=len(wallet_positions),
            open_orders=len(open_order_ids),
            bankroll=real_balance,
        )

    async def _refresh_position_marks(
        self,
        *,
        portfolio: Portfolio,
        polymarket: PolymarketClient,
        ws: MarketWebSocket,
    ) -> None:
        updated = 0
        for pos in list(portfolio.open_positions):
            current_price = ws.get_price(pos.token_id)
            if current_price is None:
                current_price = await polymarket.get_midpoint(pos.token_id)
            if current_price is None:
                gamma_yes_price = await polymarket.get_market_price_gamma(pos.market_id)
                if gamma_yes_price is not None:
                    is_no_position = "NO" in (pos.direction or "").upper()
                    current_price = 1 - gamma_yes_price if is_no_position else gamma_yes_price
            if current_price is None:
                continue
            portfolio.update_position_price(pos.market_id, float(current_price))
            updated += 1

        if updated:
            log.debug(
                "position_marks_refreshed",
                updated=updated,
                open_positions=portfolio.open_positions_count,
            )

    def _build_news_sources(self) -> list:
        sources = []

        # ESPN sports data — free, no API key, team stats + injuries + recent form
        sources.append(ESPNSource())

        # Google News RSS — free, no API key, no rate limits, most reliable on VPS
        sources.append(GoogleNewsSource())

        # Reddit — always enabled, no API key needed (uses JSON endpoints + PullPush)
        sources.append(RedditSource())

        if self._settings.finnhub_api_key:
            sources.append(FinnhubSource(self._settings.finnhub_api_key))

        if self._settings.brave_api_key:
            sources.append(BraveSearchSource(self._settings.brave_api_key))

        # GDELT: free, no API key needed — global news, 15-min detection
        sources.append(GdeltSource())

        # NewsAPI.org: optional, aggregates 80K+ sources
        if self._settings.newsapi_key:
            sources.append(NewsAPISource(self._settings.newsapi_key))

        # Twitter/X: optional, requires Bearer Token
        if self._settings.twitter_bearer_token:
            sources.append(TwitterSource(self._settings.twitter_bearer_token))

        log.info("news_sources_configured", count=len(sources),
                 sources=[s.name for s in sources])

        return sources

    async def _format_history(self, trade_repo: TradeRepository) -> str:
        trades = await trade_repo.get_recent_trades(10)
        if not trades:
            return "📜 <b>Trade History</b>\n\nНет сделок. Бот ищет возможности..."

        lines = [f"📜 <b>Recent Trades</b> ({len(trades)})\n{'━' * 28}\n"]
        for t in trades:
            emoji = "✅" if t.pnl > 0 else "❌" if t.pnl < 0 else "⏳"
            lines.append(
                f"\n{emoji} <b>{t.question[:40]}</b>\n"
                f"   {t.direction} @ ${t.price:.3f} | Size: ${t.size:.2f}\n"
                f"   Edge: {t.edge * 100:.1f}% | P&L: ${t.pnl:+.2f}\n"
                f"   📅 {t.created_at.strftime('%m/%d %H:%M') if t.created_at else 'N/A'}"
            )
        return "\n".join(lines)

    def _format_positions(self, portfolio) -> list[dict]:
        positions = []
        for p in portfolio.open_positions:
            current_price = p.current_price if p.current_price > 0 else p.entry_price
            shares = p.size / p.entry_price if p.entry_price else 0.0
            pnl = shares * current_price - p.size if shares else 0.0
            positions.append({
                "question": p.question,
                "direction": p.direction,
                "entry": p.entry_price,
                "current": current_price,
                "size": p.size,
                "pnl": pnl,
                "pnl_pct": (pnl / p.size * 100) if p.size else 0.0,
            })
        return positions

    async def _format_verify(self, trade_repo: TradeRepository) -> str:
        trades = await trade_repo.get_recent_trades(5)
        if not trades:
            return (
                "🔍 <b>Verification</b>\n\n"
                "Нет сделок для проверки.\n\n"
                "<i>После первой сделки здесь будут:\n"
                "• Ссылки на рынки Polymarket\n"
                "• Точные цены входа и текущие\n"
                "• Reasoning LLM\n"
                "• Raw timestamps из SQLite</i>"
            )

        lines = [f"🔍 <b>Verification</b> (last {len(trades)})\n{'━' * 28}\n"]
        for t in trades:
            url = f"https://polymarket.com/event/{t.market_id}"
            status_emoji = "🟢" if t.status == "open" else "⚪️"
            lines.append(
                f"\n{status_emoji} <b>{t.question[:45]}</b>\n"
                f"   ID: <code>{t.market_id[:20]}...</code>\n"
                f"   🔗 <a href=\"{url}\">Polymarket</a>\n"
                f"   Entry: ${t.price:.4f} | Market was: ${t.market_price_at_entry:.4f}\n"
                f"   LLM est: {t.estimated_prob:.1%} | Confidence: {t.confidence:.0%}\n"
                f"   Edge: {t.edge * 100:.1f}% | Kelly: {t.kelly_fraction * 100:.1f}%\n"
                f"   DB time: <code>{t.created_at}</code>\n"
                f"   💬 <i>{t.reasoning[:150]}</i>"
            )
        lines.append(
            "\n\n📁 Raw DB: <code>data/trades.db</code>\n"
            "Все цены берутся из Polymarket API"
        )
        return "\n".join(lines)

    @staticmethod
    def _make_async(fn):
        async def wrapper():
            return fn()
        return wrapper


def main():
    app = Application()
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
