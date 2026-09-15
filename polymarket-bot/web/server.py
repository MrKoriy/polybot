import asyncio
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Query, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from storage.database import get_session
from storage.models import DailyStat, Market, Prediction, RiskEvent, Trade
from sqlalchemy import select, func

log = structlog.get_logger()

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

security = HTTPBasic(auto_error=False)

_WEATHER_TARGET_RE = re.compile(r"target=(?P<date>\d{4}-\d{2}-\d{2})")
_THRESH_END_RE = re.compile(r"end=(?P<end>[0-9T:\-+:.Z]+)")


def _short_question(question: str, limit: int = 80) -> str:
    question = question or ""
    if len(question) <= limit:
        return question
    return question[: limit - 1] + "…"


def _classify_trade_category(question: str) -> str:
    q = (question or "").upper()
    if q.startswith("WEATHER "):
        return "WEATHER"
    if q.startswith("THRESH "):
        return "THRESH"
    if q.startswith("SPREAD "):
        return "SPREAD"
    if q.startswith("LASTSEC "):
        return "LASTSEC"
    if q.startswith("COPY "):
        return "COPY"
    if q.startswith("CRYPTO ") or q.startswith("BTC "):
        return "CRYPTO"
    return "CORE"


def _format_dt(value: datetime | None, fmt: str = "%m/%d %H:%M") -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime(fmt)


def _parse_market_end(end_date: str | None) -> datetime | None:
    if not end_date:
        return None
    try:
        return datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except ValueError:
        return None


