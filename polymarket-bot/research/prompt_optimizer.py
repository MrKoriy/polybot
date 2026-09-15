"""Level 2: Prompt optimization — like autoresearch modifies model architecture.

Generates prompt variations, tests them via Brier score on paper trades,
keeps the best-performing prompt.
"""

import random
from dataclasses import dataclass
from datetime import datetime

import structlog

import analysis.prompts as prompts_module
from research.experiment_log import ExperimentLog, ExperimentResult

log = structlog.get_logger()

# Prompt variation strategies
SYSTEM_PROMPT_VARIANTS = [
    # Variant 1: More aggressive — seek disagreement with market
    {
        "id": "aggressive",
        "description": "Encourage independent thinking, disagree with market more",
        "system_suffix": """

ADDITIONAL INSTRUCTION: Markets are often slow to react to breaking news. If fresh news
(<6 hours old) clearly impacts the outcome, your estimate SHOULD diverge from the market price.
Do not anchor to the market price when news provides strong signal.""",
    },
    # Variant 2: Bayesian reasoning
    {
        "id": "bayesian",
        "description": "Explicit Bayesian updating from base rate",
        "system_suffix": """

USE BAYESIAN REASONING:
1. Start with the base rate (market price is the prior).
2. For each piece of news, calculate the likelihood ratio: how much more likely is this
   news if YES vs NO?
3. Update your posterior step by step.
4. Your final probability should be the posterior after all evidence.""",
    },
    # Variant 3: Sports-focused
    {
        "id": "sports_expert",
        "description": "Sports-specific analysis with stats focus",
        "system_suffix": """

SPORTS ANALYSIS MODE:
- Focus on team form (last 5-10 games), head-to-head records, injuries, rest days.
- For O/U markets: consider pace of play, defensive ratings, recent scoring trends.
- For spread markets: consider home/away advantage, motivation (playoff implications).
- Recent team news (injuries, lineup changes) is MUCH more valuable than general commentary.
- Ignore fan sentiment — it's noise. Focus on statistical evidence.""",
    },
    # Variant 4: Contrarian
    {
        "id": "contrarian",
        "description": "Look for overreactions and fade the crowd",
        "system_suffix": """

CONTRARIAN ANALYSIS:
- Markets often OVERREACT to recent news. A team lost last game → market drops too much.
- Look for reversion to the mean: is the current price an overreaction?
- If all news points one direction and the market has already moved, the edge is likely gone.
- The best edges come from situations where recent news DOESN'T match the real probability
  (e.g. one bad game doesn't make a good team bad).""",
    },
    # Variant 5: Minimal — just answer directly
    {
        "id": "minimal",
        "description": "Minimal prompt, let model use its own reasoning",
        "system_suffix": "",
        "system_override": """You are a prediction market probability estimator.
Given a market question, current price, and recent news, estimate the true probability.
Be precise. Output calibrated probabilities. Do not be overconfident.
If news is already priced in, stay near market price.
If fresh news provides new information, deviate from market price accordingly.""",
    },
    # Variant 6: Step-by-step with explicit scoring
    {
        "id": "scoring",
        "description": "Explicit evidence scoring before probability",
        "system_suffix": """

EVIDENCE SCORING METHOD:
Before giving your final estimate, score each piece of evidence:
- Relevance (0-10): How directly does this affect the outcome?
- Freshness (0-10): How recent is this? (>6h old = stale = low score)
- Reliability (0-10): Is this from a reliable source or speculation?
Only evidence scoring 15+ total should significantly move your estimate from market price.""",
    },
]

