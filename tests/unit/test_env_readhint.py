"""terminal_exec's hint for a command that only read a file (openajar.env.readhint): the evals saw agents
read files with cat through terminal_exec, and file_read does that with line numbers."""

from __future__ import annotations

import pytest
from test_environments import node  # noqa: F401  (a fixture)

from openajar.env.readhint import file_read_hint, read_path


@pytest.mark.parametrize(("cmd", "path"), [
    ("cat package.json", "package.json"),
    ("cat server.js README.md", "server.js"),
    ("head -n 40 src/app.js", "src/app.js"),
    ("head -40 src/app.js", "src/app.js"),
    ("sed -n '1,40p' src/app.js", "src/app.js"),
    ("cat -n src/app.js | head -50", "src/app.js"),
    ("ls -la; cat package.json; ls .openajar 2>/dev/null; cat .openajar/environment.toml 2>/dev/null", "package.json"),
    ("ls -la && cat package.json 2>/dev/null | head -60 && ls .openajar 2>/dev/null", "package.json"),  # Haiku
    ('for f in src/* test/*; do echo "=== $f"; cat "$f"; done', ""),
])
def test_a_plain_read_names_the_file(cmd, path):
    assert read_path(cmd) == path


@pytest.mark.parametrize("cmd", [
    "npm test 2>&1 | tail -80",                       # a pipe's tail reads no file
    "git status --short; git log --oneline | head -5",
    "cat package.json | jq .scripts",                 # more than a read
    "cat > notes.md <<EOF\nhi\nEOF",                  # a write
    "echo hi > f.txt",
    "sed -i 's/a/b/' f.txt",
    "sed 's/a/b/' f.txt",
    "cat package.json; npm test",
    "cat $(ls *.json)",
    "ls -la",
    "pwd",
    "",
])
def test_anything_more_than_a_read_gets_no_hint(cmd):
    assert read_path(cmd) is None and file_read_hint(cmd) is None


def test_the_hint_names_the_path_from_the_commands_folder_and_is_short():
    hint = file_read_hint("cat src/price.js", "/work/calc")
    assert hint == 'file_read(path="/work/calc/src/price.js") reads a file with line numbers, as file_edit expects.'
    assert len(hint) // 4 < 30 and "—" not in hint
    assert file_read_hint("cat /etc/hosts", "/work/calc").startswith('file_read(path="/etc/hosts")')
    assert file_read_hint('for f in a b; do cat "$f"; done').startswith("file_read(path=...)")


async def test_lane_exec_adds_the_hint_only_to_a_plain_read(node):  # noqa: F811
    from test_environments import opened

    env = await opened(node)
    read = await node["call"]("terminal_exec", on=env["on"], cmd="cat package.json", cwd="/work/shop")
    assert read["hint"].startswith('file_read(path="/work/shop/package.json")')
    plain = await node["call"]("terminal_exec", on=env["on"], cmd="npm test 2>&1 | tail -5", cwd="/work/shop")
    assert "hint" not in plain