def _infer_expected_close(question: str, trade: Trade | None, market: Market | None) -> str:
    market_end = _parse_market_end(market.end_date if market else "")
    if market_end:
        return _format_dt(market_end)

    reasoning = (trade.reasoning or "") if trade else ""
    weather_match = _WEATHER_TARGET_RE.search(reasoning)
    if weather_match:
        target = datetime.strptime(weather_match.group("date"), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return _format_dt(target + timedelta(hours=30))

    thresh_match = _THRESH_END_RE.search(reasoning)
    if thresh_match:
        parsed = _parse_market_end(thresh_match.group("end"))
        if parsed:
            return _format_dt(parsed)

    created_at = getattr(trade, "created_at", None)
    if created_at and (question or "").upper().startswith("SPREAD "):
        return _format_dt(created_at + timedelta(minutes=20))
    if created_at and (question or "").upper().startswith("LASTSEC "):
        return _format_dt(created_at + timedelta(minutes=20))
    if created_at and (question or "").upper().startswith("THRESH "):
        return _format_dt(created_at + timedelta(hours=24))
    return "—"


def _equity_range_start(range_name: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    return {
        "1D": now - timedelta(days=1),
        "1W": now - timedelta(days=7),
        "1M": now - timedelta(days=30),
    }.get(range_name)


async def _build_equity_points(portfolio=None, range_name: str = "ALL") -> list[dict]:
    points: list[dict] = []
    # Equity must reflect the CURRENT mode only — mixing paper-oracle PnL
    # with live PnL produced flattering-but-meaningless curves.
    paper_mode = True
    if portfolio is not None:
        try:
            paper_mode = bool(portfolio.summary().get("paper_mode", True))
        except Exception:
            paper_mode = True
    async with get_session() as session:
        result = await session.execute(
            select(Trade)
            .where(
                Trade.status == "closed",
                Trade.side == "BUY",
                Trade.paper_mode == paper_mode,
                Trade.closed_at.is_not(None),
            )
            .order_by(Trade.closed_at.asc(), Trade.id.asc())
        )
        closed_trades = list(result.scalars().all())

    baseline = portfolio.initial_bankroll if portfolio else 0.0
    peak = baseline
    running = baseline
    range_start = _equity_range_start(range_name)

    for trade in closed_trades:
        closed_at = trade.closed_at
        if closed_at and closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        running += trade.pnl
        peak = max(peak, running)
        if range_start and closed_at and closed_at < range_start:
            continue
        points.append({
            "date": _format_dt(closed_at, "%H:%M" if range_name == "1D" else "%m/%d"),
            "bankroll": round(running, 2),
            "pnl": round(trade.pnl, 2),
            "trades": 1,
            "drawdown_pct": round(((peak - running) / peak) * 100, 1) if peak > 0 else 0,
        })

    if not points:
        async with get_session() as session:
            result = await session.execute(select(DailyStat).order_by(DailyStat.date.asc()))
            for s in result.scalars().all():
                points.append({
                    "date": s.date,
                    "bankroll": round(s.ending_bankroll, 2),
                    "pnl": round(s.daily_pnl, 2),
                    "trades": s.trades_count,
                    "drawdown_pct": round(
                        (1 - s.ending_bankroll / s.peak_bankroll) * 100, 1
                    ) if s.peak_bankroll > 0 else 0,
                })

    if portfolio:
        current_total = round(portfolio.summary()["total_value"], 2)
        future_peak = peak if peak > 0 else current_total
        if current_total > future_peak:
            future_peak = current_total
        drawdown_pct = round(((future_peak - current_total) / future_peak) * 100, 1) if future_peak > 0 else 0
        points.append({
            "date": "Now",
            "bankroll": current_total,
            "pnl": round(current_total - (points[-1]["bankroll"] if points else baseline), 2),
            "trades": 0,
            "drawdown_pct": drawdown_pct,
        })

    return points


def create_app(portfolio=None, pipeline=None, settings=None, autoresearch=None, health=None) -> FastAPI:
    app = FastAPI(title="Polymarket Bot Dashboard", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # Basic auth — disabled when dashboard_password is empty
    _auth_enabled = bool(settings and getattr(settings, "dashboard_password", ""))
    _expected_user = getattr(settings, "dashboard_user", "admin") if settings else "admin"
    _expected_pass = getattr(settings, "dashboard_password", "") if settings else ""

    async def check_auth(credentials: HTTPBasicCredentials | None = Depends(security)):
        if not _auth_enabled:
            return
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        user_ok = secrets.compare_digest(credentials.username.encode(), _expected_user.encode())
        pass_ok = secrets.compare_digest(credentials.password.encode(), _expected_pass.encode())
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )

    # Store references for live data
    app.state.portfolio = portfolio
    app.state.pipeline = pipeline
    app.state.settings = settings
    app.state.autoresearch = autoresearch
    app.state.health = health

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _=Depends(check_auth)):
        return templates.TemplateResponse(request, "dashboard.html", {
            "settings": settings,
        })

    @app.get("/api/summary")
    async def api_summary(_=Depends(check_auth)):
        sparkline = []
        try:
            sparkline = (await _build_equity_points(portfolio=portfolio))[-14:]
        except Exception:
            pass

        if portfolio:
            s = portfolio.summary()
            open_exposure = round(sum(p.size for p in portfolio.open_positions), 2)
            unrealized_pnl = round(sum(p.unrealized_pnl for p in portfolio.open_positions), 2)
            return {
                "bankroll": round(s["bankroll"], 2),
                "total_value": round(s["total_value"], 2),
                "daily_pnl": round(s["daily_pnl"], 2),
                "win_rate": round(s["win_rate"] * 100, 1),
                "total_trades": s["total_trades"],
                "wins": s["wins"],
                "losses": s["losses"],
                "open_positions": s["open_positions"],
                "peak_bankroll": round(s["peak_bankroll"], 2),
                "paper_mode": s["paper_mode"],
                "open_exposure": open_exposure,
                "unrealized_pnl": unrealized_pnl,
                "drawdown_pct": round(
                    (1 - s["bankroll"] / s["peak_bankroll"]) * 100, 1
                ) if s["peak_bankroll"] > 0 else 0,
                "sparkline": sparkline,
            }
        return {"sparkline": sparkline}

    @app.get("/api/positions", response_class=HTMLResponse)
    async def api_positions(request: Request, _=Depends(check_auth)):
        positions = []
        if portfolio and portfolio.open_positions:
            trade_ids = [p.trade_id for p in portfolio.open_positions if p.trade_id > 0]
            market_ids = [p.market_id for p in portfolio.open_positions if p.market_id]
            trade_by_id: dict[int, Trade] = {}
            market_by_id: dict[str, Market] = {}
            try:
                async with get_session() as session:
                    if trade_ids:
                        trade_result = await session.execute(
                            select(Trade).where(Trade.id.in_(trade_ids))
                        )
                        trade_by_id = {t.id: t for t in trade_result.scalars().all()}
                    if market_ids:
                        market_result = await session.execute(
                            select(Market).where(Market.condition_id.in_(market_ids))
                        )
                        market_by_id = {m.condition_id: m for m in market_result.scalars().all()}
            except Exception:
                log.exception("positions_meta_fetch_error")

            for p in portfolio.open_positions:
                trade = trade_by_id.get(p.trade_id)
                market = market_by_id.get(p.market_id)
                display_current = p.current_price if p.current_price > 0 else p.entry_price
                shares = p.size / p.entry_price if p.entry_price else 0.0
                display_pnl = shares * display_current - p.size if shares else 0.0
                display_pnl_pct = (display_pnl / p.size * 100) if p.size else 0.0
                positions.append({
                    "question": _short_question(p.question, 72),
                    "direction": p.direction,
                    "entry_price": round(p.entry_price, 3),
                    "current_price": round(display_current, 3),
                    "size": round(p.size, 2),
                    "pnl": round(display_pnl, 2),
                    "pnl_pct": round(display_pnl_pct, 1),
                    "category": _classify_trade_category(p.question),
                    "opened_at": _format_dt(getattr(trade, "created_at", None)),
                    "expected_close": _infer_expected_close(p.question, trade, market),
                })
        return templates.TemplateResponse(request, "partials/positions.html", {
            "positions": positions,
        })

    @app.get("/api/trades", response_class=HTMLResponse)
    async def api_trades(
        request: Request,
        month: str = Query("ALL"),
        category: str = Query("ALL"),
        status_filter: str = Query("ALL", alias="status"),
        _=Depends(check_auth),
    ):
        trades = []
        months: list[str] = []
        categories: set[str] = set()
        try:
            async with get_session() as session:
                month_result = await session.execute(
                    select(func.strftime("%Y-%m", Trade.created_at))
                    .where(Trade.created_at.is_not(None))
                    .distinct()
                    .order_by(func.strftime("%Y-%m", Trade.created_at).desc())
                )
                months = [row[0] for row in month_result.all() if row[0]]

                query = select(Trade).order_by(Trade.created_at.desc())
                if month != "ALL":
                    query = query.where(func.strftime("%Y-%m", Trade.created_at) == month)
                if status_filter != "ALL":
                    query = query.where(Trade.status == status_filter)

                result = await session.execute(query)
                rows = list(result.scalars().all())
                categories = {_classify_trade_category(t.question) for t in rows}
                if category != "ALL":
                    rows = [
                        t for t in rows
                        if _classify_trade_category(t.question) == category
                    ]
                for t in rows:
                    trade_category = _classify_trade_category(t.question)
                    trades.append({
                        "question": _short_question(t.question, 68),
                        "direction": t.direction,
                        "price": round(t.price, 3),
                        "size": round(t.size, 2),
                        "edge": round(t.edge * 100, 1),
                        "pnl": round(t.pnl, 2),
                        "status": t.status,
                        "paper": t.paper_mode,
                        "category": trade_category,
                        "created_at": _format_dt(t.created_at),
                        "closed_at": _format_dt(t.closed_at),
                    })
        except Exception:
            log.exception("trades_fetch_error")
        return templates.TemplateResponse(request, "partials/trades.html", {
            "trades": trades,
            "months": months,
            "categories": sorted(categories),
            "selected_month": month,
            "selected_category": category,
            "selected_status": status_filter,
            "total_count": len(trades),
        })

    @app.get("/api/equity", response_class=HTMLResponse)
    async def api_equity(request: Request, range: str = Query("ALL"), _=Depends(check_auth)):
        points = []
        try:
            points = await _build_equity_points(portfolio=portfolio, range_name=range)
        except Exception:
            log.exception("equity_fetch_error")
        return templates.TemplateResponse(request, "partials/equity.html", {
            "points": points,
        })

    @app.get("/api/risk-metrics")
    async def api_risk_metrics(_=Depends(check_auth)):
        try:
            stats = []
            async with get_session() as session:
                result = await session.execute(
                    select(DailyStat).order_by(DailyStat.date.asc())
                )
                stats = list(result.scalars().all())

            sharpe = None
            max_drawdown_pct = 0.0

            if len(stats) >= 2:
                bankrolls = [s.ending_bankroll for s in stats]
                returns = [
                    (bankrolls[i] - bankrolls[i - 1]) / bankrolls[i - 1]
                    for i in range(1, len(bankrolls))
                    if bankrolls[i - 1] > 0
                ]
                if returns:
                    avg_r = sum(returns) / len(returns)
                    variance = sum((r - avg_r) ** 2 for r in returns) / len(returns)
                    std_r = math.sqrt(variance)
                    sharpe = round((avg_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0, 2)

                peak = bankrolls[0]
                max_dd = 0.0
                for b in bankrolls:
                    if b > peak:
                        peak = b
                    dd = (peak - b) / peak if peak > 0 else 0.0
                    if dd > max_dd:
                        max_dd = dd
                max_drawdown_pct = round(max_dd * 100, 1)

            brier = None
            try:
                async with get_session() as session:
                    result = await session.execute(
                        select(Prediction).where(Prediction.actual_outcome.isnot(None))
                    )
                    preds = list(result.scalars().all())
                if preds:
                    brier = round(
                        sum((p.estimated_prob - p.actual_outcome) ** 2 for p in preds) / len(preds),
                        4
                    )
            except Exception:
                pass

            daily_loss_pct = 0.0
            if portfolio:
                s = portfolio.summary()
                bankroll = s.get("bankroll", 0)
                gross_loss = s.get("daily_gross_loss", 0)
                if bankroll > 0 and gross_loss > 0:
                    # Show how much of the daily loss limit has been used
                    # based on total losing trades today, not net P&L
                    loss_limit = settings.daily_loss_limit if settings else 0.30
                    daily_loss_pct = round(
                        min((gross_loss / bankroll) / loss_limit * 100, 100), 1
                    )

            return {
                "sharpe": sharpe,
                "max_drawdown": max_drawdown_pct,
                "brier": brier,
                "daily_loss_pct": daily_loss_pct,
            }
        except Exception:
            log.exception("risk_metrics_error")
            return {"sharpe": None, "max_drawdown": 0, "brier": None, "daily_loss_pct": 0}

    @app.get("/api/calibration")
    async def api_calibration(_=Depends(check_auth)):
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Prediction)
                    .where(Prediction.actual_outcome.isnot(None))
                    .order_by(Prediction.created_at.desc())
                    .limit(200)
                )
                preds = result.scalars().all()
            return [
                {"pred": round(p.estimated_prob, 3), "actual": round(p.actual_outcome, 3)}
                for p in preds
            ]
        except Exception:
            log.exception("calibration_error")
            return []

    @app.get("/api/risk-events", response_class=HTMLResponse)
    async def api_risk_events(request: Request, _=Depends(check_auth)):
        events = []
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(RiskEvent).order_by(RiskEvent.created_at.desc()).limit(15)
                )
                for e in result.scalars().all():
                    events.append({
                        "type": e.event_type,
                        "details": e.details[:80],
                        "time": e.created_at.strftime("%m/%d %H:%M") if e.created_at else "",
                    })
        except Exception:
            log.exception("risk_events_fetch_error")
        return templates.TemplateResponse(request, "partials/risk_events.html", {
            "events": events,
        })

    @app.get("/api/bot-status")
    async def api_bot_status(_=Depends(check_auth)):
        cycle = pipeline._cycle_count if pipeline else 0
        interval = settings.scan_interval_seconds if settings else 300
        last_health = pipeline.last_health if pipeline else {}
        return {
            "cycle": cycle,
            "interval": interval,
            "status": "running" if cycle > 0 else "starting",
            "health": {
                "polymarket_gamma": last_health.get("polymarket_gamma", None),
                "healthy": last_health.get("healthy", None),
                "healthy_news_sources": last_health.get("healthy_news_sources", 0),
                "news_sources": last_health.get("news_sources", {}),
            },
        }

    @app.get("/api/research")
    async def api_research(_=Depends(check_auth)):
        if not autoresearch:
            return {"status": "disabled"}
        experiments = autoresearch.experiment_log.get_all_results()
        return {
            "running": autoresearch.is_running,
            "active_level": autoresearch.active_level,
            "experiment_window": autoresearch.experiment_window,
            "total_experiments": len(experiments),
            "kept": sum(1 for e in experiments if e.status == "keep"),
            "discarded": sum(1 for e in experiments if e.status == "discard"),
            "best_params": autoresearch._autotuner.best_params,
            "meta_insights": autoresearch.meta_strategy.get_insights_report(),
            "recent": [
                {
                    "id": e.experiment_id,
                    "level": e.level,
                    "status": e.status,
                    "pnl": e.pnl,
                    "description": e.description,
                }
                for e in experiments[-10:]
            ],
        }

    class BotControlRequest(BaseModel):
        action: str

    @app.post("/api/bot-control")
    async def api_bot_control(body: BotControlRequest, _=Depends(check_auth)):
        action = body.action
        if pipeline and hasattr(pipeline, '_paused'):
            if action == 'pause':
                pipeline._paused = True
            elif action == 'resume':
                pipeline._paused = False
        return {"action": action, "ok": True}

    return app