USER_PROMPT_VARIANTS = [
    # Variant 1: Shorter, more direct
    {
        "id": "direct",
        "description": "Shorter user prompt, less chain-of-thought guidance",
        "template": """Market: "{question}"
Price: {market_price:.3f} ({market_pct:.1f}%)
Resolves: {end_date}

News ({news_count} items, last {lookback_hours}h):
{formatted_news}

Estimate the true probability. JSON only:
{{"probability": <0.01-0.99>, "confidence": <0.0-1.0>, "reasoning": "<brief>", "key_factors": [...], "counter_arguments": [...]}}""",
    },
    # Variant 2: With explicit market context
    {
        "id": "market_context",
        "description": "Add market efficiency context",
        "template": """Market question: "{question}"
Current market price (YES): {market_price:.3f} ({market_pct:.1f}% probability)
Resolution date: {end_date}

⚠️ IMPORTANT: This is a prediction market with real money. The current price of {market_pct:.1f}%
reflects the aggregate wisdom of many bettors. Only deviate significantly if the news below
provides STRONG, FRESH evidence that the market hasn't yet priced in.

Recent news and signals ({news_count} items, last {lookback_hours}h):
{formatted_news}

Analysis steps:
1. What's the base rate (use market price as prior)?
2. Does any news provide a genuine update?
3. How likely is this news already priced in?
4. Final calibrated probability?

JSON response:
{{"probability": <float 0.01-0.99>, "confidence": <float 0.0-1.0>, "reasoning": "<2-3 sentences>", "key_factors": ["<factor>", ...], "counter_arguments": ["<counter>", ...]}}""",
    },
]


@dataclass
class PromptExperiment:
    system_variant_id: str
    user_variant_id: str | None
    description: str
    old_system_prompt: str
    old_user_prompt: str
    new_system_prompt: str
    new_user_prompt: str


