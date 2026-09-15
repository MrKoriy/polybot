# Polymarket Strategy Research - 2026-05-08

## Current Read

The old paper results are not reliable enough for live decisions. They assumed
fills at the model price and often resolved positions deterministically without
checking CLOB fillability, spread, depth, fees, or settlement mechanics.

The live record is the useful truth sample:

- Live closed: 25 trades, PnL about -$68, WR about 32%.
- Legacy paper: strongly positive, but inflated by fake/immediate fills.
- Current city-weather markets: scanner found 0 tradable short-term city-weather
  markets on 2026-05-08.

## What Profitable Traders Usually Exploit

### 1. Market Making + Rebates

Polymarket documents market makers as liquidity providers who continuously post
bid/ask orders and earn the spread for taking inventory risk:

https://docs.polymarket.com/market-makers/overview

Polymarket also runs daily maker rebates. Makers are not charged fees; takers
pay fees, and part of those fees is redistributed to makers who add liquidity
and get filled:

https://docs.polymarket.com/market-makers/maker-rebates

This is currently the most credible bot-shaped strategy for small systematic
edge: quote tight, avoid crossing, cap inventory, and include expected rebate
value. It is not directional 10x payout hunting. It is many small fills plus
rebates, with strict inventory control.

### 2. Liquidity Rewards

Polymarket liquidity rewards score resting orders by tightness and depth around
the midpoint. The program pays daily and rewards balanced quoting:

https://docs.polymarket.com/market-makers/liquidity-rewards

This can be profitable, but only if the bot models competition. If our orders
are too small or too far from midpoint, rewards can be negligible. If quotes are
too aggressive, adverse selection can exceed rewards.

### 3. Structural / Combinatorial Arbitrage

A 2025 paper found Polymarket mispricings between mutually exclusive/exhaustive
outcomes and related markets, estimating historically large realized arbitrage:

https://arxiv.org/abs/2508.03474

This is more promising than pure prediction if we can execute across all legs
with real depth. The hard parts are matching resolution rules, walking book
depth, and avoiding fake arbitrage where one leg cannot fill.

### 4. Copy / Smart-Money Following

PolyIntel describes a realistic copy-trading methodology: cash-flow PnL,
resolved-position marking, ASOF delay, slippage, caps, and bot/sybil filtering:

https://polyintel.io/about

This is useful as a design pattern. Naive copy trading is dangerous because the
visible trade may be late, partial, hedged elsewhere, or part of market making.

### 5. Directional Forecast Models

Weather worked in paper because the old simulation overestimated fillability and
resolution accuracy. With live results, source mismatch and thin books killed
the edge. Directional models can still work, but only where:

- Resolution source is exact.
- Market is liquid enough for entry and exit.
- Edge survives taker fees, spread, and settlement delay.
- We have independent data faster or better than the market.

## Immediate Plan

1. Run `realistic_paper` for at least 24-72 hours.
2. Treat rejected signals as data: if a signal cannot fill in realistic paper,
   it is not a trade.
3. Measure fill rate, slippage, final PnL, drawdown, and category-level WR.
4. Build two new scanners before any live restart:
   - Reward maker scanner: reward/day, spread, depth, expected rebate share,
     inventory risk.
   - Structural arb scanner: mutually exclusive baskets and related-market sums,
     with executable depth.
5. Do not resume live directional weather/threshold until realistic paper has
   at least 100 filled trades or a smaller sample with clear positive expectancy
   after fees/spread.

## Decision Rule

Live mode should stay off unless one of these is true:

- Realistic paper shows positive EV after at least 100 filled trades.
- A structural arb scanner finds executable, depth-adjusted risk-free or
  near-risk-free trades.
- A maker/rebate strategy shows positive expected daily PnL under conservative
  reward-share and adverse-selection assumptions.
