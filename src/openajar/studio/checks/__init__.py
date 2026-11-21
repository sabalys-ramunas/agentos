"""Measured UI checks (S3): read layout faults, contrast, targets, names, page health and theme
leaks from a rendered page. See docs/design/studio.md.

- `run_checks(page, ctx)`: one screen, a CheckOutput with findings and coverage for every check.
- `merge(outputs_by_screen)`: one CheckRun, one finding per cause, `seen_at` listing the screens.
- `delta(before, after)`: fixed, new, still and worse by finding id; `unchecked` for the rest.
"""

from openajar.studio.checks.merge import delta, merge, unchecked
from openajar.studio.checks.registry import CHECKS, IDS
from openajar.studio.checks.run import run_checks, theme_snapshot

__all__ = ["CHECKS", "IDS", "delta", "merge", "run_checks", "theme_snapshot", "unchecked"]
