"""The review browser (S1): a headless browser pool for measuring pages, not a lane.

    pool = ReviewPool()
    async with pool.page(Target(url="http://localhost:3000/"), Screen(width=360, theme="dark")) as page:
        ...                       # a playwright Page, loaded, themed, in its state, settled
        pool.report(page)         # engine, version, fonts, theme read back
    await pool.close()

studio_status() says what this install can do. See pool.py for what a call does.
"""

from openajar.studio.browser.errors import (
    BlankPage,
    EngineMissing,
    HttpErrorPage,
    PageNotLoaded,
    StateNotReached,
    StudioError,
    StudioTimeout,
    ThemeNotApplied,
)
from openajar.studio.browser.pool import DEFAULT_LIMITS, Limits, ReviewPool
from openajar.studio.browser.status import studio_status

__all__ = ["DEFAULT_LIMITS", "BlankPage", "EngineMissing", "HttpErrorPage", "Limits", "PageNotLoaded", "ReviewPool",
           "StateNotReached", "StudioError", "StudioTimeout", "ThemeNotApplied", "studio_status"]
