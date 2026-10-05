"""What the trade emails say.

The emails are the main way anyone sees a trade, usually on a phone, so each
one carries what was dealt and at what prices, where the spread sits against
the entry, take-profit and stop levels and - on a close - the realised
result and how long the pair was held. Lines are "label: value" rather than
aligned columns, because a phone's mail app uses a proportional font.

Pure formatting: no IG calls, so it can be tested on its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from oil_pair.strategy_logic import Model, PairSide, compute_spread

UK = "Europe/London"


@dataclass(frozen=True)
class LegFill:
    """One leg of a pair trade, as much of it as is known."""

    name: str
    epic: str
    direction: str  # as held: "BUY" or "SELL"
    size: float | None = None  # £ per point
    open_level: float | None = None
    deal_id: str | None = None
    close_level: float | None = None
    profit: float | None = None  # realised, in the account currency
    profit_from_ig: bool = False  # False when worked out from the fill levels
    closed_earlier: bool = False  # closed on an earlier attempt, so no close details here


def opened(
    side: PairSide,
    reason: str,
    legs: list[LegFill],
    model: Model,
    mids: dict[str, float],
    names: dict[str, str],
    now: pd.Timestamp,
) -> tuple[str, str]:
    spread = compute_spread(mids[model.i1_key], mids[model.i2_key], model.hedge_ratio)
    size_i2 = next((leg.size for leg in legs if leg.epic == model.i2_key), None)
    take_profit, stop = _exit_levels(side, model)

    lines = [_sentence(reason), "", "Trade"]
    for leg in legs:
        fill = f"filled at {_price(leg.open_level)}" if leg.open_level is not None else "fill level not reported"
        lines.append(
            f"{leg.direction} {_size(leg.size)} {leg.name}: {fill} (mid {_price(mids[leg.epic])}), deal {leg.deal_id}"
        )
    lines.append(f"Opened {_when(now)}")
    lines += [
        "",
        "Spread",
        f"Now: {_signed(spread)} ({_signed(_stds(spread, model))} std)",
        _exit_line("Take profit", side, take_profit, spread, size_i2, model, closes_when_beyond=False),
        _exit_line("Stop loss", side, stop, spread, size_i2, model, closes_when_beyond=True),
        f"Entry band: ±{model.entry_threshold:.2f} ({_stds(model.entry_threshold, model):.2f} std)",
        "",
        *_model_lines(model, names),
    ]
    if size_i2 is not None:
        lines += [
            "",
            f"est. = the {names[model.i2_key]} leg's size × the spread's move, before dealing costs. "
            f"It assumes the legs move in line with the hedge ratio, so treat it as a guide.",
        ]

    subject = f"Opened {side.value} pair at {_signed(_stds(spread, model))} std"
    return subject, "\n".join(lines)


def closed(
    side: PairSide,
    outcome: str,
    reason: str,
    legs: list[LegFill],
    model: Model,
    mids: dict[str, float],
    names: dict[str, str],
    now: pd.Timestamp,
    opened_at: pd.Timestamp | None = None,
    entry_spread: float | None = None,
    entry_std_spread: float | None = None,
) -> tuple[str, str]:
    spread = compute_spread(mids[model.i1_key], mids[model.i2_key], model.hedge_ratio)
    take_profit, stop = _exit_levels(side, model)

    lines = [_sentence(reason), "", "Result"]
    for leg in legs:
        lines.append(_result_line(leg, mids))
    profits = [leg.profit for leg in legs]
    total = sum(profits) if all(p is not None for p in profits) else None
    if total is not None:
        source = "IG's realised P&L" if all(leg.profit_from_ig for leg in legs) else "from the fill levels"
        lines.append(f"Total: {_pounds(total)} ({source})")
    else:
        lines.append("Total: not available - check IG's history for this trade")
    if opened_at is not None:
        lines.append(f"Held {_duration(now - opened_at)}: opened {_when(opened_at)}, closed {_when(now)}")
    else:
        lines.append(f"Closed {_when(now)}")

    lines += ["", "Spread"]
    if entry_spread is not None:
        at_entry = f"At entry: {_signed(entry_spread)}"
        if entry_std_spread:
            at_entry += f" ({_signed(entry_spread / entry_std_spread)} std then)"
        lines.append(at_entry)
    lines.append(f"At exit: {_signed(spread)} ({_signed(_stds(spread, model))} std)")
    if entry_spread is not None:
        favourable = (entry_spread - spread) if side is PairSide.SHORT else (spread - entry_spread)
        direction = "in the pair's favour" if favourable >= 0 else "against the pair"
        lines.append(f"Moved {abs(favourable):.2f} {direction}")

    lines += [
        "",
        "Levels at exit",
        f"Take profit: {_signed(take_profit)} ({_signed(_stds(take_profit, model))} std)",
        f"Stop loss: {_signed(stop)} ({_signed(_stds(stop, model))} std)",
        f"Entry band: ±{model.entry_threshold:.2f} ({_stds(model.entry_threshold, model):.2f} std)",
        "",
        *_model_lines(model, names),
    ]

    subject = f"Closed {side.value} pair ({outcome})"
    if total is not None:
        subject += f": {_pounds(total)}"
    return subject, "\n".join(lines)


def leg_profit(direction: str, size: float | None, open_level: float | None, close_level: float | None) -> float | None:
    """A spread bet's P&L from its levels: £/point × points moved."""
    if size is None or open_level is None or close_level is None:
        return None
    sign = 1.0 if direction == "BUY" else -1.0
    return sign * size * (close_level - open_level)


