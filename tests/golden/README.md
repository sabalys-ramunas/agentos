# Golden traces

Each directory is one lane session, recorded once and replayed by `tests/unit/test_golden.py`
(agentos/dev/golden.py has the details):

- `session.json`: the MCP tool calls an agent made and what each returned.
- `engine.json`: every call the tool layer made on the engine (the Interaction in
  agentos/node/interfaces.py) and its result. The replay feeds these to the tool layer in place
  of the engine, so a change in how tools turn engine results into what an agent reads (outlines,
  refs, deltas, signals and holds, page results, errors) fails the test.
- `page.har`: HAR 1.2 of the pages the tab showed. The dev web has no network, so each entry is a
  page, with its outline as the response body.

Recordings come from the dev web (agentos/dev/web.py, a made-up site on `social.example`), never a
real site, so the repo holds no one else's content. The test checks every URL is on a `.example`
host.

Record again with `make golden` (scripts/record_golden.py) when a change is meant to alter what
agents read, and review the diff: it is exactly what an agent now sees differently.
