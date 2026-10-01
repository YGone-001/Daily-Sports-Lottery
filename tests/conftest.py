"""
pytest 公共夹具
==============
把运行时数据目录隔离到临时目录，避免测试污染真实的 `data/` 内容。
同时保证仓库根目录在 `sys.path` 上，使 `import config` / `import utils.*` 可用。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """将 config.DATA_DIR 指向临时目录，并清空球队实力内存缓存。"""
    import config
    from utils import team_strength

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    team_strength.reload()
    yield tmp_path
    team_strength.reload()


@pytest.fixture()
def make_match():
    """构造一场合成比赛（默认：2030 年、未开赛、足球）。"""
    def _make(**overrides):
        match = {
            "id": "500w-2030-01-01-0",
            "sport": "football",
            "league": "英超",
            "date": "2030-01-01",
            "time": "20:00",
            "status": "upcoming",
            "home": "曼城",
            "away": "阿森纳",
            "odds": {"home_win": 1.9, "draw": 3.5, "away_win": 4.0},
        }
        match.update(overrides)
        return match

    return _make
