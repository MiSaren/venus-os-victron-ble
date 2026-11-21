"""Daily history of a solar charger, for the history page of gui-v2 / VRM.

A solar charger on VE.Direct publishes 31 days of history (/History/Daily/<n>/...,
n = 0 is today) from its own memory. The instant readout only carries the yield
of today and the current values, so the history is recorded here instead:

- days are calendar days in local time (the Venus time zone setting), so a day
  is complete at midnight; the charger's own day starts at sunrise, but no
  energy is harvested between midnight and sunrise
- Yield: the energy of the day, from the increases of the charger's daily
  yield counter (see manager.YieldCounter)
- MaxPower, Min/MaxBatteryVoltage, MaxBatteryCurrent: extremes of the values seen
- TimeInBulk/Absorption/Float (minutes): time spent in each charge state
- LastError1..4: errors of the day, the most recent first (0 = none)
- MaxPvVoltage is not in the readout and stays invalid

Days for which nothing was recorded (the GX was off) are published empty, so
that day n is always n days ago.
"""

import datetime
import time

DAYS = 31
MAX_GAP = 60.0  # seconds; longer pauses between samples (no data) are not counted
STATE_BULK = (3,)
STATE_ABSORPTION = (4, 7)  # absorption, equalize
STATE_FLOAT = (5, 6)  # float, storage

DAY_PATHS = {
    "Yield": "%.2fkWh",
    "MaxPower": "%.0fW",
    "MaxPvVoltage": "%.2fV",
    "MinBatteryVoltage": "%.2fV",
    "MaxBatteryVoltage": "%.2fV",
    "MaxBatteryCurrent": "%.1fA",
    "TimeInBulk": "%dmin",
    "TimeInAbsorption": "%dmin",
    "TimeInFloat": "%dmin",
    "LastError1": "%d",
    "LastError2": "%d",
    "LastError3": "%d",
    "LastError4": "%d",
}
DAYS_AVAILABLE = "/History/Overall/DaysAvailable"
PATHS = dict(
    [(DAYS_AVAILABLE, "%d")]
    + [("/History/Daily/%d/%s" % (n, k), fmt) for n in range(DAYS) for k, fmt in DAY_PATHS.items()]
)


def local_date(wall=None):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if wall is None else wall))


def _new_day(date):
    return {
        "date": date,
        "yield": 0.0,
        "maxPower": None,
        "minV": None,
        "maxV": None,
        "maxI": None,
        "bulk": 0.0,
        "absorption": 0.0,
        "float": 0.0,
        "errors": [],
    }


def _valid_day(d):
    return isinstance(d, dict) and isinstance(d.get("date"), str) and len(d["date"]) == 10


class SolarHistory:
    """days: newest first, day 0 = today; a day without data is {'date': ..., 'missing': True}."""

    def __init__(self, saved=None):
        self.days = [d for d in (saved or []) if _valid_day(d)][:DAYS]
        self._dirty = set()
        self._last_sample = None  # monotonic time of the last time accounting

    # ---- recording -------------------------------------------------------

    def today(self, date):
        """The record of `date`, rolling over to a new day (and inserting empty
        days for days without data) when the date moved on. When the clock went
        back (wrong time at boot until NTP), day 0 is used."""
        if self.days and date <= self.days[0]["date"]:
            return self.days[0]
        if self.days:
            try:
                gap = (
                    datetime.date.fromisoformat(date) - datetime.date.fromisoformat(self.days[0]["date"])
                ).days
            except ValueError:
                gap = 1
            last = datetime.date.fromisoformat(date)
            for i in range(min(gap - 1, DAYS), 0, -1):
                self.days.insert(
                    0, {"date": (last - datetime.timedelta(days=i)).isoformat(), "missing": True}
                )
        self.days.insert(0, _new_day(date))
        del self.days[DAYS:]
        self._dirty.update(range(DAYS))
        self._dirty.add(None)
        return self.days[0]

    def add_energy(self, kwh, date):
        if kwh is None or kwh <= 0:
            return
        day = self.today(date)
        day["yield"] = round(day["yield"] + kwh, 3)
        self._dirty.add(0)

    def sample(self, values, date):
        """Extremes and errors from one decoded advertisement."""
        day = self.today(date)
        before = (day["maxPower"], day["minV"], day["maxV"], day["maxI"], len(day["errors"]))
        power = values.get("/Yield/Power")
        voltage = values.get("/Dc/0/Voltage")
        current = values.get("/Dc/0/Current")
        error = values.get("/ErrorCode")
        if power is not None:
            day["maxPower"] = power if day["maxPower"] is None else max(day["maxPower"], power)
        if voltage is not None:
            day["minV"] = voltage if day["minV"] is None else min(day["minV"], voltage)
            day["maxV"] = voltage if day["maxV"] is None else max(day["maxV"], voltage)
        if current is not None:
            day["maxI"] = current if day["maxI"] is None else max(day["maxI"], current)
        if error and (not day["errors"] or day["errors"][0] != error):
            day["errors"].insert(0, error)
            del day["errors"][4:]
        if (day["maxPower"], day["minV"], day["maxV"], day["maxI"], len(day["errors"])) != before:
            self._dirty.add(0)

    def account_time(self, state, now, date):
        """Called regularly with the current charge state while data is coming in
        (now: monotonic seconds); pause() when there is no data."""
        day = self.today(date)
        last, self._last_sample = self._last_sample, now
        if last is None or state is None:
            return
        seconds = min(max(now - last, 0.0), MAX_GAP)
        key = (
            "bulk"
            if state in STATE_BULK
            else "absorption" if state in STATE_ABSORPTION else "float" if state in STATE_FLOAT else None
        )
        if key is None:
            return
        minutes = int(day[key] // 60)
        day[key] += seconds
        if int(day[key] // 60) != minutes:
            self._dirty.add(0)

    def pause(self):
        self._last_sample = None

    # ---- publishing ------------------------------------------------------

    def _day_values(self, n):
        day = self.days[n] if n < len(self.days) else None
        prefix = "/History/Daily/%d/" % n
        if day is None or day.get("missing"):
            return dict((prefix + k, None) for k in DAY_PATHS)
        errors = day["errors"] + [0] * (4 - len(day["errors"]))

        def rounded(v, digits):
            return None if v is None else round(v, digits)

        return {
            prefix + "Yield": round(day["yield"], 2),
            prefix + "MaxPower": day["maxPower"],
            prefix + "MaxPvVoltage": None,
            prefix + "MinBatteryVoltage": rounded(day["minV"], 2),
            prefix + "MaxBatteryVoltage": rounded(day["maxV"], 2),
            prefix + "MaxBatteryCurrent": rounded(day["maxI"], 1),
            prefix + "TimeInBulk": int(day["bulk"] // 60),
            prefix + "TimeInAbsorption": int(day["absorption"] // 60),
            prefix + "TimeInFloat": int(day["float"] // 60),
            prefix + "LastError1": errors[0],
            prefix + "LastError2": errors[1],
            prefix + "LastError3": errors[2],
            prefix + "LastError4": errors[3],
        }

    def values(self):
        """All history paths."""
        out = {DAYS_AVAILABLE: len(self.days)}
        for n in range(DAYS):
            out.update(self._day_values(n))
        self._dirty.clear()
        return out

    def changes(self):
        """The paths changed since the last values()/changes() call."""
        if not self._dirty:
            return {}
        out = {DAYS_AVAILABLE: len(self.days)}
        for n in sorted(i for i in self._dirty if i is not None):
            out.update(self._day_values(n))
        self._dirty.clear()
        return out

    @property
    def dirty(self):
        return bool(self._dirty)
