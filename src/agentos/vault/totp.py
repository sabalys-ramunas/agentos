"""TOTP (RFC 6238): seeds and otpauth:// URIs in, codes out. Codes are `Secret`s.

The node computes codes locally; neither the seed nor a code ever reaches the model.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlsplit

from agentos.policy.scrub import MIN_SECRET
from agentos.vault.errors import EntryInvalid
from agentos.vault.secret import Secret

_ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
_B32_RE = re.compile(r"^[A-Z2-7]+=*$")


@dataclass(frozen=True, repr=False)
class TotpSpec:
    seed: Secret            # base32, uppercase, no padding
    digits: int = 6
    period: int = 30
    algorithm: str = "SHA1"

    def __repr__(self) -> str:
        return f"TotpSpec(digits={self.digits}, period={self.period}, algorithm={self.algorithm!r})"


def _clean_seed(raw: str) -> str:
    seed = re.sub(r"[\s-]", "", raw).upper().rstrip("=")
    if not seed or not _B32_RE.match(seed) or len(seed) > 1024:
        raise EntryInvalid("TOTP seed is not valid base32")
    try:
        key = base64.b32decode(seed + "=" * (-len(seed) % 8))
    except (binascii.Error, ValueError):
        raise EntryInvalid("TOTP seed is not valid base32") from None
    if len(key) < 10:
        raise EntryInvalid("TOTP seed is too short (under 80 bits)")
    return seed


def parse_totp(value: str) -> TotpSpec:
    """A bare base32 seed or an `otpauth://totp/...` URI. Raises EntryInvalid without
    echoing the value."""
    if not isinstance(value, str) or not value.strip():
        raise EntryInvalid("TOTP seed is empty")
    value = value.strip()
    if not value.lower().startswith("otpauth://"):
        return TotpSpec(seed=Secret(_clean_seed(value)))
    parts = urlsplit(value)
    if parts.netloc.lower() != "totp":
        raise EntryInvalid("only otpauth://totp URIs are supported (not hotp)")
    q = {k.lower(): v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    seed = _clean_seed(q.get("secret", ""))
    try:
        digits = int(q.get("digits", "6"))
        period = int(q.get("period", "30"))
    except ValueError:
        raise EntryInvalid("TOTP digits and period must be numbers") from None
    algorithm = q.get("algorithm", "SHA1").upper()
    if digits not in (6, 7, 8):
        raise EntryInvalid("TOTP digits must be 6, 7 or 8")
    if not 10 <= period <= 300:
        raise EntryInvalid("TOTP period must be 10 to 300 seconds")
    if algorithm not in _ALGORITHMS:
        raise EntryInvalid("TOTP algorithm must be SHA1, SHA256 or SHA512")
    return TotpSpec(seed=Secret(seed), digits=digits, period=period, algorithm=algorithm)


def canonical_uri(value: str, *, label: str = "agentos", issuer: str | None = None) -> str:
    spec = parse_totp(value)
    q = f"secret={spec.seed.reveal()}&digits={spec.digits}&period={spec.period}&algorithm={spec.algorithm}"
    if issuer:
        q += f"&issuer={quote(issuer, safe='')}"
    return f"otpauth://totp/{quote(label, safe='')}?{q}"


def code_at(spec: TotpSpec, at: float) -> Secret:
    import pyotp

    totp = pyotp.TOTP(
        spec.seed.reveal(), digits=spec.digits, interval=spec.period, digest=_ALGORITHMS[spec.algorithm]
    )
    return Secret(totp.at(int(at)))


def seconds_left(spec: TotpSpec, at: float) -> float:
    return spec.period - (at % spec.period)


def seed_forms(stored: str) -> list[str]:
    """A TOTP seed as a setup page shows it: as stored (a bare seed or an otpauth URI), bare, with
    its "=" padding, and in groups of four split by spaces or dashes ("JBSW Y3DP ..."). Letter case
    is the matcher's. An unparsable seed is matched as stored."""
    out = [stored.strip()]
    try:
        seed = parse_totp(stored).seed.reveal()
    except Exception:  # noqa: BLE001 - a damaged seed is still matched as it was stored
        return [v for v in out if len(v) >= MIN_SECRET]
    groups = [seed[i:i + 4] for i in range(0, len(seed), 4)]
    out += [seed, seed + "=" * (-len(seed) % 8), " ".join(groups), "-".join(groups)]
    return [v for v in dict.fromkeys(out) if len(v) >= MIN_SECRET]
