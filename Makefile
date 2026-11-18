# One command for what CI checks. `make check` before every push.

.PHONY: check merge-check lint layers complexity deadcode shell test emdash typecheck audit tools-doc errors-doc protocol-doc golden bench eval export-check

check: lint deadcode shell emdash typecheck test

# After merging a builder branch: the cheap checks that catch two branches meeting (the route
# matrices, the tool reference, the error catalog), without the whole suite.
merge-check: lint deadcode shell emdash
	uv run pytest -q tests/unit/test_node_panel.py tests/unit/test_api_members.py tests/unit/test_registry.py tests/unit/test_config_example.py tests/unit/test_error_catalog.py
	@uv run agentos tools --markdown | diff -q - docs/TOOLS.md >/dev/null || { echo "docs/TOOLS.md is stale: make tools-doc"; exit 1; }
	@uv run agentos errors --markdown | diff -q - docs/ERRORS.md >/dev/null || { echo "docs/ERRORS.md is stale: make errors-doc"; exit 1; }
	@uv run python scripts/protocol_doc.py --check

lint: layers complexity
	uv run ruff check src tests scripts

# The layer order (scripts/layers.toml, docs/ARCHITECTURE.md "Layers"): no import goes up a layer, no
# packages on one layer import each other in a circle, and the exceptions list only shrinks.
layers:
	@uv run python scripts/layers.py

# Size limits (docs/DEVELOPING.md): no new function of complexity 25 or more, no new file over 1,000
# lines, and the listed ones only shrink (scripts/complexity-allowlist.txt, file-length-allowlist.txt).
complexity:
	@uv run python scripts/complexity.py

# Dead code: what vulture is sure nothing uses. scripts/vulture_whitelist.py names the few uses it
# cannot see (framework hooks, names reached by string). A pinned uvx, so vulture stays out of uv.lock.
VULTURE_VERSION ?= 2.16
deadcode:
	uvx --python 3.12 vulture==$(VULTURE_VERSION) src/agentos scripts/vulture_whitelist.py --min-confidence 80

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

# Em dashes are fine in comments, never in text a person reads (strings, docstrings, UI).
emdash:
	@if grep -rn "—" src --include='*.py' --include='*.html' --include='*.js' --include='*.css' \
	    | grep -vE ':[0-9]+: *(#|//|/\*|\*)'; then \
	  echo "em dash in user-facing text (above): use a period, comma, colon or parentheses"; exit 1; fi
	@if grep -rn "—" web/src --include='*.ts' --include='*.tsx' --include='*.css' --include='*.html' \
	    | grep -vE ':[0-9]+: *(\*|//|/\*)'; then \
	  echo "em dash in web text (above)"; exit 1; fi

# The tool reference is generated from the ToolSpecs; CI fails when it is stale.
tools-doc:
	uv run agentos tools --markdown > docs/TOOLS.md

# The error catalog's reference, generated from the error classes and
# src/agentos/core/error_catalog.toml; CI fails when it is stale.
errors-doc:
	uv run agentos errors --markdown > docs/ERRORS.md

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

# The environments eval (docs/EVALS.md): the free suite (the perfect and careless scripts on a docker
# node of its own), appended to bench/evals.jsonl and compared with the run before. EVAL_ARGS adds
# flags: EVAL_ARGS="--agent claude --model haiku --task t01" for a paid run (--max-usd guards it).
eval:
	uv run python tests/env_eval/run.py $(EVAL_ARGS)

# The public repository's tree (scripts/public-export.toml), exported from HEAD to a temporary
# directory and scanned for private names, hosts and em dashes. Commit first: it reads HEAD.
export-check:
	uv run python scripts/public_export.py --check
