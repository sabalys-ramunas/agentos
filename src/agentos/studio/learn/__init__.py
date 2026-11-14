"""Learn a brand from a live site (studio part S8): the same reading that checks a page against
tokens, run in reverse to draft tokens for the owner to approve.

    draft = await learn_brand(browser, "http://localhost:3000/", pages=["/pricing"])
    draft.summary()      # counts, palette, coverage, what is uncertain
    draft.brand          # the profile's Brand form: DTCG tokens, fonts, components, theme

The tool `profile_learn_brand` (tool.py) proposes the draft with profile_propose. Nothing here
imports playwright: pages come from the review browser.
"""

from .draft import BrandDraft, CategorySummary, Reads, build_draft
from .learn import learn_brand, site_pages

__all__ = ["BrandDraft", "CategorySummary", "Reads", "build_draft", "learn_brand", "site_pages"]
