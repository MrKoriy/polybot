"""BTC/ETH/SOL/XRP/DOGE above/below-X threshold trader.

Finds "Will <asset> be above/below $X on <date>?" markets and prices them with
a Black-Scholes binary-option (cash-or-nothing) probability using Binance spot
+ realized volatility. Trades when model edge vs market price exceeds the floor.

Key decisions (Oct 2026 revision, see scripts/backtest_threshold_market.py):
- prob_above(S, K, T, sigma) via log-normal N(d2), sigma = 30d realised vol x1.2.
- Price off the executable book (bestBid/bestAsk), never Gamma display prices;
  require spread <= 3c. Display mids on empty books are fake (~0.5).
- Edge is NET of the ask and the crypto taker fee rate*p*(1-p).
- Model is shrunk 50/50 toward the market; |model - market| > 15pp is treated
  as bad input, not alpha.
- Only the final 24h before expiry; entry price 0.50-0.95.
- Honest backtest (May-Oct 2026, 5,812 resolved markets): WR ~85%, ROI ~+11%
  per dollar after fees; out-of-sample (Aug 15+) WR 85.4%, ROI +10.2%.
"""
import asyncio
import json
import math
import re
import time as _time
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from data.binance_ws import BinanceWSFeed
from bot.market_resolution import MarketResolutionService
from storage.repository import TradeRepository
from trading.portfolio import Portfolio

log = structlog.get_logger()

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines"

# --- Cycle + universe ---
CHECK_INTERVAL = 15  # s — tight loop; 2¢→$1 convergences close fast
MAX_THRESHOLD_POSITIONS = 25  # more room for multi-date × multi-asset × multi-strike

# --- Signal thresholds (LIVE-LOSS HARDENED, Sep 2026) ---
# The prior 2% floor was below real costs: live pays CLOB ask (+slippage),
# dynamic taker fees on crypto markets, and exits at mid-1c / redeem lag.
# Paper showed 100% WR because paper resolved via the bot's own Binance
# oracle at exactly $1/$0 — a self-fulfilling validation. The floor must
# clear fee + spread + oracle-source disagreement.
# --- Oct 2026 strategy revision (validated on 5,812 resolved daily markets,
# May-Oct 2026, real CLOB hourly prices + actual resolutions; see
# scripts/backtest_threshold_market.py). Key findings:
#  * Gamma `outcomePrices` on illiquid books is a fake ~0.5 mid (e.g. bid 0.13 /
#    ask 1.00 shows as 0.57). ALL of the big "edges" (>15pp) came from these
#    phantom prices. We now price off bestBid/bestAsk and require a tight book.
#  * Crypto markets charge a taker fee rate*p*(1-p) (rate 0.07). The old 5%
#    "net" edge never subtracted it.
#  * Shrinking the BS model halfway toward the market, using a 30-day vol with
#    a 1.2x fat-tail multiplier, and trading only the final 24h gave positive
#    ROI in every month / asset / side, out-of-sample WR ~85%.
MIN_EDGE = 0.02             # net edge AFTER paying ask + taker fee, on blended prob
MODEL_WEIGHT = 0.5          # p = w*model + (1-w)*market (shrinkage toward market)
MAX_MODEL_DISAGREEMENT = 0.15  # |model - mid| above this => our input is wrong, skip
MAX_SPREAD = 0.03           # require a real two-sided book
DEFAULT_TAKER_FEE_RATE = 0.07  # crypto_fees_v2: fee/share = rate * p * (1-p)
MIN_VOLUME = 300.0
MIN_HOURS_TO_EXPIRY = 0.5
MAX_HOURS_TO_EXPIRY = 24.0  # only the final day: liquid books, honest prices

# CRITICAL: minimum entry price. Below this, BS model is mispriced vs reality.
MIN_PRICE_ENTRY = 0.50      # was implicit 0.02; now 50¢ hard floor
MAX_PRICE_ENTRY = 0.95      # still skip nearly-certain (asymmetry risk)

# --- Sizing (de-risked after the -$20/100 live loss) ---
MIN_BET = 2.0               # was 5.0 — forced over-sizing on a $100 bankroll
MAX_POSITION_PCT = 0.30
MAX_BET_USDC = 3.0          # Hard absolute cap per bet. Enforced ALWAYS.
# Hard cap on TOTAL simultaneous open risk (dollars at cost) across all
# threshold positions. The -$20 live loss happened because up to
# MAX_THRESHOLD_POSITIONS × MAX_BET could be committed before the first
# trade resolved and any reactive breaker fired.
MAX_TOTAL_OPEN_RISK_USDC = 10.0
# Kelly multipliers ONLY for accepted price range (0.50–0.95)
_KELLY_BY_PRICE = [
    (0.60, 0.25),  # 50–60¢: lowest conviction tier
    (0.75, 0.35),  # 60–75¢: growing conviction
    (0.90, 0.50),  # 75–90¢: strong ITM — half Kelly
    (1.00, 0.40),  # 90–95¢: very close, but fill-risk + asymmetric loss
]

