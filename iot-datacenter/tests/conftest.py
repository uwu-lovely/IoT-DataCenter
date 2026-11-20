"""让测试可以直接 import backend 下的纯逻辑模块，并使用独立的临时数据库。"""

import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config  # noqa: E402

# 测试一律使用临时库，避免触碰 backend/data/iot.db 中的演示数据
config.DB_PATH = Path(tempfile.mkdtemp(prefix="iot_test_")) / "test.db"


@pytest.fixture(autouse=True)
def isolated_db():
    """每个用例前重建空库，保证告警/遥测等状态互不影响。"""
    import db

    db.init_db()
    db.reset_runtime_tables(seed_devices=False)
    yield
