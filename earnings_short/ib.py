"""What the same shorts would make at Interactive Brokers: an estimate, shown beside IG's figures on the
dashboard, for deciding whether to move the strategy there.

On IB a short would be shares sold with a market-on-close order, and its hedge SPY bought with the sale's
proceeds (SPY at a tenth of the US 500). So, against IG:
  - no bid/offer: the trade runs mid to mid - IG's mids at the same moments stand in for the closing
    auction's price;
  - a commission on each of the four orders;
  - no interest either way, as the proceeds pay for the SPY; but a fee to borrow the shares;
  - the hedge earns the interest rate on its exposure relative to IG's: IG's quarterly US 500 price
    carries interest less dividends, while SPY bought with the proceeds costs no interest and pays its
    dividends.
Dividends on the short itself are paid either way. Rates are in config/earnings_short.toml ([ib]).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class IBCosts:
    commission_per_share: float = 0.005   # $ a share, all-in: IBKR Pro's tiered $0.0035 plus exchange and other fees
    min_commission: float = 0.40          # £ an order: the $0.35 minimum plus fees
    borrow_rate: float = 0.003            # a year on the short's exposure: IB's fee on easy-to-borrow shares
    interest_rate: float = 0.039          # a year: about SOFR, what IG's quarterly price charges and SPY doesn't


def commission(costs: IBCosts, exposure: float, price_usd: float) -> float:
    """£ for one order of `exposure` £ of a share at `price_usd` (the share count cancels the exchange rate)."""
    return max(costs.min_commission, costs.commission_per_share * exposure / price_usd)


def estimate(costs: IBCosts, *, size: float, entry_mid: float, exit_mid: float, opened: pd.Timestamp,
             until: pd.Timestamp, hedge_size: float | None = None, hedge_entry_mid: float | None = None,
             hedge_exit_mid: float | None = None, dividends: float = 0.0) -> float:
    """£ the short and its hedge would make on IB, in and out. Prices are IG's: the share in cents, the
    US 500 in index points; sizes IG's £ a point."""
    years = (until - opened).total_seconds() / (365 * 86400)
    exposure = size * entry_mid
    pnl = size * (entry_mid - exit_mid) - costs.borrow_rate * exposure * years
    pnl -= 2 * commission(costs, exposure, entry_mid / 100)
    if hedge_size and hedge_entry_mid and hedge_exit_mid:
        hedge_exposure = hedge_size * hedge_entry_mid
        pnl += hedge_size * (hedge_exit_mid - hedge_entry_mid) + costs.interest_rate * hedge_exposure * years
        pnl -= 2 * commission(costs, hedge_exposure, hedge_entry_mid / 10)
    return pnl + dividends