# OTM tail-bias DISABLED — caused 0/21 WR on long-shots in paper test.
# Market is efficient at the tails; model overestimates when using +5pp boost.
OTM_BIAS_BOOST = 0.0
OTM_BIAS_MAX_PRICE = 0.0

# --- Exits ---
EXIT_TP_PRICE = 0.80       # take profit at 80% of $1.00
EXIT_SL_RATIO = 0.55       # cut if current_price < entry * 0.55 (edge collapsed)
EXIT_HOLD_TO_RESOLVE = 0.90  # if price >= 0.90 and model agrees, hold to $1.00
EXIT_MAX_AGE_HOURS = 48.0  # hard backstop if nothing else resolves

# --- Volatility ---
VOL_CACHE_TTL_SECONDS = 300  # refresh realized vol every 5 min
VOL_CANDLES_HOURS = 720      # 30d × 1h returns — 48h was too noisy (backtest)
VOL_MULT = 1.2               # fat-tail / vol-of-vol inflation of realised sigma

# Patterns to match threshold market questions
THRESHOLD_PATTERNS = [
    re.compile(
        r"Will (?:the price of )?(?P<asset>Bitcoin|BTC|Ethereum|ETH|Solana|SOL|XRP|Ripple|Dogecoin|DOGE) "
        r"be (?P<direction>above|below) \$(?P<threshold>[\d,]+(?:\.\d+)?)"
        r".*?(?:on |by )(?P<date>.+?)(?:\?|$)",
        re.IGNORECASE,
    ),
]

_SERIES_ASSETS = ("bitcoin", "ethereum", "solana", "xrp")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")

ASSET_TO_BINANCE = {
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    "ethereum": "ETHUSDT", "eth": "ETHUSDT",
    "solana": "SOLUSDT", "sol": "SOLUSDT",
    "xrp": "XRPUSDT", "ripple": "XRPUSDT",
    "dogecoin": "DOGEUSDT", "doge": "DOGEUSDT",
}

ASSET_TO_KEY = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "dogecoin": "DOGE", "doge": "DOGE",
}

