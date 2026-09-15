# 🤖 Polybot — AI-Powered Polymarket Prediction Market Trading Bot

Automated algorithmic trading system and research framework for [Polymarket](https://polymarket.com) prediction markets.

---

## ⚡️ Key Strategies & Features

1. **Weather Arbitrage & Anomaly Engine:**
   - Real-time weather forecasting using NOAA and Open-Meteo models.
   - High-probability exploitation of temperature and precipitation threshold markets.

2. **Complete Sets Arbitrage:**
   - Detects and exploits mispricings where the sum of complementary outcomes $\sum P(Outcome_i) < \$1.00$.
   - Instant guaranteed risk-free spread capture upon settlement.

3. **LLM Edge Detection & Superforecasting:**
   - Multi-agent news aggregation (Brave Search, Google News, Reddit).
   - Ensemble reasoning (Claude / GPT-4o / Gemini) with calibrated Brier scoring and bias correction.

4. **Mathematical Risk Management:**
   - **Fractional Kelly Criterion** (default $0.25$ Kelly) to maximize long-term bankroll growth while avoiding drawdown risk.
   - Dynamic position caps ($\le 5\%$ per single market, max open exposures).
   - Hard circuit-breakers: daily loss stop ($\le 30\%$) and drawdown pause ($\ge 50\%$).

5. **Operational Modes:**
   - **Paper Trading:** Full simulated execution with live orderbook feeds and paper PnL accounting.
   - **Live Trading:** Non-custodial execution via Polymarket CLOB API (Polygon Network).

---

## 📁 Repository Structure

```text
.
├── polymarket-bot/          # Core trading engine & package
│   ├── bot/                 # Bot orchestrator, workers & reconciliation
│   ├── trading/             # Kelly engine, execution & portfolio tracker
│   ├── analysis/            # LLM clients, edge detection & probability estimator
│   ├── data/                # WebSocket clients (Polymarket CLOB, Binance) & news sources
│   ├── core/                # Event bus, scheduler & exceptions
│   ├── config/              # Pydantic settings & logging
│   ├── web/                 # Web monitoring dashboard (FastAPI + HTMX)
│   ├── scripts/             # Diagnostic tools, scanners, systemd unit
│   ├── tests/               # Unit & integration test suite
│   ├── pyproject.toml       # Package dependencies and entrypoints
│   └── .env.example         # Template for environment variables
├── RESEARCH.md              # Research summary and implementation gaps
├── STRATEGY_RESEARCH.md     # Deep-dive analysis of prediction market alpha
├── RESEARCH_STRATEGIES.md   # Six core profit models breakdown
├── polymarket-strategy-overhaul.md # Strategic plan for live production
└── plan.md                  # Development roadmap and milestones
```

---

## 🚀 Quickstart

### 1. Prerequisites
- Python 3.11+
- Polygon (MATIC) RPC & Polymarket account (for live mode)

### 2. Installation

```bash
cd polymarket-bot

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install package in development mode
pip install -e ".[dev]"
```

### 3. Configuration

Copy the sample environment file:

```bash
cp .env.example .env
```

Fill in required keys in `.env`:
- `OPENROUTER_API_KEY` (or direct OpenAI / Anthropic keys)
- `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` (for alerts)
- `POLYMARKET_PRIVATE_KEY` (keep `PAPER_TRADING=true` initially)

### 4. Running the Bot

```bash
# Run via module
python -m bot.app

# Or via installed CLI entrypoint
polybot
```

### 5. Running Diagnostics

```bash
python scripts/check_live_ready.py
```

---

## 🛡 Disclaimer

This software is for educational and research purposes only. Prediction market trading involves financial risk. Always test thoroughly in paper trading mode before committing capital.
