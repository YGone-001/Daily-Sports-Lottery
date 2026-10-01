"""市场覆盖判定的单元测试（准入的唯一权威定义）。"""
from __future__ import annotations

import pytest

from utils.market_coverage import (
    basketball_has_moneyline,
    basketball_has_total,
    football_has_one_x_two,
    football_has_total,
    has_usable_market_odds,
    is_finite_number,
    is_valid_price,
)

FOOTBALL_1X2 = {"home_win": 1.82, "draw": 3.55, "away_win": 4.30}
BASKETBALL_ML = {"home_win": 1.80, "away_win": 2.00}


def _match(sport: str, odds) -> dict:
    return {"sport": sport, "odds": odds}


# ---------------------------------------------------------------------------
# 足球
# ---------------------------------------------------------------------------

def test_football_complete_one_x_two_accepted(isolated_data_dir):
    assert football_has_one_x_two(FOOTBALL_1X2) is True
    assert has_usable_market_odds(_match("football", FOOTBALL_1X2)) is True


def test_football_incomplete_one_x_two_rejected(isolated_data_dir):
    incomplete = {"home_win": 1.82, "away_win": 4.30}
    assert football_has_one_x_two(incomplete) is False
    assert has_usable_market_odds(_match("football", incomplete)) is False


def test_football_total_market_accepted(isolated_data_dir):
    totals = {"total_line": 2.5, "over": 1.90, "under": 1.90}
    assert football_has_total(totals) is True
    assert has_usable_market_odds(_match("football", totals)) is True


def test_football_total_alt_naming_accepted(isolated_data_dir):
    alt = {"over_2_5": 1.90, "under_2_5": 1.95}
    assert football_has_total(alt) is True
    assert has_usable_market_odds(_match("football", alt)) is True


def test_football_incomplete_total_rejected(isolated_data_dir):
    assert football_has_total({"total_line": 2.5}) is False
    assert football_has_total({"over": 1.90}) is False
    assert football_has_total({"over_2_5": 1.90}) is False


# ---------------------------------------------------------------------------
# 篮球
# ---------------------------------------------------------------------------

def test_basketball_moneyline_accepted(isolated_data_dir):
    assert basketball_has_moneyline(BASKETBALL_ML) is True
    assert has_usable_market_odds(_match("basketball", BASKETBALL_ML)) is True


def test_basketball_incomplete_moneyline_rejected(isolated_data_dir):
    assert basketball_has_moneyline({"home_win": 1.80}) is False
    assert has_usable_market_odds(_match("basketball", {"home_win": 1.80})) is False


def test_basketball_total_market_accepted(isolated_data_dir):
    totals = {"total_line": 220.5, "over": 1.90, "under": 1.90}
    assert basketball_has_total(totals) is True
    assert has_usable_market_odds(_match("basketball", totals)) is True


def test_basketball_incomplete_total_rejected(isolated_data_dir):
    assert basketball_has_total({"total_line": 220.5}) is False
    assert basketball_has_total({"over": 1.90}) is False
    assert basketball_has_total({"over": 1.90, "under": 1.90}) is False


# ---------------------------------------------------------------------------
# 非覆盖内容
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "odds",
    [
        None,
        {},
        {"matchnum": "001"},
        {"jczq_no": "001"},
        {"handicap_line": -1.0},
        {"total_line": 2.5},
        {"home_win": None, "draw": None, "away_win": None},
    ],
)
def test_non_price_content_is_not_coverage(isolated_data_dir, odds):
    assert has_usable_market_odds(_match("football", odds)) is False
    assert has_usable_market_odds(_match("basketball", odds)) is False


def test_unsupported_sport_rejected(isolated_data_dir):
    assert has_usable_market_odds(_match("tennis", FOOTBALL_1X2)) is False


# ---------------------------------------------------------------------------
# 价格校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [None, True, False, "2.10", float("nan"), float("inf"), float("-inf"), 0, 1.0, -1.5, 0.5],
)
def test_invalid_prices_do_not_prove_coverage(isolated_data_dir, bad):
    assert is_valid_price(bad) is False
    assert has_usable_market_odds(
        _match("football", {"home_win": bad, "draw": 3.55, "away_win": 4.30})
    ) is False


@pytest.mark.parametrize("good", [1.01, 2, 3.55, 100.0])
def test_valid_prices_accepted(isolated_data_dir, good):
    assert is_valid_price(good) is True


@pytest.mark.parametrize(
    "value,expected",
    [(2.5, True), (0, True), (-1.0, True), (True, False), ("2.5", False), (float("nan"), False)],
)
def test_finite_number(isolated_data_dir, value, expected):
    assert is_finite_number(value) is expected


def test_total_line_must_be_finite_number(isolated_data_dir):
    odds = {"total_line": float("inf"), "over": 1.90, "under": 1.90}
    assert basketball_has_total(odds) is False
    assert has_usable_market_odds(_match("basketball", odds)) is False
