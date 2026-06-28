"""Tests for JavaScript, TypeScript, JSX, and TSX indexing and tools."""
import pytest
import textwrap
import aiosqlite

from code_server.indexer import init_db, index_file, index_repo, get_db_path
from code_server import tools as T


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Override get_db_path() to return a temp DB path for every test."""
    db_file = str(tmp_path / "index.db")

    def _fixed_db_path(repo_path=None):
        return db_file

    monkeypatch.setattr("code_server.indexer.get_db_path", _fixed_db_path)
    monkeypatch.setattr("code_server.tools.get_db_path", _fixed_db_path)
    return db_file


@pytest.fixture
def sample_js_file(tmp_path):
    """Create a sample JavaScript file with class, method, function, and JSDoc."""
    src = textwrap.dedent('''
        import defaultExport from "module-a";
        import { export1, export2 as alias2 } from "module-b";

        /**
         * A helper class.
         */
        class JSClass {
            /**
             * Return a greet string.
             */
            greet(name) {
                return `Hello, ${name}`;
            }
        }

        /**
         * Standalone test function.
         */
        function standalone(x) {
            return x * 2;
        }

        /**
         * Arrow expression function.
         */
        const arrowFunc = (y) => {
            return y + 1;
        };
    ''')
    f = tmp_path / "sample.js"
    f.write_text(src)
    return str(f)


@pytest.fixture
def sample_ts_file(tmp_path):
    """Create a sample TypeScript file with types and classes."""
    src = textwrap.dedent('''
        /**
         * TS Class interface helper.
         */
        class TSClass<T> {
            val: T;
            constructor(val: T) {
                this.val = val;
            }

            getVal(): T {
                return this.val;
            }
        }

        function tsFunc(x: number): string {
            return "done";
        }
    ''')
    f = tmp_path / "sample.ts"
    f.write_text(src)
    return str(f)


@pytest.mark.asyncio
async def test_js_indexing_finds_all_symbols(tmp_db, sample_js_file):
    await init_db()
    await index_file(sample_js_file)

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute("SELECT name, kind, parent, docstring FROM symbols ORDER BY start_line")
        rows = await cur.fetchall()

    names = {r[0] for r in rows}
    kinds = {r[1] for r in rows}
    docs = {r[3] for r in rows if r[3]}

    assert "JSClass" in names
    assert "greet" in names
    assert "standalone" in names
    assert "arrowFunc" in names

    assert "class" in kinds
    assert "method" in kinds
    assert "function" in kinds

    assert "A helper class." in docs
    assert "Return a greet string." in docs
    assert "Arrow expression function." in docs


@pytest.mark.asyncio
async def test_ts_indexing_handles_types_properly(tmp_db, sample_ts_file):
    await init_db()
    await index_file(sample_ts_file)

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute("SELECT name, kind, parent FROM symbols ORDER BY start_line")
        rows = await cur.fetchall()

    names = {r[0] for r in rows}
    assert "TSClass" in names
    assert "getVal" in names
    assert "tsFunc" in names

    # Method should link to class
    greet_row = next(r for r in rows if r[0] == "getVal")
    assert greet_row[2] == "TSClass"


@pytest.mark.asyncio
async def test_js_import_scanning(tmp_db, sample_js_file):
    await init_db()
    await index_file(sample_js_file)

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute("SELECT module, alias, symbol FROM imports ORDER BY id")
        rows = await cur.fetchall()

    assert len(rows) == 3
    # import defaultExport from "module-a";
    assert rows[0] == ("module-a", None, "defaultExport")
    # import { export1, export2 as alias2 } from "module-b";
    assert rows[1] == ("module-b", None, "export1")
    assert rows[2] == ("module-b", "alias2", "export2")


@pytest.mark.asyncio
async def test_index_repo_with_mixed_languages(tmp_db, tmp_path):
    # Setup mixed repo
    (tmp_path / "a.py").write_text("def py_func(): pass\n")
    (tmp_path / "b.js").write_text("function jsFunc() {}\n")
    (tmp_path / "c.tsx").write_text("const Component = () => <div>Hello</div>;\n")

    await init_db()
    await index_repo(str(tmp_path))

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute("SELECT name, kind FROM symbols ORDER BY name")
        rows = await cur.fetchall()

    names = {r[0] for r in rows}
    assert "py_func" in names
    assert "jsFunc" in names
    assert "Component" in names


@pytest.mark.asyncio
async def test_js_list_imports_regex(sample_js_file):
    imps = await T.list_imports(sample_js_file)
    statements = [i["statement"] for i in imps]
    assert any('import defaultExport from "module-a"' in s for s in statements)
    assert any('import { export1, export2 as alias2 } from "module-b"' in s for s in statements)
