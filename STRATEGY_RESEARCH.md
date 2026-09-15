# Polymarket Betting Strategies -- Comprehensive Web Research

**Research Date:** April 2026
**Scope:** 20+ web searches, 10+ deep page reads across academic papers, trading blogs, GitHub repos, and data analyses.

---

## Table of Contents

1. [Market Reality & Profitability Statistics](#1-market-reality--profitability-statistics)
2. [The Six Core Profit Models](#2-the-six-core-profit-models)
3. [Bot Strategies That Actually Work](#3-bot-strategies-that-actually-work)
4. [AI/LLM-Powered Forecasting](#4-aillm-powered-forecasting)
5. [Kelly Criterion & Position Sizing](#5-kelly-criterion--position-sizing)
6. [Arbitrage Strategies](#6-arbitrage-strategies)
7. [Market Making Strategies](#7-market-making-strategies)
8. [Whale Analysis: What Top Traders Actually Do](#8-whale-analysis-what-top-traders-actually-do)
9. [Edge Detection & Alternative Data](#9-edge-detection--alternative-data)
10. [Weather Bot Strategy (Niche Alpha)](#10-weather-bot-strategy-niche-alpha)
11. [Tools & Ecosystem](#11-tools--ecosystem)
12. [Academic Findings on Market Efficiency](#12-academic-findings-on-market-efficiency)
13. [Fee Structure & Execution Considerations](#13-fee-structure--execution-considerations)
14. [Resolution Risk & UMA Oracle Edge](#14-resolution-risk--uma-oracle-edge)
15. [Practical Recommendations for Our Bot](#15-practical-recommendations-for-our-bot)

---

## 1. Market Reality & Profitability Statistics

**Hard numbers from on-chain analysis of 86M+ transactions (Apr 2024 - Dec 2025):**

- Only **0.51%** of wallets achieved profits exceeding $1,000
- Only **7.6%** of wallets on Polymarket are profitable overall (~120K profitable vs 1.5M+ losing)
- Whale accounts (>$50K volume) represent only **1.74%** of traders
- **14 of 20** most profitable wallets are bots (~$40M total extracted)
- Average arbitrage window: **2.7 seconds** (down from 12.3s in 2024)
- Total 2025 volume: **95M+ transactions**, $21.5B nominal trading volume

**Key insight:** The vast majority of traders lose. Profitable trading requires systematic edges -- not intuition.

Sources:
- [ChainCatcher: Polymarket 2025 Six Major Profit Models](https://www.chaincatcher.com/en/article/2233047)
- [DataWallet: Top 10 Strategies](https://www.datawallet.com/crypto/top-polymarket-trading-strategies)

---

## 2. The Six Core Profit Models

Analysis of 95M on-chain transactions identified six distinct profitable approaches:

### 2.1 Information Arbitrage (Theo Strategy)
- **Case Study:** French trader "Theo" netted **$85M** by commissioning custom polling ($<100K spent) asking swing state voters about *neighbors'* voting intentions to detect "shy voter" effects
- **Return:** ~85,000% ROI on research investment
- **Barrier:** Extremely high capital + original research methodology
- **Bot applicability:** LOW for replication, but the principle (proprietary data > public data) is universal. Our bot can't commission polls, but can systematically aggregate alternative data sources others ignore.

### 2.2 Cross-Platform Arbitrage
- **Profits extracted:** $40M+ (Apr 2024 - Apr 2025); top 3 wallets earned $4.2M combined
- **Example:** BTC price at $0.45 YES on Polymarket vs $0.48 NO on Kalshi = 7.5% risk-free return
- **Risk:** Settlement definition divergence (e.g., "OPM announcement" vs "actual shutdown >24hrs")
- **Barrier:** Lowest among six strategies; open-source bots exist on GitHub
- **Bot applicability:** HIGH -- requires monitoring multiple platforms and fast execution. Main challenge is cross-platform capital allocation and settlement risk.

### 2.3 High-Probability Bond Strategy ("Favorite Compounder")
- **Returns:** ~5.2% per 3-day trade on near-certainty events (95%+ probability)
- **Annualized equivalent:** ~1,800% with compounding (2 opportunities/week)
- **Practitioner income:** Some earn $150K+/year with few trades/week
- **Data:** >90% of orders exceeding $10K occur at price levels >$0.95
- **Risk:** Black swan events can eliminate dozens of profitable trades in single loss
- **Bot applicability:** MEDIUM -- simple to implement but requires large capital for meaningful returns and careful tail risk management.

### 2.4 Liquidity Provision / Market Making
- **Returns:** 80-200% annualized in new market conditions
- **Case Study:** @defiance_cr generated $700-800/day at peak (started $10K earning ~$200/day)
- **Current Status:** Rewards decreased post-2024 election; competition intensifying
- **Bot applicability:** HIGH -- see detailed section below.

### 2.5 Domain Specialization
- **Case Study:** Trader "Axios" maintains **96% accuracy** on mention markets via historical statement analysis
- **Sports:** HyperLiquid0xb made $1.4M total, single baseball bet = $755K
- **Trade frequency:** Top specialists do only 10-30 trades/year with extremely high confidence
- **Bot applicability:** MEDIUM -- LLMs can serve as domain specialists by ingesting complete historical datasets for specific categories. Best combined with human expertise.

### 2.6 Speed Trading
- **Example:** Fed rate-cut contract jumped $0.65 to $0.78 in **8 seconds** after Powell statement
- **Top traders:** 10,200+ speed trades (2024-2025) generating $4.2M combined
- **Window compression:** Arbitrage windows compressed from minutes to seconds as institutional capital entered
- **Bot applicability:** HIGH -- but requires low-latency infrastructure (dedicated RPC nodes, WebSocket to CLOB, <100ms execution). This is our strongest potential edge if done well.

Sources:
- [ChainCatcher Six Profit Models Report](https://www.chaincatcher.com/en/article/2233047)
- [PolyTrack: French Whale Case Study](https://www.polytrackhq.app/blog/polymarket-french-whale-case-study)

---

## 3. Bot Strategies That Actually Work

From analysis of actual profitable bots in 2025-2026:

### Strategy 1: Automated Market Making
- **Returns:** 1-3% monthly | **Win rate:** 78-85% | **Risk:** Low
- Bot provides liquidity on both YES/NO sides, capturing spreads
- Rebalances every 30 seconds based on inventory levels
- Withdraws liquidity before major news events
- Sets inventory limits (never >30% on one side)
- Auto-widens spreads during volatility spikes
- Batches transactions to reduce gas costs
- **Real performance:** $1,247 on $10K capital (12.47%) over 3 weeks

### Strategy 2: AI-Powered Probability Arbitrage
- **Returns:** 3-8% monthly | **Win rate:** 65-75% | **Risk:** Medium
- Deploys multiple LLMs (GPT-4, Claude) analyzing identical headlines
- Cross-references Reuters, AP, Bloomberg APIs
- Executes when market price diverges >15% from AI consensus
- **Real example:** Trump indictment witness recanted -- bot captured 13c spread on $2K position = $896 profit in <10 minutes
- Uses Kelly Criterion sizing based on confidence intervals
- Monitors 1,200+ verified expert accounts for sentiment

### Strategy 3: Correlation & Logical Arbitrage
- **Returns:** 2-5% monthly | **Win rate:** 70-80% | **Risk:** Low-Medium
- Maps logical relationships between all active markets using graph theory
- Flags probability violations >3%
- Executes multi-leg trades within 500ms windows
- **Example:** "Chiefs win Super Bowl" at 28% while "AFC team wins" at 24% (mathematical impossibility)
- Non-directional, market-neutral, pure mathematics
- Average holding period: 2.3 days

### Strategy 4: High-Frequency Momentum Trading
- **Returns:** 8-15% monthly | **Win rate:** 60-70% | **Risk:** High
- Monitors orderbook changes every 100ms
- Aggregates 6 real-time news sources simultaneously
- Executes only when 3+ signals align
- Uses trailing stops locking 50% of gains
- Circuit breaker: pauses at -5% daily drawdown
- Requires sub-100ms latency via dedicated Polygon RPC nodes

### Portfolio Mix Results (Dec 2025 - Feb 2026)
| Mix | Composition | Return | Max Drawdown |
|-----|------------|--------|-------------|
| Conservative | 80% Arbitrage, 20% MM | 4.2% | 0.8% |
| Balanced | 50% Arb, 30% AI, 20% MM | 11.7% | 3.2% |
| Aggressive | 30% Arb, 50% AI/Momentum, 20% MM | 23.4% | 8.9% |

Sources:
- [Medium/Illumination: 4 Bot Strategies That Profit](https://medium.com/illumination/beyond-simple-arbitrage-4-polymarket-strategies-bots-actually-profit-from-in-2026-ddacc92c5b4f)

---

## 4. AI/LLM-Powered Forecasting

### Current LLM Performance vs Humans

**Key study -- "Wisdom of the Silicon Crowd" (Science Advances 2024):**
- Ensemble of 12 LLMs vs 925 human forecasters on 31 binary questions
- **LLM ensemble Brier score: 0.20** vs **Human crowd: 0.19** (statistically indistinguishable)
- Best individual model: GPT-4 (0.15 Brier), worst: Coral Command (0.38)
- Cost: ~$1 per forecast (massively cheaper than human tournaments)
- **Critical finding:** Presenting GPT-4 with human median forecasts improved accuracy from 0.17 to 0.14 (17% improvement). Claude 2 improved 28%.

**ForecastBench results (2025-2026):**
- Frontier LLMs exceed average human crowd but fall short of superforecasters
- Superforecasters: Brier 0.023 vs o3: 0.135
- **Projected parity:** LLMs match superforecasters ~November 2026 (95% CI: Dec 2025 - Jan 2028)
- All LLMs perform better on political forecasting than economic predictions

**Calibration (2025 Epistemic Study):**
- Claude Opus 4.5: best calibration (ECE=0.120)
- GPT-5.2-XHigh: worse calibration despite comparable accuracy (ECE=0.395)
- All models show systematic overconfidence
- Raw LLM outputs cluster around 0.4-0.6; **Platt scaling** provides the most effective correction

### Practical Implementation for Bots

**What works:**
1. **Multi-model ensemble** (3-5 LLMs) with median/mean aggregation
2. **Superforecaster prompting** -- tell models they are superforecasters, include "commandments"
3. **Chain-of-thought reasoning** with curated information (50+ sources per question)
4. **Post-hoc calibration** via Platt scaling or temperature scaling
5. **Information injection** -- showing models current market prices improves accuracy
6. **AIA Forecaster system** (agentic search + supervisor + Platt scaling) achieves performance indistinguishable from superforecaster median

**What doesn't work:**
- Single model predictions without calibration
- Raw probability outputs (biased toward 50/50 due to RLHF)
- Models without internet access or curated data
- Predictions on purely stochastic events (short-term economics)

**Biases to correct for:**
- Acquiescence bias: models favor positive outcomes (mean 57.35% vs 45% actual positive resolution rate)
- Rounding bias: cluster at 50%, zero predictions at 49% or 51%
- Overconfidence in extreme probabilities

Sources:
- [PMC: Wisdom of the Silicon Crowd](https://pmc.ncbi.nlm.nih.gov/articles/PMC11800985/)
- [arXiv: LLMs vs Superforecasters](https://arxiv.org/html/2507.04562v3)
- [arXiv: Epistemic Calibration via Prediction Markets](https://arxiv.org/html/2512.16030v1)

---

## 5. Kelly Criterion & Position Sizing

### Core Formula for Prediction Markets

**Standard Kelly:**
```
f* = (bp - q) / b
```
Where:
- p = your true probability forecast
- q = 1 - p
- b = (1 - market_price) / market_price (net odds ratio)

**Applied example:** Market prices "Fed Rate Cut" at $0.60. Your model says 75%.
- b = 0.40/0.60 = 0.667
- f* = (0.667 * 0.75 - 0.25) / 0.667 = **0.375 (37.5% of bankroll)**

### Fractional Kelly (What Practitioners Use)

**Full Kelly problems:**
- 33% probability of halving bankroll before doubling
- Extremely sensitive to probability estimation errors
- Assumes perfect knowledge of true probabilities

**Practical recommendation:** Use **0.15x to 0.40x Kelly** depending on model accuracy:
- Brier score 0.10-0.15: use 0.30-0.40x
- Brier score 0.15-0.25: use 0.20-0.30x
- Brier score 0.25+: use 0.10-0.15x

**From academic paper (arXiv 2412.14144):**
- Errors in probability estimation harm performance MORE severely than equivalent investment fraction errors
- The gap between price and probability "can be wide" even with simple investor structures
- Risk aversion effects mean market prices don't exactly equal probabilities

### Bot Implementation Guidelines

1. **Never exceed 5% of bankroll per single market** (hard cap)
2. **Never exceed 2% on any single market with model uncertainty** (soft cap)
3. **Track running Kelly fraction** across all open positions (total exposure)
4. **Reduce Kelly when drawdown exceeds threshold** (e.g., halve at -15% drawdown)
5. **Use per-trade caps** -- e.g., `min(kelly_size, 0.02 * bankroll)`
6. Our current setting of 0.25 Kelly fraction is within recommended range

Sources:
- [arXiv: Kelly Criterion in Prediction Markets](https://arxiv.org/html/2412.14144v1)
- [Substack: Math of Prediction Markets](https://navnoorbawa.substack.com/p/the-math-of-prediction-markets-binary)

---

## 6. Arbitrage Strategies

### 6.1 Binary Complement Arbitrage (YES + NO < $1)
- Buy both sides when combined ask < $1.00
- Typical profit: $0.02-0.05 per contract
- Windows last seconds; requires automated scanning
- Near-zero risk if both orders fill (use FOK orders)

### 6.2 Multi-Outcome Bundle Arbitrage
- In categorical markets, sum cheapest asks for every outcome
- If total < $1.00, buy all outcomes
- **Example:** Oscars Best Picture -- all nominees at combined $0.97 = guaranteed $0.03 profit
- Risk: order book depth on shallowest leg

### 6.3 Cross-Platform Arbitrage (Polymarket vs Kalshi vs Robinhood)
- **$40M+ extracted** Apr 2024 - Apr 2025
- Tools: EventArb.com (free calculator), ArbBets (AI-driven), Matchr (1,500+ markets)
- **Critical risk:** Settlement definition divergence between platforms
- Requires capital on multiple platforms simultaneously
- Most retail-accessible opportunities hover around 2-3% (often fee-negative after costs)

### 6.4 Correlation/Logical Arbitrage
- "Trump wins 2028" cannot trade lower than "Republican wins 2028"
- Cumulative probabilities in mutually exclusive markets summing >100%
- Requires graph-theory mapping of all market relationships
- **Average opportunity:** 2.3 day holding period, 2-5% monthly return

### 6.5 Term-Structure Spreads
- Compare same-theme contracts across different expiry dates
- **Example:** BTC >$100K on Sept 20 at 46% vs Nov 13 at 48% -- flat curve signals mispricing
- Construct hedged pairs: buy underpriced date, sell overpriced date

### Key Challenge
- 78% of arbitrage opportunities in low-volume markets failed due to execution inefficiencies (2025 study)
- Institutional capital has compressed windows from minutes to seconds
- A single bot turned $313 into $414,000 in one month through temporal arbitrage -- but this is exceptional

Sources:
- [DataWallet: Top 10 Strategies](https://www.datawallet.com/crypto/top-polymarket-trading-strategies)
- [EventArb Calculator](https://www.eventarb.com/)
- [MEXC: Five Arbitrage Strategies](https://www.mexc.com/news/584334)

---

## 7. Market Making Strategies

### How It Works on Polymarket
- Post bid (buy) and ask (sell) on both YES and NO sides simultaneously
- Capture the spread when both sides fill
- Earn additional income from Polymarket's Liquidity Rewards Program

### Key Parameters
- **Spread width:** 2-4 cents for liquid markets, 5-10 cents for illiquid
- **Rebalancing frequency:** Every 30-60 seconds
- **Inventory limit:** Never exceed 30% on one side
- **Volatility adjustment:** Auto-widen spreads during news events
- **Two-sided bonus:** Polymarket rewards nearly 3x for orders on both sides vs one side

### Open-Source Implementation
- **poly-maker** (github.com/warproxxx/poly-maker): Automated MM bot with Google Sheets config
- Two components: (1) Data module calculating volatility, (2) Execution module with preset parameters
- Focuses on low-volatility markets with high liquidity rewards

### Performance Benchmarks
- Starting capital $10K: ~$200/day initially
- Scaled performance: $700-800/day at peak
- Monthly return: 1-3% (conservative), up to 200% annualized in new markets

### Critical Insight
"Polymarket's reward system isn't perfectly calibrated to risk" -- some low-volatility markets offer disproportionately high rewards. Systematic identification of these is a legitimate edge.

### Competitive Landscape
- Post-2024 election: rewards decreased, competition intensifying
- Infrastructure costs now exceed typical salaries for serious participants
- Requires: WebSocket connections to CLOB, quote management (hundreds of orders/sec), inventory tracking, rewards system integration

Sources:
- [Polymarket News: Automated Market Making](https://news.polymarket.com/p/automated-market-making-on-polymarket)
- [GitHub: poly-maker](https://github.com/warproxxx/poly-maker)

---

## 8. Whale Analysis: What Top Traders Actually Do

### Deep analysis of 27,000 trades by Polymarket's top 10 whales (PANews):

**Critical finding:** Displayed win rates dramatically overstate actual performance due to "zombie orders" (unclosed positions).

| Trader | Reported Win Rate | Actual Win Rate | Monthly Profit | Strategy |
|--------|------------------|-----------------|----------------|----------|
| SeriouslySirius | 73.7% | **53.3%** | ~$3.29M | Multi-directional hedging (11+ directions) |
| DrPufferfish | 83.5% | **50.9%** | ~$2.06M | Diversified betting (27 teams simultaneously) |
| gmanas | N/A | **51.8%** | ~$1.97M | High-frequency systematic (2,400+ predictions) |
| simonbanza | N/A | **57.6%** | ~$1.04M | Probability fluctuation trading (exits before settlement) |
| gmpm | N/A | **56.16%** | ~$2.93M (total) | Asymmetric hedging (larger on high-prob) |
| Swisstony | N/A | N/A | ~$860K | Small-scale arbitrage ($156 avg profit/trade, 5,527 trades) |
| 0xafEe | N/A | **69.5%** | ~$929K | Selective (0.4 trades/day), Google trends + pop culture |
| RN1 | N/A | **42%** | **-$920K** | Negative example -- over-invested in low-prob outcomes |

### Five Critical Insights from Whale Data

1. **Hedging complexity exceeds social media narratives** -- real strategies use sports metrics (over/under, spreads) beyond simple yes/no
2. **Copy trading proves ineffective** -- market depth limitations prevent replication; actual win rates rarely exceed 70%
3. **Profit/loss ratio management > win rate** -- SeriouslySirius wins 53% but has 2.52 P/L ratio; DrPufferfish wins 51% but has 8.62 P/L ratio
4. **Success requires proprietary decision algorithms** -- top traders have undisclosed analytical methods
5. **Market scaling limitations** -- December's top earner capped at ~$3M; market size constrains returns

### Key Takeaway for Our Bot
The whales who profit consistently have **mediocre win rates (50-57%)** but **excellent position management**. The edge is in *how much* you bet when you're right vs wrong, not in being right more often. This validates our Kelly Criterion approach.

Sources:
- [PANews: Analysis of 27,000 Trades by Top 10 Whales](https://www.panewslab.com/en/articles/516262de-6012-4302-bb20-b8805f03f35f)

---

## 9. Edge Detection & Alternative Data

### Where Edges Come From
1. **Speed** -- processing news faster than the market reprices
2. **Information** -- proprietary or alternative data others don't use
3. **Mathematics** -- logical inconsistencies between correlated markets
4. **Behavioral** -- exploiting known biases (longshot bias, recency bias, panic selling)

### Market Inefficiency Data Points
- Markets under **$100K volume** exhibit **61% accuracy rates** (severe mispricing)
- Markets become reliable only above $100K volume
- Daily returns show **negative autocorrelation** (momentum doesn't persist)
- Retail traders systematically overpay low-probability outcomes ($0.01-$0.15)

### Alternative Data Sources for Edge
| Data Source | Application | Lead Time | Access |
|------------|-------------|-----------|--------|
| Google Trends | Voter engagement, consumer demand | 1-4 weeks | Free (API) |
| Amazon search volume | Consumer product predictions | 2-6 weeks | Paid |
| NOAA weather data | Weather market predictions | 1-7 days | Free (API) |
| Satellite imagery | Economic activity, supply chain | Variable | Paid ($$$) |
| Social media sentiment | Political, crypto markets | Hours-days | API costs |
| Wikipedia page views | Topic trending/relevance | Days | Free |
| App download data | Tech company predictions | Weeks | Paid |
| Reddit/TikTok analytics | Pop culture, mentions markets | Hours-days | API costs |
| Binance/Coinbase feeds | Crypto market latency arb | Milliseconds | Free (WebSocket) |

### Tools for Edge Detection
- **Paradox Intelligence** -- aggregates Google Trends, Amazon, YouTube, TikTok, Reddit, Wikipedia, news sentiment
- **PolymarketAI** -- overlays real-time AI analysis showing edge, fair value, risk on every outcome
- **Forcazt** -- AI analytics for high-alpha market discovery and arbitrage
- **Inside Edge** -- identifies market inefficiencies with quantified edge percentages
- **Polysights** -- uses Vertex AI + Gemini with 30+ custom metrics + insider trade detection

Sources:
- [Paradox Intelligence: Alternative Data for Polymarket](https://www.paradoxintelligence.com/blog/how-to-use-alternative-data-predict-polymarket-outcomes)
- [DeFiPrime: 170+ Polymarket Tools](https://defiprime.com/definitive-guide-to-the-polymarket-ecosystem)

---

## 10. Weather Bot Strategy (Niche Alpha)

### Why Weather Markets Are Profitable
- Professional meteorological data (NOAA, GFS ensemble) is **freely available** and highly accurate
- Polymarket prices often **diverge significantly** from forecast data
- Low competition compared to political/crypto markets
- Micro-bet friendly ($1-$3 per position)

### The Strategy
1. Pull latest NOAA/GFS forecast for target city
2. Compare predicted temperature range to Polymarket prices
3. If NOAA says 43F but market prices that range at 15% ($0.15), there's a massive edge
4. Buy at $0.15, win $1.00 when forecast proves correct

### Real Performance
- One address scaled **$1,000 to $24,000** since April 2025 on London weather
- Another bot printed **$65,000** across NYC, London, Seoul weather events
- Trader "meropi" made **$30,000** using fully automated $1-$3 micro bets
- Some traders achieved **9,000%+ returns** over 3,000+ predictions

### Implementation Details
- **Data source:** NOAA's 31-member GFS ensemble forecasts
- **Entry rules:** Buy YES shares priced <$0.15 when models strongly support; buy NO shares priced >$0.45 when models show unlikely
- **Win rates:** Typically 50-80% with high payoff asymmetry
- **Bet sizing:** $1-$3 per market (Kelly on micro scale)
- **Markets:** Temperature ranges for major cities (NYC, London, Seoul, etc.)
- **GitHub:** `hcharper/polyBot-Weather`, `suislanchez/polymarket-kalshi-weather-bot`, `alteregoeth-ai/weatherbot`

### Bot Applicability: VERY HIGH
This is arguably the most accessible automated strategy:
- Free data sources
- Clear resolution criteria (actual temperature)
- Mathematical edge (forecast vs market price)
- Low capital requirements
- Scalable across many cities/days

Sources:
- [Dev Genius: Weather Trading Bots Making $24K](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09)
- [GitHub: polymarket-kalshi-weather-bot](https://github.com/suislanchez/polymarket-kalshi-weather-bot)

---

## 11. Tools & Ecosystem

### 170+ tools across the Polymarket ecosystem. Key categories:

**Official:**
- `py-clob-client` -- Python SDK for CLOB trading
- `polymarket-us-python` -- US-specific SDK
- Polymarket Agents framework (GitHub) -- modular AI agent architecture

**AI Agents:**
- Alphascope, PolyBro, Billy Bets (sports), Astron (98% short-term accuracy claimed), Polyseer (open-source, Bayesian aggregation)
- Browser extensions: Polyprophet, PolyPulse (free, Perplexity AI integration), PolyOracle (multi-LLM consensus)

**Copy Trading:**
- KreoPoly (Telegram), Polycop (sub-second replication), Specula ("Conviction Score" system)
- Whale trackers: PolyTracker, Polywhaler ($10K+ trades), LayerHub

**Analytics:**
- Polymarket Analytics (real-time, Goldsky-powered, 5-min updates)
- HashDive ("Smart Scores" -100 to 100), Polysights (Vertex AI + Gemini, 30+ metrics)
- Polymtrade (mobile terminal, AI trained on 55K+ resolved markets)

**Arbitrage:**
- EventArb.com (free cross-platform calculator)
- ArbBets (AI-driven), Matchr (1,500+ markets)

**Alerts:**
- PolyAlertHub, PolySpyBot (new market discovery), Tremor.live (unusual volatility detection)

**Data APIs:**
- Dome (unified normalized data), PolyRouter, FinFeedAPI
- Goldsky (infrastructure behind Polymarket Analytics)

Sources:
- [DeFiPrime: Definitive Guide to Polymarket Ecosystem](https://defiprime.com/definitive-guide-to-the-polymarket-ecosystem)
- [GitHub: Awesome-Prediction-Market-Tools](https://github.com/aarora4/Awesome-Prediction-Market-Tools)

---

## 12. Academic Findings on Market Efficiency

### 2024 Election Study (2,500 markets, $2B+ volume)
- Prices for identical contracts **diverged across exchanges**
- Daily price changes were **weakly correlated or negatively autocorrelated**
- Arbitrage opportunities **peaked in the final two weeks** before Election Day
- More liquid markets substantially **outperform polls** in predicting results
- Polymarket **leads Kalshi** in price discovery when liquidity is high

### Market Microstructure
- Polymarket's CLOB is hybrid-decentralized: off-chain matching, on-chain settlement
- Linear slippage: dp ~ Q / (2 * orderbook_depth)
- 2023 migration from AMM to CLOB improved capital efficiency significantly
- **Longshot bias:** Retail overpays low-probability outcomes systematically
- **Recency bias:** Prices overreact to news then mean-revert

### Practical Implications
1. Markets are **not fully efficient**, especially during high-information periods
2. Cross-platform price divergence is persistent (not quickly arbitraged away)
3. Negative autocorrelation in daily returns = **mean reversion is profitable**
4. Volume matters: markets <$100K are significantly mispriced

Sources:
- [SSRN: Price Discovery in Modern Prediction Markets](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5331995)
- [SSRN: Accuracy and Efficiency in 2024 Presidential Election](https://ideas.repec.org/p/osf/socarx/d5yx2_v1.html)

---

## 13. Fee Structure & Execution Considerations

### Polymarket Fees
- **Maker (limit orders):** Zero fees + rebate of 20-50% of taker fees
- **Taker (market orders):** 0.30% (30 bps) on Total Contract Premium
- **High-frequency crypto markets:** Up to 1.80% peak fees
- **Maker rebate formula:** Varies by category (Finance: up to 50%, Politics: ~25%)

### Execution Best Practices
- For small bets (<$20): market orders are acceptable in liquid markets
- For anything >$20 or illiquid: **always use limit orders** (maker rebates)
- FOK (Fill or Kill) for arbitrage (prevents partial fills)
- GTC orders require **heartbeat every 5 seconds** (auto-cancel after 10s)
- For simple $10 starter bets: FOK market orders (simpler, no heartbeat needed)
- Minimum: 5 shares for limit orders

### Spread Calculation
- Market maker posts bid at 53c and offer at 57c (4c spread)
- If both fill: 4 cents/share profit regardless of outcome
- **Formula:** Spread profit = (ask - bid) * position_size - fees

Sources:
- [Polymarket Docs: Fees](https://docs.polymarket.com/trading/fees)
- [Start Polymarket: Market Making](https://startpolymarket.com/strategies/market-making/)

---

## 14. Resolution Risk & UMA Oracle Edge

### How Resolution Works
1. Proposer submits outcome + bond (USDC.e, forfeited if wrong)
2. 2-hour challenge period
3. If disputed once: new request created (same params)
4. If disputed again: escalated to UMA DVM (token holder vote)

### Trading Edge from Resolution Risk
- Older markets carry higher resolution risk
- **Strategy:** If most participants avoid ambiguous markets, but you've analyzed resolution criteria carefully and one outcome is overwhelmingly likely to be determined correct by UMA voters, you find value others miss
- Requires deep understanding of UMA voting patterns and resolution criteria

### Known Risks
- March 2025: UMA governance attack -- concentrated voting power settled $7M contract incorrectly
- Polymarket confirmed UMA reached incorrect outcome
- Settlement definition divergence between platforms (e.g., "announcement" vs "actual event")

Sources:
- [Polymarket Docs: Resolution](https://docs.polymarket.com/polymarket-learn/markets/how-are-markets-resolved)
- [PolyTrack: Disputes & UMA](https://www.polytrackhq.app/blog/polymarket-resolution-disputes-uma)

---

## 15. Practical Recommendations for Our Bot

### High-Priority Strategies to Implement

**Tier 1 -- Implement First (Highest ROI vs Effort):**

1. **Weather Bot Module**
   - Add NOAA/GFS ensemble data integration
   - Compare forecast probability distributions to Polymarket prices
   - Micro-bet ($1-3) on high-edge opportunities
   - Expected: 50-80% win rate with asymmetric payoffs
   - Implementation: API calls to NOAA, simple probability comparison

2. **Multi-Model AI Ensemble** (enhance existing)
   - Add 3rd+ model (currently dual-model per RESEARCH.md)
   - Implement Platt scaling for calibration
   - Add superforecaster system prompts
   - Show models current market prices (improves accuracy 17-28%)
   - Target: Brier score <0.20

3. **Logical/Correlation Arbitrage Scanner**
   - Build graph of related markets
   - Flag when mutually exclusive outcomes sum >100% or conditional probabilities violate logic
   - Pure math, no prediction needed
   - Expected: 2-5% monthly, 70-80% win rate

**Tier 2 -- Implement Next:**

4. **News Speed Trading**
   - Already partially implemented (Brave Search)
   - Add WebSocket feeds for real-time news
   - Reduce decision-to-execution latency
   - Target: <30 second news-to-trade pipeline

5. **Market Making Module**
   - Implement two-sided quoting in low-volatility markets
   - Earn Polymarket liquidity rewards
   - Use poly-maker as reference implementation
   - Requires: heartbeat management, inventory tracking

6. **Cross-Platform Arbitrage** (Polymarket + Kalshi)
   - Monitor same events across platforms
   - Execute when spread > fees + capital cost
   - Biggest risk: settlement divergence

**Tier 3 -- Longer-Term:**

7. **Alternative Data Integration**
   - Google Trends API (free)
   - GDELT Project (free, already noted in RESEARCH.md)
   - Reddit/social sentiment for mention markets
   - Satellite/app data for specialized markets (paid)

8. **Copy Trading Signal Generation**
   - Track top wallets on-chain
   - Generate signals (don't blindly copy -- whale data shows mediocre win rates)
   - Use as one input among many

### Key Configuration Recommendations

Based on research findings, current bot settings appear well-calibrated:
- **Kelly fraction 0.25:** Within recommended 0.15-0.40 range -- keep it
- **min_edge 0.04:** Good, research shows markets <$100K often have 5-15% mispricing
- **min_confidence 0.60:** Research suggests raising to **0.70-0.75** for better accuracy
- **Max position per market:** Should be hard-capped at **2-5% of bankroll**
- **Daily loss limit:** Should be **5% of bankroll** (circuit breaker)
- **Number of models in ensemble:** Increase from 2 to **3-5** for better calibration

### Critical Implementation Notes

1. **Probability estimation errors hurt more than sizing errors** (academic finding) -- invest more in prediction quality than position sizing optimization
2. **Mean reversion works** -- negative autocorrelation in daily returns means contrarian plays after sharp moves have edge
3. **Volume filter is essential** -- avoid markets under $100K volume unless you have a strong domain-specific edge (like weather data)
4. **Maker orders only** for non-arbitrage trades (zero fees + rebate)
5. **Track Brier score per category** separately -- the bot may be good at crypto but bad at politics
6. **Weather markets are the low-hanging fruit** -- free data, clear edge, low competition
