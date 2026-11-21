"""Personas: one lane's waking habits, drawn once from its own seed.

    persona = draw_persona("biz-a/instagram", seed)
    week = persona.for_day(day)        # the same persona, drifted for that day's week

Everything is drawn from generators keyed by the seed and a label (`rng(seed, "day", "2026-11-05")`),
so two lanes never share a clock, a plan never depends on the order things were drawn in, and the
same seed always gives the same plans.

A persona has a wake time and an awake span (the sleep trough is 6 to 9 hours), a weekend
shift (weekends start about 2 hours later, after Golder and Macy), three bumps of activity
(morning, lunch, evening), a preferred session count, a session length scale, a scroll speed,
a liker factor and a quiet-day rate. It drifts slowly: each week moves a few percent from the
week before, pulled back toward the drawn values. Keepalive reads its waking window; every field
is still drawn, in the same order, so a seed gives the same window it always did.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date
from functools import lru_cache

from pydantic import BaseModel, ConfigDict

EPOCH = date(2026, 1, 5)        # a Monday: week 0 of the drift


def rng(seed: str, *labels: object) -> random.Random:
    digest = hashlib.sha256("|".join([seed, *map(str, labels)]).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


class Persona(BaseModel):
    model_config = ConfigDict(frozen=True)

    surface: str
    seed: str
    redraws: int = 0
    wake_h: float               # weekday wake, local hours (6.0 to 9.5)
    awake_h: float              # 15 to 18 (a 6 to 9 hour sleep trough)
    weekend_shift_h: float      # 1.5 to 2.5
    morning_h: float            # bump centers, weekday local hours
    lunch_h: float
    evening_h: float
    weights: tuple[float, float, float]   # morning, lunch, evening
    sessions: int               # preferred sessions a day, 2 to 4
    length_scale: float         # 0.7 to 1.4 times the 9 minute median
    scroll_speed: float         # 0.8 to 1.25
    liker: float                # acts on 1 in 8 to 1 in 4 posts seen
    quiet_p: float              # 0.08 to 0.2

    def for_day(self, day: date) -> Persona:
        """The persona as it has drifted by `day`'s week."""
        week = (day - EPOCH).days // 7
        if week <= 0:
            return self
        d = _drift(self.seed, self.redraws, week)
        return self.model_copy(update={
            "wake_h": _clip(self.wake_h + d[0] / 60, 6.0, 9.5),
            "morning_h": _clip(self.morning_h + d[0] / 60, 6.5, 11.0),
            "lunch_h": _clip(self.lunch_h + d[1] / 60, 11.5, 14.5),
            "evening_h": _clip(self.evening_h + d[2] / 60, 18.0, 22.5),
            "length_scale": _clip(self.length_scale * (1 + d[3]), 0.7, 1.4),
            "liker": _clip(self.liker * (1 + d[4]), 0.125, 0.25),
        })

    def window(self, day: date) -> tuple[float, float]:
        """(first, last) local hour sessions may start on `day`."""
        shift = self.weekend_shift_h if day.weekday() >= 5 else 0.0
        wake = self.wake_h + shift
        return wake + 0.25, min(wake + self.awake_h - 0.5, 23.9)

    def intensity(self, day: date, hour: float) -> float:
        """The relative rate of session starts at local `hour`: a low floor while awake, plus the
        three bumps. Zero outside the waking window."""
        lo, hi = self.window(day)
        if not lo <= hour <= hi:
            return 0.0
        weekend = day.weekday() >= 5
        shift = self.weekend_shift_h if weekend else 0.0
        centers = (self.morning_h + shift, self.lunch_h + shift / 2, self.evening_h + shift / 4)
        widths = (1.0, 0.9, 1.6)
        rate = 0.12
        for c, w, k in zip(centers, widths, self.weights, strict=True):
            rate += k * math.exp(-((hour - c) ** 2) / (2 * w * w))
        return rate

    def summary(self) -> str:
        def hm(h: float) -> str:
            return f"{int(h):02d}:{round((h % 1) * 60) % 60:02d}"
        lo, hi = self.window(date(2026, 1, 5))
        return (f"awake about {hm(lo)} to {hm(hi)} on weekdays, {self.weekend_shift_h:.1f} hours later at weekends; "
                f"busiest around {hm(self.evening_h)}; about {self.sessions} sessions a day; "
                f"acts on 1 in {round(1 / self.liker)} posts seen")


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@lru_cache(maxsize=4096)
def _drift(seed: str, redraws: int, week: int) -> tuple[float, ...]:
    """A mean-reverting walk per field (AR(1), 0.85), one step a week: wake, lunch and evening in
    minutes (steps of about 4), length scale and liker as fractions (steps of 2 percent)."""
    steps = (4.0, 4.0, 5.0, 0.02, 0.02)
    state = (0.0, 0.0, 0.0, 0.0, 0.0)
    for w in range(1, max(0, week) + 1):
        r = rng(seed, "drift", redraws, w)
        state = tuple(0.85 * p + r.gauss(0, s) for p, s in zip(state, steps, strict=True))
    return state


def draw_persona(surface: str, seed: str, *, redraws: int = 0) -> Persona:
    r = rng(seed, "persona", redraws)
    wake = r.uniform(6.0, 9.5)
    weights = (r.uniform(0.4, 1.0), r.uniform(0.3, 0.9), r.uniform(0.7, 1.3))
    return Persona(
        surface=surface, seed=seed, redraws=redraws,
        wake_h=round(wake, 3),
        awake_h=round(24 - r.uniform(6.0, 9.0), 3),
        weekend_shift_h=round(r.uniform(1.5, 2.5), 3),
        morning_h=round(_clip(wake + r.uniform(0.5, 1.5), 6.5, 11.0), 3),
        lunch_h=round(r.uniform(12.0, 13.5), 3),
        evening_h=round(r.uniform(19.0, 21.5), 3),
        weights=tuple(round(w, 3) for w in weights),
        sessions=r.choice((2, 3, 3, 4)),
        length_scale=round(r.uniform(0.7, 1.4), 3),
        scroll_speed=round(r.uniform(0.8, 1.25), 3),
        liker=round(r.uniform(0.125, 0.25), 4),
        quiet_p=round(r.uniform(0.08, 0.2), 4),
    )