# Fallback daily vol (annualised crypto historicals) if Binance fetch fails.
# In practice realised σ is used; these prevent dead-markets on vol outage.
_FALLBACK_SIGMA_DAILY = {
    "BTC": 0.030,
    "ETH": 0.040,
    "SOL": 0.055,
    "XRP": 0.050,
    "DOGE": 0.060,
}


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf (no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(S: float, K: float, T_hours: float, sigma_daily: float) -> float:
    """P(S_T > K) under log-normal with μ=0, σ from realised daily vol.

    This is the risk-neutral, zero-drift binary-option probability. For short
    horizons (<5d) and near-spot strikes, drift is dwarfed by vol so μ≈0 is fine.
    """
    if T_hours <= 0:
        return 1.0 if S > K else 0.0
    if sigma_daily <= 0 or S <= 0 or K <= 0:
        return 1.0 if S > K else 0.0
    T = T_hours / 24.0
    sigma = sigma_daily * math.sqrt(T)
    if sigma <= 1e-9:
        return 1.0 if S > K else 0.0
    d2 = (math.log(S / K) - 0.5 * sigma ** 2) / sigma
    return _norm_cdf(d2)


def taker_fee_per_share(price: float, rate: float = DEFAULT_TAKER_FEE_RATE,
                        exponent: float = 1.0) -> float:
    """Polymarket CLOB V2 dynamic taker fee per share: rate * (p*(1-p))^exp."""
    if not 0.0 < price < 1.0 or rate <= 0:
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def score_threshold_signal(
    *,
    spot: float,
    threshold: float,
    is_above: bool,
    t_hours: float,
    sigma_daily: float,
    best_bid: float | None,
    best_ask: float | None,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    fee_exponent: float = 1.0,
) -> dict | None:
    """Pure signal logic shared by the live trader and the backtest.

    Prices off the executable book (YES ask / NO = 1 - YES bid), never the
    Gamma display price. Returns None when there is no tradeable edge.
    """
    if t_hours < MIN_HOURS_TO_EXPIRY or t_hours > MAX_HOURS_TO_EXPIRY:
        return None
    if best_bid is None or best_ask is None:
        return None
    if not (0.0 < best_bid < best_ask < 1.0):
        return None
    if best_ask - best_bid > MAX_SPREAD:
        return None
    sigma = max(0.01, min(0.20, sigma_daily)) * VOL_MULT
    p_above = prob_above(spot, threshold, t_hours, sigma)
    model_yes = p_above if is_above else 1.0 - p_above
    mid_yes = (best_bid + best_ask) / 2.0
    if abs(model_yes - mid_yes) > MAX_MODEL_DISAGREEMENT:
        return None
    prob_yes = MODEL_WEIGHT * model_yes + (1.0 - MODEL_WEIGHT) * mid_yes

    best = None
    for side, prob, price in (("YES", prob_yes, best_ask), ("NO", 1.0 - prob_yes, 1.0 - best_bid)):
        if price < MIN_PRICE_ENTRY or price > MAX_PRICE_ENTRY:
            continue
        fee = taker_fee_per_share(price, fee_rate, fee_exponent)
        edge = prob - price - fee
        if edge > MIN_EDGE and (best is None or edge > best["edge"]):
            best = {"side": side, "price": price, "edge": edge, "fee": fee}
    if best is None:
        return None
    best.update(our_prob_yes=prob_yes, model_prob_yes=model_yes, mid_yes=mid_yes,
                sigma=sigma)
    return best


def _parse_threshold_question(question: str) -> dict | None:
    for pat in THRESHOLD_PATTERNS:
        m = pat.search(question)
        if m:
            asset = m.group("asset").lower()
            try:
                threshold = float(m.group("threshold").replace(",", ""))
            except ValueError:
                continue
            return {
                "asset": asset,
                "binance_symbol": ASSET_TO_BINANCE.get(asset, ""),
                "asset_key": ASSET_TO_KEY.get(asset, asset.upper()),
                "direction": m.group("direction").lower(),
                "threshold": threshold,
                "date": m.group("date").strip(),
            }
    return None


def _opt_float(v, default: float | None = None) -> float | None:
    try:
        return float(v) if v is not None and v != "" else default
    except (TypeError, ValueError):
        return default


def _parse_end_date(end_date_str: str) -> datetime | None:
    if not end_date_str:
        return None
    try:
        return datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class ThresholdTrader:
    """Trades 'Will X be above/below $Y on date?' markets via BS-binary pricing."""

    def __init__(
        self,
        portfolio: Portfolio,
        binance_feed: BinanceWSFeed,
        paper_mode: bool = True,
        trade_repo: TradeRepository | None = None,
        calibration=None,
        executor=None,  # OrderExecutor — required for live mode; None = paper-only trader
        breakers=None,  # CircuitBreakerManager — optional halt enforcement
        resolution: MarketResolutionService | None = None,
    ) -> None:
        self._portfolio = portfolio
        self._binance = binance_feed
        self._paper_mode = paper_mode
        self._trade_repo = trade_repo
        self._calibration = calibration
        self._executor = executor
        self._breakers = breakers
        # Honest resolution: actual market outcome, never a bot-internal oracle.
        self._resolution = resolution or MarketResolutionService()
        self._http = httpx.AsyncClient(
            timeout=8.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)"},
        )
        self._running = False
        self._cycle_count = 0
        self._trades_count = 0
        # market_id → prediction_id, for calibration resolve
        self._prediction_ids: dict[str, int] = {}
        # market_id → (end_date_utc, threshold, is_above) cached at entry
        self._position_context: dict[str, tuple[datetime | None, float, bool]] = {}
        # asset_key → (sigma_daily, fetched_at_epoch)
        self._vol_cache: dict[str, tuple[float, float]] = {}
        # market_id → monotonic ts of last failed live close attempt (backoff)
        self._close_retry_at: dict[str, float] = {}

    async def start(self) -> None:
        self._running = True
        log.info("threshold_trader_started")
        await self._rehydrate_position_context()
        asyncio.create_task(self._run_loop())

    async def _rehydrate_position_context(self) -> None:
        """Restore (end_date, threshold, is_above) for open positions after restart.

        Without this, restored positions fall through to _legacy_resolve (6h age
        fallback) instead of closing at their real resolution time.
        Parses threshold/direction from question prefix; queries Polymarket Gamma
        API for the endDate of each market.
        """
        if not self._trade_repo:
            return
        try:
            held = self._portfolio.held_market_ids
            open_trades = await self._trade_repo.get_open_trades(self._paper_mode)
        except Exception:
            log.exception("threshold_rehydrate_fetch_failed")
            return

        recovered = 0
        skipped_no_parse = 0
        skipped_no_date = 0
        for trade in open_trades:
            if trade.market_id in self._position_context:
                continue
            if trade.market_id not in held:
                continue
            q = (trade.question or "").upper()
            if not q.startswith("THRESH "):
                continue
            m = re.search(r"THRESH (\w+) ([><])\$([\d,\.]+)", q)
            if not m:
                skipped_no_parse += 1
                continue
            try:
                threshold = float(m.group(3).replace(",", ""))
            except ValueError:
                skipped_no_parse += 1
                continue
            is_above = m.group(2) == ">"

            end_date = await self._fetch_market_end_date(trade.market_id)
            if end_date is None:
                skipped_no_date += 1
                # Still populate threshold/is_above so _manage_positions doesn't
                # fall to legacy; _manage_positions will skip close until age>6h.
            self._position_context[trade.market_id] = (end_date, threshold, is_above)
            recovered += 1

        if recovered or skipped_no_parse or skipped_no_date:
            log.info("threshold_ctx_rehydrated",
                     recovered=recovered,
                     skipped_no_parse=skipped_no_parse,
                     skipped_no_date=skipped_no_date)

    async def _fetch_market_end_date(self, condition_id: str) -> datetime | None:
        """Query Polymarket Gamma for market endDate. Returns None on failure."""
        try:
            resp = await self._http.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_ids": condition_id},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data:
                return None
            end_str = data[0].get("endDate") or ""
            if not end_str:
                return None
            return datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except Exception:
            log.exception("threshold_fetch_end_date_failed", market_id=condition_id)
            return None

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()
        await self._resolution.close()
        log.info("threshold_trader_stopped", trades=self._trades_count)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._run_cycle()
            except Exception:
                log.exception("threshold_trader_error")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _run_cycle(self) -> None:
        self._cycle_count += 1
        await self._manage_positions()

        if self._breakers is not None:
            halted, reason = self._breakers.is_halted("threshold")
            if halted:
                log.info("threshold_cycle_halted", reason=reason)
                return

        threshold_positions = sum(
            1 for p in self._portfolio.open_positions
            if "THRESH" in (p.question or "").upper()
        )
        pending_threshold = 0
        if self._executor is not None:
            pending_threshold = sum(
                1 for data in self._executor.pending_orders
                if (data.get("question") or "").upper().startswith("THRESH ")
            )
        threshold_positions += pending_threshold
        if threshold_positions >= MAX_THRESHOLD_POSITIONS:
            return

        # Hard dollar cap on simultaneous open risk. Reactive breakers only
        # see CLOSED trades; positions here resolve hours later, so exposure
        # must be gated at entry time.
        open_risk = sum(
            p.size for p in self._portfolio.open_positions
            if "THRESH" in (p.question or "").upper()
        )
        pending_risk = 0.0
        if self._executor is not None:
            pending_risk = sum(
                float(data.get("size") or 0.0)
                for data in self._executor.pending_orders
                if (data.get("question") or "").upper().startswith("THRESH ")
            )
        if open_risk + pending_risk >= MAX_TOTAL_OPEN_RISK_USDC:
            log.debug("threshold_open_risk_cap",
                      open_risk=round(open_risk, 2),
                      pending_risk=round(pending_risk, 2))
            return

        markets = await self._find_threshold_markets()
        if not markets:
            return

        # Rank by model |edge| descending — take best opportunities first
        scored = []
        for m in markets:
            evaluated = await self._score_market(m)
            if evaluated:
                scored.append(evaluated)
        scored.sort(key=lambda x: -x["edge"])

        for signal in scored:
            if threshold_positions >= MAX_THRESHOLD_POSITIONS:
                break
            if await self._execute_signal(signal):
                threshold_positions += 1

    # ------------------------------------------------------------------
    # Volatility (realised σ from Binance klines)
    # ------------------------------------------------------------------
    async def _get_sigma_daily(self, asset_key: str) -> float:
        """Realized daily vol from last N × 1h log returns (annualised-free)."""
        now = _time.time()
        cached = self._vol_cache.get(asset_key)
        if cached and (now - cached[1]) < VOL_CACHE_TTL_SECONDS:
            return cached[0]

        symbol = ASSET_TO_BINANCE.get(asset_key.lower()) or \
                 ASSET_TO_BINANCE.get(asset_key.upper().lower())
        fallback = _FALLBACK_SIGMA_DAILY.get(asset_key.upper(), 0.04)
        if not symbol:
            return fallback
        try:
            resp = await self._http.get(
                BINANCE_KLINES,
                params={"symbol": symbol, "interval": "1h", "limit": VOL_CANDLES_HOURS},
            )
            if resp.status_code != 200:
                return fallback
            klines = resp.json()
            closes = [float(k[4]) for k in klines]
            if len(closes) < 6:
                return fallback
            # log-returns hour-over-hour
            rets = [math.log(closes[i] / closes[i - 1])
                    for i in range(1, len(closes)) if closes[i - 1] > 0]
            if not rets:
                return fallback
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
            hourly_sigma = math.sqrt(var)
            # Convert hourly → daily: σ_d = σ_h · sqrt(24)
            sigma_daily = hourly_sigma * math.sqrt(24.0)
            # Sanity clamp: crypto realised daily σ rarely outside [1%, 20%]
            sigma_daily = max(0.01, min(0.20, sigma_daily))
            self._vol_cache[asset_key] = (sigma_daily, now)
            return sigma_daily
        except Exception:
            log.exception("vol_fetch_error", asset=asset_key)
            return fallback

    # ------------------------------------------------------------------
    # Position management (resolution-driven exits)
    # ------------------------------------------------------------------
    _CLOSE_RETRY_BACKOFF_SEC = 60.0

    async def _manage_positions(self) -> None:
        """Close positions ONLY on the actual market resolution.

        HISTORICAL BUG (fixed here): paper mode used to close at $1/$0 based
        on the bot's own Binance-vs-threshold oracle. That fabricated the
        "14/14 WINS, 100% WR" validation which then lost real money live —
        the market's actual resolution (different source and timing) is the
        only legitimate payout.

        Exit priority:
          1. Market resolved (Gamma): paper closes at actual $1/$0 for our
             side; live closes via executor.close_live (redeem preferred).
          2. Age backstop: after EXIT_MAX_AGE_HOURS unresolved, live tries a
             CLOB exit; paper holds until resolution (positions always
             resolve eventually — inventing a price is worse than waiting).
        Binance spot vs threshold is logged as a DIAGNOSTIC only, never PnL.
        """
        now = datetime.now(timezone.utc)

        for pos in list(self._portfolio.open_positions):
            if "THRESH" not in (pos.question or "").upper():
                continue

            # 1. Actual market resolution — the ONLY paper PnL source.
            resolved, exit_price, note = await self._resolution.resolve_position(pos)
            if resolved and exit_price is not None:
                await self._close_resolved(pos, exit_price, source=note)
                continue

            # 2. Backstop for unresolved markets.
            ctx = self._position_context.get(pos.market_id)
            if not ctx:
                await self._legacy_manage(pos, now)
                continue

            opened = pos.opened_at
            if opened and opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            age_h = (now - opened).total_seconds() / 3600 if opened else 0.0
            if age_h <= EXIT_MAX_AGE_HOURS:
                continue

            log.warning(
                "threshold_unresolved_past_backstop",
                market=(pos.question or "")[:60],
                age_hours=round(age_h, 1),
                end_date=str(ctx[0]),
            )
            if self._executor and not self._paper_mode:
                retry_at = self._close_retry_at.get(pos.market_id, 0.0)
                if _time.monotonic() < retry_at:
                    continue
                result = await self._executor.close_live(
                    pos, reason="threshold_backstop_unresolved"
                )
                if result.success:
                    pnl = await self._portfolio.close_position(
                        pos.market_id, result.fill_price
                    )
                    self._cleanup_position(pos.market_id, result.fill_price)
                    log.info("threshold_backstop_closed",
                             market=(pos.question or "")[:60],
                             exit=round(result.fill_price, 4), pnl=round(pnl, 2))
                else:
                    self._close_retry_at[pos.market_id] = (
                        _time.monotonic() + self._CLOSE_RETRY_BACKOFF_SEC
                    )
                    log.warning("threshold_backstop_close_failed",
                                market=pos.market_id, error=result.error)
            # Paper: keep holding until the market actually resolves.

    async def _close_resolved(self, pos, exit_price: float, source: str) -> None:
        """Close a position at its ACTUAL market resolution outcome."""
        if self._executor and not self._paper_mode:
            retry_at = self._close_retry_at.get(pos.market_id, 0.0)
            if _time.monotonic() < retry_at:
                return
            result = await self._executor.close_live(
                pos, reason=f"threshold_resolved_{source}"
            )
            if not result.success:
                # Redeem/sell failed (CTF not finalised yet, book gone) —
                # retry next cycle with backoff. Never fake-close.
                self._close_retry_at[pos.market_id] = (
                    _time.monotonic() + self._CLOSE_RETRY_BACKOFF_SEC
                )
                log.warning("threshold_close_live_failed",
                            market=pos.market_id, error=result.error)
                return
            realised = result.fill_price
        else:
            # Paper: close at the actual outcome (this is now the REAL
            # resolution of the market, not a bot-internal oracle).
            realised = exit_price

        pnl = await self._portfolio.close_position(pos.market_id, realised)
        self._cleanup_position(pos.market_id, realised)

        if self._calibration:
            pred_id = self._prediction_ids.pop(pos.market_id, None)
            if pred_id:
                try:
                    await self._calibration.resolve_prediction(pred_id, exit_price)
                except Exception:
                    log.exception("calibration_resolve_error", market_id=pos.market_id)

        log.info(
            "threshold_resolved",
            market=(pos.question or "")[:60],
            source=source,
            exit_price=round(exit_price, 4),
            realised_exit=round(realised, 4),
            pnl=round(pnl, 2),
        )

    async def _resolve_by_binance(self, pos, asset_key: str, threshold: float,
                                   is_above: bool, current_price: float) -> None:
        """DIAGNOSTIC-ONLY Binance oracle. Logs what Binance implies.

        Kept for rehydration paths; the actual close decision always comes
        from _manage_positions' market-resolution check.
        """
        above_threshold = current_price > threshold
        yes_wins = above_threshold if is_above else (not above_threshold)
        is_buy_yes = "YES" in (pos.direction or "").upper()
        expected_exit = 1.0 if (is_buy_yes == yes_wins) else 0.0
        log.info(
            "threshold_binance_diagnostic",
            market=(pos.question or "")[:60],
            spot=round(current_price, 2),
            threshold=threshold,
            binance_implies="WIN" if expected_exit == 1.0 else "LOSS",
            note="diagnostic only — PnL comes from actual market resolution",
        )

    async def _legacy_manage(self, pos, now: datetime) -> None:
        """Fallback for positions without cached context: wait for resolution.

        The old path force-closed at 6h via the Binance oracle; that invented
        PnL. Now: resolution check happens in _manage_positions already, so
        here we only log that context is missing (rehydration will fix it).
        """
        log.debug("threshold_missing_position_context",
                  market_id=pos.market_id,
                  hint="rehydrate should restore ctx; waiting for actual resolution")

    def _cleanup_position(self, market_id: str, exit_price: float) -> None:
        self._position_context.pop(market_id, None)

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------
    async def _find_threshold_markets(self) -> list[dict]:
        results = []
        seen_cids = set()
        all_markets: list[dict] = []
        try:
            # Broad volume-ordered scan
            for offset in [0, 50, 100, 150, 200, 250]:
                resp = await self._http.get(
                    f"{GAMMA_API}/markets",
                    params={"closed": "false", "limit": 50, "offset": offset,
                            "order": "volume24hr", "ascending": "false"},
                )
                if resp.status_code == 200:
                    all_markets.extend(resp.json())

            # Events scan — catches future-day nested markets
            for offset in [0, 50, 100, 150]:
                resp = await self._http.get(
                    f"{GAMMA_API}/events",
                    params={"closed": "false", "limit": 50, "offset": offset,
                            "order": "volume24hr", "ascending": "false"},
                )
                if resp.status_code == 200:
                    for event in resp.json():
                        title = (event.get("title") or "").lower()
                        if any(kw in title for kw in
                               ["bitcoin", "btc", "ethereum", "eth",
                                "solana", "sol", "xrp", "doge", "ripple", "price"]):
                            for mkt in event.get("markets", []):
                                all_markets.append(mkt)

            # Daily "<asset> above ___ on <date>" series by slug — the volume
            # scans above miss most of them. Only today/tomorrow matter given
            # MAX_HOURS_TO_EXPIRY = 24h.
            today = datetime.now(timezone.utc).date()
            for asset in _SERIES_ASSETS:
                for d_off in (0, 1):
                    d = today + timedelta(days=d_off)
                    slug = f"{asset}-above-on-{_MONTHS[d.month - 1]}-{d.day}-{d.year}"
                    resp = await self._http.get(f"{GAMMA_API}/events", params={"slug": slug})
                    if resp.status_code == 200:
                        for event in resp.json() or []:
                            all_markets.extend(event.get("markets", []) or [])

            # Dedicated crypto-price events scan (P4.1)
            for slug in ["bitcoin", "ethereum", "solana", "xrp"]:
                resp = await self._http.get(
                    f"{GAMMA_API}/events",
                    params={"closed": "false", "limit": 50, "tag": slug},
                )
                if resp.status_code == 200:
                    for event in resp.json():
                        for mkt in event.get("markets", []):
                            all_markets.append(mkt)

            for m in all_markets:
                question = m.get("question", "")
                parsed = _parse_threshold_question(question)
                if not parsed or not parsed["binance_symbol"]:
                    continue

                condition_id = m.get("conditionId", "")
                if not condition_id or condition_id in seen_cids:
                    continue
                seen_cids.add(condition_id)
                # Only block markets we currently hold — lifetime dedup removed.
                if condition_id in self._portfolio.held_market_ids:
                    continue
                if self._executor and condition_id in self._executor.pending_market_ids:
                    continue

                raw_prices = m.get("outcomePrices", '["0.5","0.5"]')
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                yes_price = float(prices[0]) if prices else 0.5

                raw_tokens = m.get("clobTokenIds", "[]")
                tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens

                volume = float(m.get("volumeNum", 0) or 0)
                if volume < MIN_VOLUME:
                    continue

                end_date = _parse_end_date(m.get("endDate", ""))
                fee_sched = m.get("feeSchedule") or {}

                results.append({
                    **parsed,
                    "condition_id": condition_id,
                    "question": question,
                    "yes_price": yes_price,
                    "no_price": 1.0 - yes_price,
                    "yes_token": str(tokens[0]) if tokens else "",
                    "no_token": str(tokens[1]) if len(tokens) > 1 else "",
                    "volume": volume,
                    "end_date": end_date,
                    "best_bid": _opt_float(m.get("bestBid")),
                    "best_ask": _opt_float(m.get("bestAsk")),
                    "fee_rate": _opt_float(fee_sched.get("rate"), DEFAULT_TAKER_FEE_RATE)
                    if m.get("feesEnabled", True) else 0.0,
                    "fee_exponent": _opt_float(fee_sched.get("exponent"), 1.0),
                })

        except Exception:
            log.exception("threshold_market_search_error")

        log.info("threshold_markets_found", count=len(results))
        return results

    # ------------------------------------------------------------------
    # Scoring + execution
    # ------------------------------------------------------------------
    async def _score_market(self, market: dict) -> dict | None:
        """Compute BS probability + choose side with edge ≥ MIN_EDGE. Returns signal dict."""
        asset_key = market["asset_key"]
        current_price = self._binance.get_price(asset_key)
        if not current_price:
            try:
                resp = await self._http.get(
                    "https://data-api.binance.vision/api/v3/ticker/price",
                    params={"symbol": market["binance_symbol"]},
                )
                if resp.status_code == 200:
                    current_price = float(resp.json()["price"])
            except Exception:
                return None
        if not current_price:
            return None

        end_date: datetime | None = market.get("end_date")
        now = datetime.now(timezone.utc)
        if end_date:
            T_hours = (end_date - now).total_seconds() / 3600
        else:
            T_hours = 24.0  # conservative default
        if T_hours < MIN_HOURS_TO_EXPIRY or T_hours > MAX_HOURS_TO_EXPIRY:
            return None

        sigma = await self._get_sigma_daily(asset_key)
        is_above = market["direction"] == "above"
        sig = score_threshold_signal(
            spot=current_price,
            threshold=market["threshold"],
            is_above=is_above,
            t_hours=T_hours,
            sigma_daily=sigma,
            best_bid=market.get("best_bid"),
            best_ask=market.get("best_ask"),
            fee_rate=market.get("fee_rate", DEFAULT_TAKER_FEE_RATE),
            fee_exponent=market.get("fee_exponent", 1.0),
        )
        if sig is None:
            return None
        side, price, edge = sig["side"], sig["price"], sig["edge"]
        our_prob_yes = sig["our_prob_yes"]
        sigma = sig["sigma"]
        token_id = market["yes_token"] if side == "YES" else market["no_token"]
        if not token_id:
            return None

        return {
            "market": market,
            "side": side,
            "token_id": token_id,
            "price": price,
            "edge": edge,
            "our_prob_yes": our_prob_yes,
            "current_price": current_price,
            "T_hours": T_hours,
            "sigma": sigma,
            "is_above": is_above,
            "end_date": end_date,
        }

    def _kelly_multiplier(self, price: float) -> float:
        for cutoff, k in _KELLY_BY_PRICE:
            if price < cutoff:
                return k
        return 0.15

    def _build_proposal_data(
        self,
        market: dict,
        signal: dict,
        token_id: str,
        question: str,
        direction: str,
        estimated_prob: float,
        kelly: float,
        reasoning: str,
    ) -> dict:
        metadata = {
            "strategy": "threshold",
            "asset_key": market["asset_key"],
            "threshold": float(market["threshold"]),
            "is_above": bool(signal["is_above"]),
            "end_date": signal["end_date"].isoformat() if signal["end_date"] else "",
            "side": signal["side"],
        }
        return {
            "market_id": market["condition_id"],
            "token_id": token_id,
            "question": question,
            "direction": direction,
            "estimated_prob": estimated_prob,
            "confidence": 0.80,
            "edge": signal["edge"],
            "kelly_fraction": kelly,
            "reasoning": reasoning,
            "market_price": signal["price"],
            "metadata": metadata,
        }

    async def on_live_fill(
        self,
        trade_id: int,
        fill_price: float,
        fill_size: float,
        proposal_data: dict,
    ) -> None:
        if fill_price <= 0 or fill_size <= 0:
            return

        self._portfolio.open_position(
            trade_id=trade_id,
            market_id=proposal_data["market_id"],
            token_id=proposal_data["token_id"],
            question=proposal_data["question"],
            direction=proposal_data["direction"],
            price=fill_price,
            size=fill_size,
        )

        metadata = proposal_data.get("metadata", {})
        end_date = _parse_end_date(metadata.get("end_date", ""))
        threshold = float(metadata.get("threshold", 0.0))
        is_above = bool(metadata.get("is_above", True))
        self._position_context[proposal_data["market_id"]] = (end_date, threshold, is_above)

        if self._calibration:
            try:
                pred_id = await self._calibration.record_prediction(
                    market_id=proposal_data["market_id"],
                    question=proposal_data["question"],
                    estimated_prob=proposal_data["estimated_prob"],
                    market_price=proposal_data["market_price"],
                    confidence=proposal_data["confidence"],
                    model_used="threshold_bs",
                    reasoning=proposal_data.get("reasoning", ""),
                )
                self._prediction_ids[proposal_data["market_id"]] = pred_id
            except Exception:
                log.exception("calibration_record_error")

        self._trades_count += 1
        log.info(
            "threshold_trade_executed",
            asset=metadata.get("asset_key"),
            side=metadata.get("side"),
            price=fill_price,
            size=fill_size,
            edge=round(proposal_data["edge"], 3),
            trade_id=trade_id,
            pending_fill=False,
        )

    async def _execute_signal(self, signal: dict) -> bool:
        market = signal["market"]
        price = signal["price"]
        edge = signal["edge"]
        side = signal["side"]
        token_id = signal["token_id"]
        asset_key = market["asset_key"]
        threshold = market["threshold"]
        is_above = signal["is_above"]

        bankroll = self._portfolio.bankroll
        if bankroll <= 0:
            return False

        k_mult = self._kelly_multiplier(price)
        kelly = max(0.0, edge / (1.0 - price)) * k_mult
        # Enforce 3 caps: Kelly, pct-of-bankroll, absolute $ cap.
        bet_size = round(min(
            bankroll * kelly,
            bankroll * MAX_POSITION_PCT,
            MAX_BET_USDC,
        ), 2)
        if bet_size < MIN_BET:
            return False

        direction = f"BUY_{side}"
        question = (f"THRESH {asset_key} {'>' if is_above else '<'}${threshold:,.0f} | "
                    f"{market['question'][:30]}")

        log.info(
            "threshold_signal",
            asset=asset_key, threshold=threshold,
            spot=round(signal["current_price"], 2),
            T_h=round(signal["T_hours"], 1),
            sigma_d=round(signal["sigma"], 4),
            market_yes=round(market["yes_price"], 3),
            our_prob_yes=round(signal["our_prob_yes"], 3),
            side=side, price=round(price, 3), edge=round(edge, 3),
            k_mult=k_mult, bet_size=bet_size, volume=market["volume"],
        )

        estimated_prob = signal["our_prob_yes"] if side == "YES" else (1 - signal["our_prob_yes"])
        end_date_text = signal["end_date"].isoformat() if signal["end_date"] else ""
        reasoning = (f"BS: spot=${signal['current_price']:,.2f} K=${threshold:,.0f} "
                     f"T={signal['T_hours']:.1f}h σ={signal['sigma']:.3f} "
                     f"P_yes={signal['our_prob_yes']:.3f} | end={end_date_text}")
        proposal_data = self._build_proposal_data(
            market=market,
            signal=signal,
            token_id=token_id,
            question=question,
            direction=direction,
            estimated_prob=estimated_prob,
            kelly=kelly,
            reasoning=reasoning,
        )

        # --- Execution path: live and realistic-paper go through OrderExecutor.
        # Legacy paper writes a Trade directly for deterministic old backtests.
        use_executor = self._executor and (
            not self._paper_mode
            or getattr(self._executor, "realistic_paper_execution", False)
        )
        if use_executor:
            from trading.risk_manager import TradeProposal
            proposal = TradeProposal(
                market_id=market["condition_id"],
                token_id=token_id,
                question=question,
                direction=direction,  # "BUY_YES" or "BUY_NO"
                price=price,
                size=bet_size,
                edge=edge,
                estimated_prob=estimated_prob,
                confidence=0.80,
                kelly_fraction=kelly,
                reasoning=reasoning,
                metadata=proposal_data["metadata"],
            )
            result = await self._executor.execute(proposal)
            if not result.success:
                log.warning("threshold_trade_rejected",
                            market=market["condition_id"],
                            error=result.error)
                return False
            if result.pending:
                log.info(
                    "threshold_order_pending",
                    market=market["condition_id"],
                    order_id=result.order_id,
                    price=price,
                    size=bet_size,
                )
                return True
            await self.on_live_fill(
                trade_id=result.trade_id,
                fill_price=result.fill_price,
                fill_size=result.fill_size,
                proposal_data=proposal_data,
            )
        else:
            # Paper mode — write Trade directly, open simulated position.
            trade_id = 0
            if self._trade_repo:
                trade = await self._trade_repo.create_trade(
                    market_id=market["condition_id"],
                    token_id=token_id,
                    question=question,
                    side="BUY",
                    direction=direction,
                    price=price,
                    size=bet_size,
                    edge=edge,
                    kelly_fraction=kelly,
                    estimated_prob=estimated_prob,
                    market_price_at_entry=price,
                    confidence=0.80,
                    status="filled",
                    paper_mode=self._paper_mode,
                    order_id=f"thresh_{asset_key}_{int(_time.time())}",
                    reasoning=reasoning,
                )
                trade_id = trade.id
            self._portfolio.open_position(
                trade_id=trade_id,
                market_id=market["condition_id"],
                token_id=token_id,
                question=question,
                direction=direction,
                price=price,
                size=bet_size,
            )
            self._position_context[market["condition_id"]] = (signal["end_date"], threshold, is_above)

            if self._calibration:
                try:
                    pred_id = await self._calibration.record_prediction(
                        market_id=market["condition_id"],
                        question=question,
                        estimated_prob=estimated_prob,
                        market_price=price,
                        confidence=0.80,
                        model_used="threshold_bs",
                        reasoning=f"BS binary: S={signal['current_price']:.2f} K={threshold} "
                                  f"T_h={signal['T_hours']:.1f} sigma={signal['sigma']:.4f}",
                    )
                    self._prediction_ids[market["condition_id"]] = pred_id
                except Exception:
                    log.exception("calibration_record_error")

            self._trades_count += 1
            log.info("threshold_trade_executed",
                     asset=asset_key, side=side, price=price, size=bet_size,
                     edge=round(edge, 3), trade_id=trade_id)
        return True
