"""New York Stock Exchange trading days.

The exchange's full-day holidays, by its own observance rules: a holiday on a Sunday moves to the
Monday; on a Saturday to the Friday, except New Year's Day (the exchange stays open on the Friday
before). Early-close days (13:00) are trading days; on those the 15:45 run finds the market shut and
does nothing.
"""

from __future__ import annotations

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
    sunday_to_monday,
)
from pandas.tseries.offsets import CustomBusinessDay


class NYSEHolidays(AbstractHolidayCalendar):
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


SESSION = CustomBusinessDay(calendar=NYSEHolidays())


def is_session(day: pd.Timestamp) -> bool:
    day = pd.Timestamp(day).normalize()
    return day == (day - SESSION) + SESSION


def previous_session(day: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(day).normalize() - SESSION


def first_session_on_or_after(day: pd.Timestamp) -> pd.Timestamp:
    day = pd.Timestamp(day).normalize()
    return day if is_session(day) else day + SESSION


def add_sessions(day: pd.Timestamp, n: int) -> pd.Timestamp:
    return pd.Timestamp(day).normalize() + n * SESSION
