# One command for what CI checks. `make check` before every push.

.PHONY: check merge-check lint shell test panel emdash typecheck audit tools-doc errors-doc protocol-doc golden bench export-check

check: lint shell emdash typecheck test panel

# After merging a builder branch: the cheap checks that catch two branches meeting (the route
# matrices, the tool reference, the panel's typed maps), without the whole suite.
merge-check: lint shell emdash
	uv run pytest -q tests/unit/test_node_panel.py tests/unit/test_api_members.py tests/unit/test_registry.py tests/unit/test_config_example.py tests/unit/test_error_catalog.py
	@uv run agentos tools --markdown | diff -q - docs/TOOLS.md >/dev/null || { echo "docs/TOOLS.md is stale: make tools-doc"; exit 1; }
	@uv run agentos errors --markdown | diff -q - docs/ERRORS.md >/dev/null || { echo "docs/ERRORS.md is stale: make errors-doc"; exit 1; }
	@uv run python scripts/protocol_doc.py --check
	@if [ -f scripts/publish_cards.py ]; then uv run python scripts/publish_cards.py --check; fi
	@if [ -d panel/node_modules ]; then cd panel && npm run -s lint && npx tsc --noEmit -p . && npm test --silent; \
	else echo "panel: run 'npm ci' in panel/ first (skipped)"; fi

lint:
	uv run ruff check src tests scripts

# mypy on the modules pyproject.toml's [tool.mypy] lists (its TODO says what comes next). A pinned
# uvx, so mypy stays out of uv.lock.
MYPY_VERSION ?= 2.4.1
typecheck:
	uvx --from mypy==$(MYPY_VERSION) mypy --python-executable "$$(uv run python -c 'import sys; print(sys.executable)')"

# Known vulnerabilities in the locked dependencies (every extra and the dev group), from PyPI's
# advisory data through pip-audit. Exits 1 on any finding.
PIP_AUDIT_VERSION ?= 2.10.1
audit: SHELL := /bin/bash
audit: .SHELLFLAGS := -o pipefail -c
audit:
	uv export --frozen --all-extras --no-emit-project --format requirements-txt --quiet \
	  | uvx pip-audit==$(PIP_AUDIT_VERSION) --strict --disable-pip --no-deps --progress-spinner off -r /dev/stdin

shell:
	@for f in deploy/*.sh deploy/templates/*/*.sh scripts/*.sh; do bash -n "$$f" || exit 1; done

test:
	uv run pytest -q

panel:
	@if [ -d panel/node_modules ]; then cd panel && npm run -s lint && npm test --silent && npm run -s build; \
	else echo "panel: run 'npm ci' in panel/ first (skipped)"; fi

# Em dashes are fine in comments, never in text a person reads (strings, docstrings, UI).
emdash:
	@if grep -rn "—" src --include='*.py' --include='*.html' --include='*.js' --include='*.css' \
	    | grep -vE ':[0-9]+: *(#|//|/\*|\*)'; then \
	  echo "em dash in user-facing text (above): use a period, comma, colon or parentheses"; exit 1; fi
	@if grep -rn "—" panel/src --include='*.ts' --include='*.tsx' --include='*.css' --include='*.html' \
	    | grep -vE ':[0-9]+: *(\*|//|/\*)'; then \
	  echo "em dash in panel text (above)"; exit 1; fi
	@if grep -rn "—" web/src --include='*.ts' --include='*.tsx' --include='*.css' --include='*.html' \
	    | grep -vE ':[0-9]+: *(\*|//|/\*)'; then \
	  echo "em dash in web text (above)"; exit 1; fi

# The tool reference is generated from the ToolSpecs; CI fails when it is stale.
tools-doc:
	uv run agentos tools --markdown > docs/TOOLS.md

# The error catalog's reference and the panel's code-to-fix map, generated from the error
# classes and src/agentos/core/error_catalog.toml; CI fails when either is stale.
errors-doc:
	uv run agentos errors --markdown > docs/ERRORS.md
	uv run agentos errors --panel-json > panel/src/api/errors.json

# docs/PROTOCOL.md's message examples: record a real node and controller session into
# tests/fixtures/protocol/messages.json, then render it into the doc; CI fails when it is stale.
protocol-doc:
	uv run python scripts/protocol_doc.py --record

# Record the golden lane session again (tests/golden/dev-social: the dev web, never a real site);
# tests/unit/test_golden.py replays it against the engine abstraction.
golden:
	uv run python scripts/record_golden.py

# Lane benchmarks on the node, in slot 53 unless AGENTOS_TEST_SLOT says otherwise (docs/BENCH.md):
# appends the result to bench/results.jsonl, then flags a regression against the last one.
bench:
	@mkdir -p bench
	AGENTOS_TEST_SLOT=$${AGENTOS_TEST_SLOT:-53} scripts/node-test.sh bench --python scripts/bench_node.py \
	  --runs $${BENCH_RUNS:-5} --commit $$(git rev-parse --short HEAD) $$(git diff --quiet HEAD || echo --dirty) \
	  > bench/.last.txt
	@cat bench/.last.txt
	@sed -n 's/^BENCH //p' bench/.last.txt >> bench/results.jsonl
	uv run python scripts/bench_compare.py

# The public repository's tree (scripts/public-export.toml), exported from HEAD to a temporary
# directory and scanned for private names, hosts and em dashes. Commit first: it reads HEAD.
export-check:
	uv run python scripts/public_export.py --check
