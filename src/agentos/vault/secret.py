"""A string wrapper that never shows its value by accident."""

from __future__ import annotations

import hmac


class Secret:
    """Holds a secret string. repr, str, format and pickling never reveal it.

    Call `reveal()` at the single place the value is handed to its sink (the typist,
    an auth header, an IMAP login). Equality is constant time.
    """

    __slots__ = ("_value",)
    _value: str

    def __init__(self, value: str):
        if not isinstance(value, str):
            raise TypeError("Secret holds a str")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        return self._value

    def __setattr__(self, name, value):  # immutable
        raise AttributeError("Secret is immutable")

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "***"

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self._value.encode(), other._value.encode())

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self):
        raise TypeError("a Secret cannot be pickled")

    def __copy__(self) -> Secret:
        return self

    def __deepcopy__(self, memo) -> Secret:
        return self
