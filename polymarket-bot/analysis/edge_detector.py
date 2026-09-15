from dataclasses import dataclass

import structlog

from config.constants import Direction

log = structlog.get_logger()


@dataclass
class EdgeSignal:
    direction: Direction
    edge_size: float
    estimated_prob: float
    market_price: float
    confidence: float
    token_id: str  # which token to buy
    buy_price: float  # price to buy at
    confidence_adjusted_edge: float  # edge weighted by confidence
    expected_value: float  # expected profit per dollar risked


class EdgeDetector:
    def __init__(self, min_edge: float = 0.10, min_confidence: float = 0.75) -> None:
        self._min_edge = min_edge
        self._min_confidence = min_confidence

    def update_thresholds(self, min_edge: float, min_confidence: float) -> None:
        """Called each cycle so AutoResearch setting changes take effect immediately."""
        self._min_edge = min_edge
        self._min_confidence = min_confidence

    def detect(
        self,
        estimated_prob: float,
        market_yes_price: float,
        market_no_price: float,
        confidence: float,
        yes_token_id: str,
        no_token_id: str,
        market_volume: float = 0,
        spread: float = 0,
    ) -> EdgeSignal | None:
        if confidence < self._min_confidence:
            return None

        # Guard against extreme prices that cause division by zero in EV calc
        if market_yes_price <= 0.01 or market_yes_price >= 0.99:
            log.debug("extreme_yes_price_skip", price=market_yes_price)
            return None
        if market_no_price <= 0.01 or market_no_price >= 0.99:
            log.debug("extreme_no_price_skip", price=market_no_price)
            return None

        # Edge on YES side: our estimate is higher than market price
        yes_edge = estimated_prob - market_yes_price
        # Edge on NO side: our estimate is lower (so NO is underpriced)
        no_edge = (1 - estimated_prob) - market_no_price

        # Deduct half the spread from edge as execution cost estimate
        spread_cost = spread / 2 if spread > 0 else 0

        # Build candidates and pick the best one by confidence-adjusted expected value
        candidates = []

        if yes_edge > spread_cost:
            net_yes_edge = yes_edge - spread_cost
            if net_yes_edge >= self._min_edge:
                # EV per dollar spent: buy 1 share at price P, win pays $1, lose pays $0
                # EV = prob_win * (1/P) - 1  (return per dollar spent)
                ev_per_dollar = estimated_prob / market_yes_price - 1.0
                adj_edge = net_yes_edge * confidence
                candidates.append((
                    Direction.BUY_YES,
                    net_yes_edge,
                    estimated_prob,
                    market_yes_price,
                    yes_token_id,
                    adj_edge,
                    ev_per_dollar,
                ))

        if no_edge > spread_cost:
            net_no_edge = no_edge - spread_cost
            if net_no_edge >= self._min_edge:
                no_prob = 1 - estimated_prob
                ev_per_dollar = no_prob / market_no_price - 1.0
                adj_edge = net_no_edge * confidence
                candidates.append((
                    Direction.BUY_NO,
                    net_no_edge,
                    no_prob,
                    market_no_price,
                    no_token_id,
                    adj_edge,
                    ev_per_dollar,
                ))

        if not candidates:
            return None

        # Pick candidate with highest confidence-adjusted edge
        best = max(candidates, key=lambda c: c[5])
        direction, edge, prob, price, token_id, adj_edge, ev = best

        # Reject if expected value is negative (should not happen with positive edge, but safety check)
        if ev <= 0:
            log.debug("negative_ev_rejected", direction=direction.value, ev=ev)
            return None

        # Apply liquidity discount: shrink edge for low-volume markets
        if market_volume > 0:
            liquidity_factor = min(1.0, market_volume / 10000)  # Full credit at $10k+ volume
            adj_edge *= liquidity_factor

        return EdgeSignal(
            direction=direction,
            edge_size=edge,
            estimated_prob=prob,
            market_price=price,
            confidence=confidence,
            token_id=token_id,
            buy_price=price,
            confidence_adjusted_edge=adj_edge,
            expected_value=ev,
        )