class PromptOptimizer:
    """Tests prompt variations, measures Brier score, keeps improvements."""

    def __init__(
        self,
        experiment_log: ExperimentLog,
        cycles_per_experiment: int = 10,
    ) -> None:
        self._log = experiment_log
        self._cycles_per_experiment = cycles_per_experiment
        self._experiment_count = 0
        self._current_experiment: PromptExperiment | None = None
        self._tested_variants: set[str] = set()

        # Track baseline prompts
        self._baseline_system = prompts_module.PROBABILITY_ESTIMATION_SYSTEM
        self._baseline_user = prompts_module.PROBABILITY_ESTIMATION_USER
        self._best_system = self._baseline_system
        self._best_user = self._baseline_user

        # Measurement
        self._start_brier = 0.0
        self._start_trades = 0
        self._start_wins = 0
        self._start_bankroll = 0.0
        self._start_cycle = 0

    def propose_experiment(self) -> PromptExperiment | None:
        """Propose a prompt variation to test."""
        self._experiment_count += 1

        # Alternate between system and user prompt changes
        if random.random() < 0.7:
            # System prompt variation (more impactful, test more often)
            untested = [v for v in SYSTEM_PROMPT_VARIANTS if v["id"] not in self._tested_variants]
            if not untested:
                # All tested — pick random for re-testing
                self._tested_variants.clear()
                untested = SYSTEM_PROMPT_VARIANTS

            variant = random.choice(untested)
            self._tested_variants.add(variant["id"])

            if "system_override" in variant:
                new_system = variant["system_override"]
            else:
                new_system = self._best_system + variant["system_suffix"]

            experiment = PromptExperiment(
                system_variant_id=variant["id"],
                user_variant_id=None,
                description=f"system_prompt: {variant['description']}",
                old_system_prompt=prompts_module.PROBABILITY_ESTIMATION_SYSTEM,
                old_user_prompt=prompts_module.PROBABILITY_ESTIMATION_USER,
                new_system_prompt=new_system,
                new_user_prompt=prompts_module.PROBABILITY_ESTIMATION_USER,
            )
        else:
            # User prompt variation
            variant = random.choice(USER_PROMPT_VARIANTS)
            experiment = PromptExperiment(
                system_variant_id="",
                user_variant_id=variant["id"],
                description=f"user_prompt: {variant['description']}",
                old_system_prompt=prompts_module.PROBABILITY_ESTIMATION_SYSTEM,
                old_user_prompt=prompts_module.PROBABILITY_ESTIMATION_USER,
                new_system_prompt=prompts_module.PROBABILITY_ESTIMATION_SYSTEM,
                new_user_prompt=variant["template"],
            )

        log.info(
            "prompt_experiment_proposed",
            system_variant=experiment.system_variant_id,
            user_variant=experiment.user_variant_id,
            description=experiment.description,
        )
        return experiment

    def apply_experiment(self, experiment: PromptExperiment) -> None:
        """Swap prompts in the live module."""
        self._current_experiment = experiment
        prompts_module.PROBABILITY_ESTIMATION_SYSTEM = experiment.new_system_prompt
        prompts_module.PROBABILITY_ESTIMATION_USER = experiment.new_user_prompt
        log.info("prompt_applied", description=experiment.description)

    def revert_experiment(self) -> None:
        """Revert to previous best prompts."""
        if self._current_experiment:
            prompts_module.PROBABILITY_ESTIMATION_SYSTEM = self._current_experiment.old_system_prompt
            prompts_module.PROBABILITY_ESTIMATION_USER = self._current_experiment.old_user_prompt
            log.info("prompt_reverted")
            self._current_experiment = None

    def start_measurement(self, bankroll: float, total_trades: int, wins: int, brier: float, cycle: int) -> None:
        self._start_bankroll = bankroll
        self._start_trades = total_trades
        self._start_wins = wins
        self._start_brier = brier
        self._start_cycle = cycle

    def evaluate_experiment(
        self,
        bankroll: float,
        total_trades: int,
        wins: int,
        brier_score: float,
        cycle: int,
    ) -> ExperimentResult:
        """Evaluate prompt experiment. Primary metric: Brier score (lower = better)."""
        if not self._current_experiment:
            raise ValueError("No active prompt experiment")

        pnl = bankroll - self._start_bankroll
        new_trades = total_trades - self._start_trades
        new_wins = wins - self._start_wins
        win_rate = new_wins / new_trades if new_trades > 0 else 0.0
        cycles_run = cycle - self._start_cycle

        # Decision logic:
        # Primary: Brier score improvement (lower is better)
        # Secondary: P&L positive
        # Need at least a few trades to judge
        if new_trades < 2:
            status = "discard"  # Not enough data
        elif brier_score < self._start_brier and brier_score > 0:
            status = "keep"  # Better calibration
        elif pnl > 0 and win_rate > 0.5:
            status = "keep"  # Profitable with decent win rate
        else:
            status = "discard"

        variant_id = self._current_experiment.system_variant_id or self._current_experiment.user_variant_id
        exp_id = f"R{self._experiment_count:04d}_{variant_id}"

        result = ExperimentResult(
            experiment_id=exp_id,
            level="prompt",
            description=self._current_experiment.description,
            status=status,
            pnl=pnl,
            win_rate=win_rate,
            brier_score=brier_score,
            trades_count=new_trades,
            cycles_run=cycles_run,
            started_at=datetime.utcnow().isoformat(),
            finished_at=datetime.utcnow().isoformat(),
        )

        self._log.log_result(result)

        if status == "keep":
            self._best_system = prompts_module.PROBABILITY_ESTIMATION_SYSTEM
            self._best_user = prompts_module.PROBABILITY_ESTIMATION_USER
            log.info("prompt_experiment_kept", variant=variant_id, brier=brier_score, pnl=pnl)
        else:
            self.revert_experiment()
            log.info("prompt_experiment_discarded", variant=variant_id, brier=brier_score, pnl=pnl)

        self._current_experiment = None
        return result

    @property
    def cycles_per_experiment(self) -> int:
        return self._cycles_per_experiment

    @property
    def has_active_experiment(self) -> bool:
        return self._current_experiment is not None
