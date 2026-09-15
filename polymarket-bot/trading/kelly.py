from dataclasses import dataclass

import structlog

log = structlog.get_logger()


@dataclass
class KellyResult:
    bet_fraction: float  # fraction of bankroll to bet
    bet_size: float  # dollars to bet
    expected_value: float  # expected value per dollar bet
    edge: float  # probability advantage over market


class KellyCalculator:
    def __init__(
        self,
        default_fraction: float = 0.25,
        max_position_pct: float = 0.20,
        min_bet: float = 0.50,
    ) -> None:
        self._default_fraction = default_fraction
        self._max_position_pct = max_position_pct
        self._min_bet = min_bet

    def calculate(
        self,
        estimated_prob: float,
        market_price: float,
        bankroll: float,
        kelly_fraction: float | None = None,
    ) -> KellyResult | None:
        if bankroll <= 0:
            return None

        fraction = kelly_fraction or self._default_fraction

        # In a prediction market, buying YES at price p:
        # If YES resolves: you get $1/share, profit = (1 - p) per share
        # If NO resolves: you lose p per share
        # Odds (net payout per dollar risked) = (1 - p) / p
        # But we need to use our estimated probability, not market's

        p = estimated_prob  # our estimated probability of YES
        price = market_price  # market price of the token we're buying

        # For buying YES at `price`:
        # Win probability: p, win amount per share: (1 - price)
        # Lose probability: (1-p), lose amount per share: price
        # Edge = p * (1 - price) - (1 - p) * price = p - price
        edge = p - price

        if edge <= 0:
            return None

        # Kelly fraction for binary bets:
        # f* = (p - price) / (1 - price)
        if price <= 0 or price >= 1.0:
            return None

        full_kelly = edge / (1.0 - price)

        # Cap full Kelly at 1.0 to prevent over-betting on extreme edges
        full_kelly = min(full_kelly, 1.0)

        # Apply fractional Kelly
        adjusted_kelly = full_kelly * fraction

        # Clamp to max position percentage
        adjusted_kelly = min(adjusted_kelly, self._max_position_pct)

        # Calculate bet size in dollars
        bet_size = bankroll * adjusted_kelly

        # Minimum bet check — if Kelly says bet less than min, skip entirely
        if bet_size < self._min_bet:
            if bankroll < self._min_bet * 2:
                return None
            bet_size = self._min_bet

        # Expected value per dollar risked:
        # You spend `price` to buy 1 share. If win: get $1 (profit = 1-price). If lose: get $0.
        # EV per dollar spent = p * (1/price) + (1-p) * 0 - 1 = p/price - 1
        ev = p / price - 1.0

        log.debug(
            "kelly_calculated",
            estimated_prob=p,
            market_price=price,
            edge=edge,
            full_kelly=full_kelly,
            adjusted_kelly=adjusted_kelly,
            bet_size=bet_size,
            ev=ev,
        )

        return KellyResult(
            bet_fraction=adjusted_kelly,
            bet_size=round(bet_size, 2),
            expected_value=ev,
            edge=edge,
        )
