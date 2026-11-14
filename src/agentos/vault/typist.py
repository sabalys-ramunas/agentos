"""The small surface the vault needs from a browser lane to type a secret.

The node adapts agentos.node.human / agentos.node.interaction to this protocol and
registers a factory `page -> Typist` in the runtime under the name "typist". The vault
never touches Playwright directly, and tests use a fake.

What the adapter must guarantee:

- `fields()` and `field_for_ref()` read structural metadata only, from every frame,
  without modifying the page (Camoufox isolated world; no injected attributes).
- `frame_origins` is the chain of serialized origins (`location.origin`, or "null" for
  an opaque origin) from the top document down to the document that holds the field.
  `["https://instagram.com"]` for a field in the main frame.
- `focus()` scrolls by wheel, moves to a random point inside the field and clicks it
  (human input), then re-reads and returns the field's fresh metadata.
- `type_secret()` clears the field, then types at human rhythm. Before every keystroke
  it checks that the focused element is still that field and that its frame chain
  origins are unchanged; otherwise it stops at once and raises TypistAborted. It never
  logs, traces, screenshots or returns the text.
- `press()` sends a real key press to the field (for example "Enter").
- `settle()` waits for navigation or network quiet after a submit, bounded by timeout.
- `open_login()` and `close_login()` are optional: an adapter without them has no commit gate to
  tell. `open_login(domain)` tells the lane's egress guard the vault is filling a login field of
  that domain, so the submit that follows is a login and not a possible publish; `close_login`
  takes that back when the typing failed. Only the vault calls them, with the domain of the stored
  entry, after the entry passed its checks: nothing a page or a model says reaches them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentos.core.models import Signal


@dataclass(frozen=True)
class FieldInfo:
    """One `<input>` (or other form control) as the adapter sees it, structurally."""

    locator: str                         # opaque handle the adapter understands
    frame_origins: tuple[str, ...]       # top document first, the field's own document last
    tag: str = "input"                   # lowercased tag name
    type: str = "text"                   # lowercased `type` attribute (default "text")
    autocomplete: str = ""               # raw `autocomplete` attribute
    name: str = ""                       # `name` attribute (developer-written, not localized)
    id: str = ""                         # `id` attribute
    inputmode: str = ""
    maxlength: int | None = None
    form: str | None = None              # form association key (form owner), None if formless
    visible: bool = True
    disabled: bool = False
    readonly: bool = False
    ref: str | None = None               # snapshot ref, when the field has one
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def autocomplete_tokens(self) -> tuple[str, ...]:
        return tuple(t for t in self.autocomplete.lower().split() if t)


class Typist(Protocol):
    async def page_url(self) -> str:
        """The top-level document URL as the browser reports it."""

    async def fields(self) -> list[FieldInfo]:
        """Every visible-or-not form control in every frame, in document order."""

    async def field_for_ref(self, ref: str) -> FieldInfo:
        """The field a snapshot ref points to. Raises RefNotFound."""

    async def focus(self, field: FieldInfo) -> FieldInfo:
        """Click into the field with human input; return its fresh metadata."""

    async def type_secret(self, field: FieldInfo, text: str) -> None:
        """Clear the focused field and type `text` at human rhythm (see module docs)."""

    async def press(self, field: FieldInfo, key: str) -> None: ...

    async def settle(self, timeout: float = 10.0) -> None: ...

    async def signal(self) -> Signal: ...

    # optional (see the module docs): `open_login(self, domain: str)`, `close_login(self, domain: str)`


TypistFactory = Callable[[Any], Typist]
