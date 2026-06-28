"""Tests for code_server/tools.py."""
from __future__ import annotations

import textwrap
import pytest
import aiosqlite

from code_server import tools as T
from code_server.indexer import init_db, index_file


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    """Override get_db_path() to always return a temp DB for every test."""
    db_file = str(tmp_path / "index.db")

    def _fixed(repo_path=None):
        return db_file

    monkeypatch.setattr("code_server.indexer.get_db_path", _fixed)
    monkeypatch.setattr("code_server.tools.get_db_path", _fixed)
    return db_file


@pytest.fixture
def sample_repo(tmp_path):
    """A tiny repo with two Python files."""
    a = tmp_path / "alpha.py"
    a.write_text(textwrap.dedent("""
        import os
        from pathlib import Path

        class Alpha:
            \"\"\"Alpha class.\"\"\"

            def run(self, x: int) -> int:
                \"\"\"Run alpha.\"\"\"
                return x * 2

        def helper():
            pass
    """))

    b = tmp_path / "beta.py"
    b.write_text(textwrap.dedent("""
        from alpha import Alpha

        def main():
            a = Alpha()
            result = a.run(42)
            return result
    """))

    return tmp_path


@pytest.fixture
async def indexed_repo(sample_repo):
    """Init DB and index the sample repo."""
    await init_db()
    for f in sample_repo.glob("*.py"):
        await index_file(str(f))
    return sample_repo


# ---------------------------------------------------------------------------
# find_function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_function_returns_matches(indexed_repo):
    results = await T.find_function("run")
    names = [r["name"] for r in results]
    assert "run" in names


@pytest.mark.asyncio
async def test_find_function_partial_match(indexed_repo):
    results = await T.find_function("help")
    names = [r["name"] for r in results]
    assert "helper" in names


@pytest.mark.asyncio
async def test_find_function_no_match(indexed_repo):
    results = await T.find_function("zzz_nonexistent_zzz")
    assert results == []


@pytest.mark.asyncio
async def test_find_function_returns_kind_and_parent(indexed_repo):
    results = await T.find_function("run")
    hit = next(r for r in results if r["name"] == "run")
    assert hit["kind"] == "method"
    assert hit["parent"] == "Alpha"


# ---------------------------------------------------------------------------
# get_callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_callers_finds_call_site(sample_repo):
    callers = await T.get_callers("run", str(sample_repo))
    snippets = [c["snippet"] for c in callers]
    # beta.py calls a.run(42)
    assert any("run" in s for s in snippets)


@pytest.mark.asyncio
async def test_get_callers_caps_at_30(tmp_path):
    """Create 40 files each calling foo() once — result must be ≤ 30."""
    for i in range(40):
        (tmp_path / f"f{i}.py").write_text(f"foo()\n")
    callers = await T.get_callers("foo", str(tmp_path))
    assert len(callers) <= 30


@pytest.mark.asyncio
async def test_get_callers_returns_line_and_snippet(sample_repo):
    callers = await T.get_callers("Alpha", str(sample_repo))
    for c in callers:
        assert "file" in c
        assert "line" in c
        assert "snippet" in c
        assert isinstance(c["line"], int)


# ---------------------------------------------------------------------------
# read_file_slice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_slice_basic(sample_repo):
    target = str(sample_repo / "alpha.py")
    result = await T.read_file_slice(target, 1, 3)
    lines = result.splitlines()
    assert len(lines) == 3
    # Each line starts with a number
    assert lines[0].strip().startswith("1:")


@pytest.mark.asyncio
async def test_read_file_slice_line_numbers_prepended(sample_repo):
    target = str(sample_repo / "alpha.py")
    result = await T.read_file_slice(target, 5, 5)
    # Single line — number should be 5
    assert result.strip().startswith("5:")


@pytest.mark.asyncio
async def test_read_file_slice_bad_file():
    result = await T.read_file_slice("/nonexistent/path/file.py", 1, 5)
    assert result.startswith("ERROR:")


@pytest.mark.asyncio
async def test_read_file_slice_start_beyond_eof(sample_repo):
    target = str(sample_repo / "alpha.py")
    result = await T.read_file_slice(target, 99999, 100000)
    assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# list_imports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_imports_finds_all(sample_repo):
    target = str(sample_repo / "alpha.py")
    imps = await T.list_imports(target)
    statements = [i["statement"] for i in imps]
    assert any("import os" in s for s in statements)
    assert any("from pathlib import Path" in s for s in statements)


@pytest.mark.asyncio
async def test_list_imports_line_numbers(sample_repo):
    target = str(sample_repo / "alpha.py")
    imps = await T.list_imports(target)
    for i in imps:
        assert isinstance(i["line"], int)
        assert i["line"] > 0


@pytest.mark.asyncio
async def test_list_imports_bad_file():
    result = await T.list_imports("/no/such/file.py")
    assert len(result) == 1
    assert "ERROR:" in result[0]["statement"]


@pytest.mark.asyncio
async def test_list_imports_from_style(sample_repo):
    target = str(sample_repo / "beta.py")
    imps = await T.list_imports(target)
    statements = [i["statement"] for i in imps]
    assert any("from alpha import Alpha" in s for s in statements)