def _exit_levels(side: PairSide, model: Model) -> tuple[float, float]:
    """(take-profit level, stop level) on the raw spread, as next_decision uses them."""
    if side is PairSide.SHORT:
        return model.limit_threshold, model.stop_threshold
    return -model.limit_threshold, -model.stop_threshold


def _exit_line(
    label: str, side: PairSide, level: float, spread: float, size_i2: float | None, model: Model,
    closes_when_beyond: bool,
) -> str:
    # SHORT profits as the spread falls, so take profit is below and the stop above; LONG mirrors it.
    above = (side is PairSide.SHORT) == closes_when_beyond
    when = "rises above" if above else "falls below"
    line = (
        f"{label}: when the spread {when} {_signed(level)} ({_signed(_stds(level, model))} std), "
        f"{abs(level - spread):.2f} away"
    )
    if size_i2 is not None:
        sign = 1.0 if side is PairSide.LONG else -1.0
        line += f", est. {_pounds(sign * size_i2 * (level - spread))}"
    return line


def _result_line(leg: LegFill, mids: dict[str, float]) -> str:
    if leg.closed_earlier:
        return f"{leg.name}: closed on an earlier attempt - see IG for its result"
    head = f"{leg.name}: {leg.direction} {_size(leg.size)}"
    opened_at = f"opened {_price(leg.open_level)}" if leg.open_level is not None else "opening level not recorded"
    closed_at = (
        f"closed {_price(leg.close_level)}" if leg.close_level is not None else f"closed near {_price(mids[leg.epic])}"
    )
    profit = _pounds(leg.profit) if leg.profit is not None else "P&L n/a"
    return f"{head}, {opened_at}, {closed_at}, {profit}"


def _model_lines(model: Model, names: dict[str, str]) -> list[str]:
    return [
        "Model",
        f"spread = {model.hedge_ratio:.6f} × {names[model.i1_key]} − {names[model.i2_key]}",
        f"std {model.std_spread:.2f}, fitted {_when(model.fitted_at)} on {model.n_observations} hourly points",
    ]


def _stds(value: float, model: Model) -> float:
    return value / model.std_spread if model.std_spread else math.nan


def _signed(value: float) -> str:
    return f"{value + 0.0:+.2f}"  # + 0.0 turns -0.0 into 0.0, so a zero level never prints as "-0.00"


def _pounds(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}£{abs(value):,.2f}"


def _price(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.10g}"


def _size(size: float | None) -> str:
    return "(size not recorded)" if size is None else f"£{size:.2f}/pt"


def _when(ts: pd.Timestamp) -> str:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(UK).strftime("%a %d %b %H:%M") + " UK"


def _duration(delta: pd.Timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _sentence(text: str) -> str:
    text = text.strip()
    return (text[:1].upper() + text[1:]).rstrip(".") + "." if text else ""
