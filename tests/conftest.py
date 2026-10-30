import sys
from pathlib import Path

import pytest

pytest_plugins = ["results_plugin"]

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory.store import connect, copy_db  # noqa: E402
from data.seed import seed  # noqa: E402


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("seed") / "template.db"
    seed(path, 42)
    return path


@pytest.fixture
def db_path(seeded_template, tmp_path):
    dst = tmp_path / "munim.db"
    copy_db(seeded_template, dst)
    return dst


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


from fastapi.testclient import TestClient  # noqa: E402

from core.engine.clock import SimClock  # noqa: E402
from packs.paytm_merchant.mocks import create_app  # noqa: E402
from packs.paytm_merchant.mocks.client import MockClient  # noqa: E402


@pytest.fixture
def clock():
    return SimClock()


@pytest.fixture
def app(db_path, clock):
    return create_app(db_path, clock, chaos_seed=1)


@pytest.fixture
def http(app):
    return TestClient(app)


@pytest.fixture
def mock(http):
    return MockClient(http)


def count(conn, table, where="1=1", args=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
