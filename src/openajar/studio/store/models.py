"""What the store keeps beside the shared types: the layout manifest as it is written to a blob."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from openajar.studio.contracts import Element


class Manifest(BaseModel):
    """One screen's layout manifest. `elements` is what `build_manifest` returns; the counts say
    how much of the page is in it, so a cap is never a silent loss (AX rule 6)."""

    model_config = ConfigDict(extra="forbid")

    elements: list[Element] = Field(default_factory=list)
    total: int = 0          # elements under <body>
    omitted: int = 0        # elements not in the manifest because the cap was reached
    cap: int = 0            # the cap that applied
    page_width: float = 0
    page_height: float = 0
    image_height: float | None = None   # CSS px kept in the image, when the page was taller than the cap

    @property
    def complete(self) -> bool:
        return self.omitted == 0


class CaptureInfo(BaseModel):
    """A line in a list of captures: enough to choose one, no shots."""

    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    created_at: str
    screens: int
    failed: int
    pinned: str | None = None
    baseline_for: list[str] = Field(default_factory=list)   # profiles this capture is the baseline of
