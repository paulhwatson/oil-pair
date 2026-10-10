"""Pure pairs-trading math: hedge ratio, spread, entry/exit decisions, leg sizing.

No network or IG dependencies here on purpose, so this module is fully
unit-testable with synthetic price data. Ported from the hedge-ratio/spread/
entry-exit logic in /Users/Paul/trading's pairs_actor.py + pairs_strategy.py,
simplified for a single-venue (IG-only) standalone app: no cross-venue price
multiplier, and the two decision functions are collapsed into one state
transition (`next_decision`) instead of separate enter/close methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd
from sklearn.linear_model import LinearRegression

MIN_FIT_OBSERVATIONS = 30


class PairSide(Enum):
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


class Action(Enum):
    NONE = "NONE"
    ENTER = "ENTER"
    EXIT_TAKE_PROFIT = "EXIT_TAKE_PROFIT"
    EXIT_STOP_LOSS = "EXIT_STOP_LOSS"


@dataclass(frozen=True)
class Model:
    i1_key: str
    i2_key: str
    hedge_ratio: float
    mean_spread: float
    std_spread: float
    entry_threshold: float
    limit_threshold: float
    stop_threshold: float
    fitted_at: pd.Timestamp
    n_observations: int


@dataclass(frozen=True)
class Decision:
    action: Action
    side: PairSide
    stopped_out: bool
    reason: str


def fit_model(
    mid_prices: dict[str, pd.Series],
    entry_stds: float,
    limit_stds: float,
    stop_stds: float,
    fitted_at: pd.Timestamp | None = None,
) -> Model:
    """Fits Y = hedge_ratio * X (OLS, no intercept) on the two aligned mid-price
    series. The higher-mean-price instrument becomes i1 (X, the regressor);
    the lower-priced one becomes i2 (Y) — matching the old repo's convention.
    """
    if len(mid_prices) != 2:
        raise ValueError(f"fit_model requires exactly 2 instruments, got {len(mid_prices)}")

    key_a, key_b = list(mid_prices.keys())
    combined = pd.DataFrame({key_a: mid_prices[key_a], key_b: mid_prices[key_b]}).dropna()
    if len(combined) < MIN_FIT_OBSERVATIONS:
        raise ValueError(
            f"insufficient aligned observations to fit model: {len(combined)} < {MIN_FIT_OBSERVATIONS}"
        )

    if combined[key_a].mean() >= combined[key_b].mean():
        i1_key, i2_key = key_a, key_b
    else:
        i1_key, i2_key = key_b, key_a

    x = combined[i1_key].to_numpy(dtype=float).reshape(-1, 1)
    y = combined[i2_key].to_numpy(dtype=float).reshape(-1, 1)

    regression = LinearRegression(fit_intercept=False)
    regression.fit(x, y)
    hedge_ratio = float(regression.coef_[0][0])

    spread = combined[i1_key] * hedge_ratio - combined[i2_key]
    std_spread = float(spread.std())

    return Model(
        i1_key=i1_key,
        i2_key=i2_key,
        hedge_ratio=hedge_ratio,
        mean_spread=float(spread.mean()),
        std_spread=std_spread,
        entry_threshold=std_spread * entry_stds,
        limit_threshold=std_spread * limit_stds,
        stop_threshold=std_spread * stop_stds,
        fitted_at=fitted_at if fitted_at is not None else pd.Timestamp.now(tz="UTC"),
        n_observations=len(combined),
    )


def compute_spread(mid_i1: float, mid_i2: float, hedge_ratio: float) -> float:
    return mid_i1 * hedge_ratio - mid_i2


def next_decision(
    spread: float,
    model: Model,
    current_side: PairSide,
    stopped_out: bool,
) -> Decision:
    """State transition for one pair position. `stopped_out` blocks re-entry
    until the spread first returns inside (-entry_threshold, entry_threshold).
    """
    entry, limit, stop = model.entry_threshold, model.limit_threshold, model.stop_threshold

    if current_side is PairSide.FLAT:
        if -entry < spread < entry:
            return Decision(Action.NONE, PairSide.FLAT, False, "spread inside entry band")
        if stopped_out:
            return Decision(Action.NONE, PairSide.FLAT, True, "outside entry band but still stopped out")
        if spread >= entry:
            return Decision(Action.ENTER, PairSide.SHORT, False, "spread above entry threshold")
        return Decision(Action.ENTER, PairSide.LONG, False, "spread below -entry threshold")

    if current_side is PairSide.SHORT:
        if spread < limit:
            return Decision(Action.EXIT_TAKE_PROFIT, PairSide.FLAT, stopped_out, "spread reverted past limit threshold")
        if spread > stop:
            return Decision(Action.EXIT_STOP_LOSS, PairSide.FLAT, True, "spread breached stop threshold")
        return Decision(Action.NONE, current_side, stopped_out, "holding short pair position")

    if spread > -limit:
        return Decision(Action.EXIT_TAKE_PROFIT, PairSide.FLAT, stopped_out, "spread reverted past limit threshold")
    if spread < -stop:
        return Decision(Action.EXIT_STOP_LOSS, PairSide.FLAT, True, "spread breached stop threshold")
    return Decision(Action.NONE, current_side, stopped_out, "holding long pair position")


def risk_sized_notional(
    max_loss: float,
    entry_stds: float,
    stop_stds: float,
    std_spread: float,
    price_i2: float,
    max_notional: float,
) -> float:
    """Notional per leg so that a trade running from entry to its stop
    loses about max_loss, capped at max_notional.

    Both legs carry the same notional N, so the i2 leg is N/price_i2 per
    point. The spread (hedge_ratio * i1 - i2) is in i2's points, and with
    equal notionals the pair's P&L moves by about (N/price_i2) per point of
    spread - exactly so when hedge_ratio equals price_i2/price_i1, and within
    a few percent at an entry level of a couple of std. A trade enters around
    entry_stds and is stopped at stop_stds, so the loss at the stop is about
    (N/price_i2) * (stop_stds - entry_stds) * std_spread. Solving for N:

        N = max_loss * price_i2 / ((stop_stds - entry_stds) * std_spread)

    "About", because dealing costs come on top, a price can jump past the
    stop between checks, and a refit mid-trade moves the stop itself.
    """
    distance = (stop_stds - entry_stds) * std_spread
    if distance <= 0:
        raise ValueError(
            f"risk sizing needs the stop beyond the entry with a positive std "
            f"(stop {stop_stds} - entry {entry_stds}, std {std_spread})"
        )
    if price_i2 <= 0:
        raise ValueError(f"price_i2 must be positive, got {price_i2}")
    return min(max_loss * price_i2 / distance, max_notional)


def compute_leg_size(
    notional_trade_size: float,
    mid_price: float,
    min_deal_size: float,
) -> float:
    """size = notional / mid_price, rounded to IG's standard 2-decimal size
    precision and floored at the market's minDealSize. Note: IG's
    `dealingRules.minStepDistance` is the minimum stop/limit *distance*, not
    a position-size increment - there is no separate size-step field, so
    minDealSize is used only as a floor, not a rounding step.
    """
    if mid_price <= 0:
        raise ValueError(f"mid_price must be positive, got {mid_price}")

    raw_size = notional_trade_size / mid_price
    size = round(raw_size, 2)
    return max(size, min_deal_size)
