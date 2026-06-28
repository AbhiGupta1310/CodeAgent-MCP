"""Tests for the AST indexer."""
import pytest
import textwrap
import aiosqlite

from code_server.indexer import init_db, index_file, index_repo, get_db_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Override get_db_path() to return a temp DB path for every test.

    This replaces the old DB_PATH monkeypatching strategy; now that DB path
    resolution is always done at call time via get_db_path(), we patch the
    function itself in both the indexer and tools modules.
    """
    db_file = str(tmp_path / "index.db")

    def _fixed_db_path(repo_path=None):
        return db_file

    monkeypatch.setattr("code_server.indexer.get_db_path", _fixed_db_path)
    # tools.py imports get_db_path — patch there too
    try:
        monkeypatch.setattr("code_server.tools.get_db_path", _fixed_db_path)
    except AttributeError:
        pass  # tools may not be imported yet

    return db_file


@pytest.fixture
def sample_py_file(tmp_path):
    """Create a sample Python file with a class, method, and function."""
    src = textwrap.dedent('''
        """Module docstring."""

        class MyClass:
            """A sample class."""

            def greet(self, name: str) -> str:
                """Return a greeting."""
                return f"Hello, {name}!"

        def standalone(x: int) -> int:
            """Double x."""
            return x * 2
    ''')
    f = tmp_path / "sample.py"
    f.write_text(src)
    return str(f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_db_creates_tables(tmp_db):
    await init_db()
    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in await cur.fetchall()}
    assert "symbols" in tables
    assert "imports" in tables


@pytest.mark.asyncio
async def test_index_file_finds_class_and_function(tmp_db, sample_py_file):
    await init_db()
    await index_file(sample_py_file)

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute("SELECT name, kind, parent FROM symbols ORDER BY start_line")
        rows = await cur.fetchall()

    names = {r[0] for r in rows}
    kinds = {r[1] for r in rows}

    assert "MyClass" in names
    assert "greet" in names
    assert "standalone" in names
    assert "class" in kinds
    assert "method" in kinds
    assert "function" in kinds


@pytest.mark.asyncio
async def test_method_has_parent_class(tmp_db, sample_py_file):
    await init_db()
    await index_file(sample_py_file)

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute(
            "SELECT parent FROM symbols WHERE name='greet'"
        )
        row = await cur.fetchone()

    assert row is not None
    assert row[0] == "MyClass"


@pytest.mark.asyncio
async def test_standalone_function_has_no_parent(tmp_db, sample_py_file):
    await init_db()
    await index_file(sample_py_file)

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute(
            "SELECT parent FROM symbols WHERE name='standalone'"
        )
        row = await cur.fetchone()

    assert row is not None
    assert row[0] is None


@pytest.mark.asyncio
async def test_index_repo_walks_multiple_files(tmp_db, tmp_path):
    """index_repo should process every .py file and skip excluded dirs."""
    (tmp_path / "a.py").write_text("def func_a(): pass\n")
    (tmp_path / "b.py").write_text("def func_b(): pass\n")

    # Create a file inside __pycache__ — must be skipped
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "c.py").write_text("def func_c(): pass\n")

    await init_db()
    await index_repo(str(tmp_path))

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute("SELECT name FROM symbols")
        names = {r[0] for r in await cur.fetchall()}

    assert "func_a" in names
    assert "func_b" in names
    assert "func_c" not in names   # skipped


@pytest.mark.asyncio
async def test_upsert_does_not_duplicate(tmp_db, sample_py_file):
    """Indexing the same file twice must not create duplicate rows."""
    await init_db()
    await index_file(sample_py_file)
    await index_file(sample_py_file)

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM symbols WHERE name='standalone'"
        )
        count = (await cur.fetchone())[0]

    assert count == 1
