"""The diff that names the cause (S6): what changed between two captures of the same target, in
words an agent can act on, cause first.

- `diff_captures(store, before_id, after_id, tokens=, mask=, screens=)` -> `DiffResult`.
- `noise_mask(store, capture_a, capture_b)` -> `NoiseMask`: what differs between two captures
  of the same unchanged state, to pass back as `mask`.

Pillow is imported only when screenshots are compared.
"""

from openajar.studio.diff.diff import NoiseMask, ScreenMask, diff_captures, noise_mask

__all__ = ["NoiseMask", "ScreenMask", "diff_captures", "noise_mask"]
