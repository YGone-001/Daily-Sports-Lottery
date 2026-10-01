"""
500.com 数据源兼容性测试
========================
全部使用 mock 响应，**不依赖 500.com 线上可用性**。

覆盖：
- 足球即时比分：新 `gy` 布局 / 旧索引布局 / 空页 / 布局漂移
- 篮球即时比分：matchList 有数据 / 空 matchList / 表格行兜底
- 竞彩赔率 XML：有赔率 -> 可用盘口；空 XML -> 无盘口覆盖
- HTTP 失败（404 / 500 / 超时）-> 受控失败，不写入运行时文件
- 结构化数据源诊断（unavailable / empty / ok）
- 时间语义：live 源带权威开赛时间；XML 源为占位 00:00
"""
from __future__ import annotations

import copy
import os
from datetime import timedelta

import pytest
import requests as requests_lib

import config
from utils import fetcher_500
from utils.market_coverage import has_usable_market_odds

BEIJING_OFFSET = timedelta(hours=8)


# ---------------------------------------------------------------------------
# mock 基础设施
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, body: str, status: int = 200, encoding: str = "utf-8"):
        self.status_code = status
        self.encoding = encoding
        self._body = body.encode(encoding, errors="replace")

    @property
    def content(self) -> bytes:
        return self._body

    @property
    def text(self) -> str:
        return self._body.decode(self.encoding, errors="replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests_lib.HTTPError(f"HTTP {self.status_code}")


class _StubRequests:
    """按 URL 片段路由的 requests 替身。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        # 按片段长度降序匹配，保证更具体的路由优先（例如 weekfixture.php 先于 live.500.com/）
        for fragment in sorted(self.routes, key=len, reverse=True):
            if fragment in url:
                response = self.routes[fragment]
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"未预期的 URL: {url}")


@pytest.fixture(autouse=True)
def _reset_fetcher_state():
    fetcher_500._CACHE.clear()
    fetcher_500._DIAGNOSTICS.clear()
    yield
    fetcher_500._CACHE.clear()
    fetcher_500._DIAGNOSTICS.clear()


def _patch(monkeypatch, routes: dict) -> _StubRequests:
    stub = _StubRequests(routes)
    monkeypatch.setattr(fetcher_500, "requests", stub)
    return stub


# ---------------------------------------------------------------------------
# 样例响应
# ---------------------------------------------------------------------------

# 新布局：weekfixture.php —— gy 属性权威给出 联赛/主队/客队，列数可变
NEW_LAYOUT_HTML = """
<table><tbody>
<tr id="a1396100" gy="美女职,西雅图统治女足,北卡罗莱纳女足" lid="802" class="">
  <td>美女职</td><td>联赛</td><td>10-03&nbsp;10:00</td>
  <td class="gray">[10] 西雅图统治女足</td><td>-</td>
  <td class="gray">北卡罗莱纳女足 [09]</td><td></td><td class="live_animate"></td>
  <td class="red hide">析 亚 欧 情</td>
</tr>
<tr id="a1495652" status="3" gy="阿曼联杯,巴赫拉,法纳加" lid="2605" fid="1495652" class="">
  <td></td><td>阿曼联杯</td><td>分组赛</td><td>10-01&nbsp;22:25</td><td>&nbsp;</td>
  <td class="gray">[01] 1 巴赫拉</td><td class="pk">4 - 0</td>
  <td class="yellowcard">法纳加 1 [04]</td><td>1 - 0</td><td class="live_animate"></td>
  <td class="red hide">析 亚 欧 荐</td><td class="icon_notop">置顶</td>
</tr>
</tbody></table>
"""

# 旧布局：live.500.com/ —— 固定列索引 + order/status 属性 + mainName
LEGACY_LAYOUT_HTML = """
<table><tbody>
<tr id="a900001" order="3001" status="0" gy="" lid="1" class="">
  <td>周六001</td><td>英超</td><td>第1轮</td><td>10-04 20:00</td><td>未</td>
  <td><a href="/x"><span class="mainName">曼城</span></a></td><td> </td>
  <td><a href="/y"><span class="mainName">阿森纳</span></a></td><td>析</td>
</tr>
<tr id="a900002" order="3002" status="4" gy="" lid="1" class="">
  <td>周六002</td><td>西甲</td><td>第1轮</td><td>10-04 22:00</td><td>完</td>
  <td><a href="/x"><span class="mainName">皇马</span></a></td><td>2 - 1</td>
  <td><a href="/y"><span class="mainName">巴萨</span></a></td><td>析</td>
</tr>
</tbody></table>
"""

EMPTY_TABLE_HTML = "<html><body><table><tr><td>无赛事</td></tr></table></body></html>"

# 篮球：matchList + oddsList 内联
BASKETBALL_HTML = """
<script>
var matchList=[
 ["1","243049","x","2026-10-03","19:30","","NBA","","","第1轮","","","","湖人","","","","","凯尔特人","","","","","","","","3001"]
];
var oddsList={"243049":[["1","官方","1.80","2.00"]]};
</script>
"""

BASKETBALL_EMPTY_HTML = """
<script>
var matchList=[];
var oddsList={};
</script>
"""

# 竞彩赔率 XML（spf 提供 1X2）
ODDS_XML_SPF = """<?xml version="1.0" encoding="utf-8"?>
<xml>
<m id="122025" date="2026-10-03" dayofweek="星期六" matchnum="3001" league="英超"
   home="曼城" away="阿森纳">
  <row win="1.85" draw="3.40" lost="4.20" w="0" d="0" l="0" />
</m>
</xml>
"""

ODDS_XML_NSPF = """<?xml version="1.0" encoding="utf-8"?>
<xml>
<m id="122025" date="2026-10-03" dayofweek="星期六" matchnum="3001" league="英超"
   home="曼城" away="阿森纳">
  <row win="3.10" draw="3.30" lost="2.10" w="1" d="0" l="0" />
</m>
</xml>
"""

ODDS_XML_EMPTY = '<?xml version="1.0" encoding="utf-8"?><xml></xml>'


# ---------------------------------------------------------------------------
# 足球即时比分解析
# ---------------------------------------------------------------------------

def test_football_new_layout_parsing():
    rows, errors = fetcher_500._parse_live(NEW_LAYOUT_HTML, "a", "football")

    assert errors == 0
    assert len(rows) == 2

    first = rows[0]
    assert first["home"] == "西雅图统治女足"
    assert first["away"] == "北卡罗莱纳女足"
    assert first["league"] == "美女职"
    assert (first["date"], first["time"]) == ("2026-10-03", "10:00")
    assert first["kickoff_time_known"] is True
    assert first["status"] == "upcoming"
    assert first["score"] is None
    assert first["home_rank"] == 10 and first["away_rank"] == 9

    second = rows[1]
    assert second["status"] == "live"
    assert second["score"] == {"ft": [4, 0]}  # 取 pk 全场比分，不误取半场 '1 - 0'
    assert second["round"] == "分组赛"


def test_football_legacy_layout_still_parses():
    rows, errors = fetcher_500._parse_live(LEGACY_LAYOUT_HTML, "a", "football")

    assert errors == 0
    assert len(rows) == 2
    assert rows[0]["home"] == "曼城" and rows[0]["away"] == "阿森纳"
    assert rows[0]["jczq_no"] == "3001"          # order 属性 -> 竞彩编号
    assert rows[0]["kickoff_time_known"] is True
    assert rows[0]["league"] == "英超"

    finished = rows[1]
    assert finished["jczq_no"] == "3002"
    assert finished["score"] == {"ft": [2, 1]}
    assert finished["status"] == "finished"


def test_football_empty_payload_yields_no_rows():
    rows, errors = fetcher_500._parse_live(EMPTY_TABLE_HTML, "a", "football")
    assert rows == []
    assert errors == 0


def test_football_layout_drift_extra_fields_and_reorder():
    """列数/列序变化与额外字段下仍能解析（靠 gy + 时间模式定位）。"""
    html = """
    <table>
    <tr id="a1" gy="日职,横滨水手,川崎前锋" lid="9" extra="x" class="odd">
      <td class="newcol">新增列</td>
      <td>10-05 18:00</td>
      <td class="gray">[03] 横滨水手</td>
      <td class="whatever">-</td>
      <td class="gray">川崎前锋 [07]</td>
      <td>日职</td><td>第2轮</td><td class="live_animate"></td>
    </tr>
    </table>
    """
    rows, errors = fetcher_500._parse_live(html, "a", "football")

    assert errors == 0
    assert len(rows) == 1
    assert rows[0]["league"] == "日职"
    assert rows[0]["home"] == "横滨水手"
    assert rows[0]["away"] == "川崎前锋"
    assert rows[0]["time"] == "18:00"


def test_football_row_without_kickoff_time_is_counted_not_emitted():
    html = """
    <table>
    <tr id="a1" gy="英超,曼城,阿森纳" lid="1">
      <td>英超</td><td>待定</td><td>曼城</td><td>-</td><td>阿森纳</td>
    </tr>
    </table>
    """
    rows, errors = fetcher_500._parse_live(html, "a", "football")
    assert rows == []
    assert errors == 1


# ---------------------------------------------------------------------------
# 篮球即时比分解析
# ---------------------------------------------------------------------------

def test_basketball_matchlist_parsing():
    rows, errors = fetcher_500._parse_lq(BASKETBALL_HTML)

    assert errors == 0
    assert len(rows) == 1
    row = rows[0]
    assert row["sport"] == "basketball"
    assert row["home"] == "湖人" and row["away"] == "凯尔特人"
    assert row["date"] == "2026-10-03" and row["time"] == "19:30"
    assert row["kickoff_time_known"] is True
    assert row["jczq_no"] == "3001"
    assert row["odds"] == {"home_win": 1.80, "away_win": 2.00}


def test_basketball_empty_matchlist_is_empty_not_error():
    rows, errors = fetcher_500._parse_lq(BASKETBALL_EMPTY_HTML)
    assert rows == []
    assert errors == 0


def test_basketball_table_row_fallback():
    """当 matchList 为空时，若页面直出 <tr id="bN"> 表格行，同样可解析。"""
    html = """
    <table>
    <tr id="b777" gy="NBA,湖人,凯尔特人" lid="1" status="1">
      <td>NBA</td><td>常规赛</td><td>10-05 09:00</td>
      <td class="gray">[01] 湖人</td><td class="pk">-</td>
      <td class="gray">凯尔特人 [02]</td>
    </tr>
    </table>
    """
    rows, errors = fetcher_500._parse_live(html, "b", "basketball")
    assert errors == 0
    assert len(rows) == 1
    assert rows[0]["sport"] == "basketball"
    assert rows[0]["home"] == "湖人" and rows[0]["away"] == "凯尔特人"


def test_fetch_live_basketball_empty_source_diagnostic(monkeypatch):
    _patch(monkeypatch, {"lq.php": _Resp(BASKETBALL_EMPTY_HTML)})
    rows = fetcher_500.fetch_live_basketball()

    assert rows == []
    diag = fetcher_500.source_diagnostics()["live_basketball"]
    assert diag["status"] == "empty"
    assert diag["rows"] == 0


# ---------------------------------------------------------------------------
# 竞彩赔率 XML
# ---------------------------------------------------------------------------

def test_odds_xml_produces_usable_market_odds(monkeypatch):
    _patch(monkeypatch, {"pl_spf_2.xml": _Resp(ODDS_XML_SPF),
                         "pl_nspf_2.xml": _Resp(ODDS_XML_NSPF)})
    rows = fetcher_500.fetch_jczq_xml("football")

    assert len(rows) == 1
    entry = rows[0]
    assert entry["jczq_no"] == "3001"
    assert entry["home"] == "曼城" and entry["away"] == "阿森纳"
    assert entry["odds"]["home_win"] == 1.85
    assert entry["odds"]["draw"] == 3.40
    assert entry["odds"]["away_win"] == 4.20
    assert has_usable_market_odds(entry) is True
    # XML 源不提供开赛时间 -> 占位 00:00 且标记为未知
    assert entry["time"] == "00:00"
    assert entry["kickoff_time_known"] is False
    assert fetcher_500.source_diagnostics()["jczq_odds_football"]["status"] == "ok"


def test_empty_odds_xml_is_not_market_coverage(monkeypatch):
    _patch(monkeypatch, {"pl_spf_2.xml": _Resp(ODDS_XML_EMPTY),
                         "pl_nspf_2.xml": _Resp(ODDS_XML_EMPTY)})
    rows = fetcher_500.fetch_jczq_xml("football")

    assert rows == []
    diag = fetcher_500.source_diagnostics()["jczq_odds_football"]
    assert diag["status"] == "empty"
    assert diag["rows"] == 0


def test_odds_xml_missing_odds_is_not_coverage(monkeypatch):
    """只有编号、没有价格的行不构成盘口覆盖，也不伪造赔率。"""
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <xml><m id="1" date="2026-10-03" matchnum="3001" league="英超"
      home="曼城" away="阿森纳"><row w="0" d="0" l="0" /></m></xml>
    """
    _patch(monkeypatch, {"pl_spf_2.xml": _Resp(xml), "pl_nspf_2.xml": _Resp(ODDS_XML_EMPTY)})
    rows = fetcher_500.fetch_jczq_xml("football")

    assert len(rows) == 1
    odds = rows[0]["odds"] or {}
    # 不伪造任何价格：三个价格字段都不存在
    assert "home_win" not in odds and "draw" not in odds and "away_win" not in odds
    assert has_usable_market_odds(rows[0]) is False


def test_odds_xml_malformed_row_does_not_crash(monkeypatch):
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <xml>
      <m id="1" date="2026-10-03" matchnum="3001" league="英超" home="曼城">
        <row win="1.85" draw="3.3" lost="4.0" />
      </m>
      <m id="2" date="2026-10-03" matchnum="3002" league="英超" home="A" away="B">
        <row win="not-a-number" draw="3.3" lost="2.0" />
      </m>
    </xml>
    """
    _patch(monkeypatch, {"pl_spf_2.xml": _Resp(xml), "pl_nspf_2.xml": _Resp(ODDS_XML_EMPTY)})
    rows = fetcher_500.fetch_jczq_xml("football")

    # 缺 away 的行被拒绝；非法价格被丢弃（不伪造），但合法价格保留
    assert [r["jczq_no"] for r in rows] == ["3002"]
    odds = rows[0]["odds"]
    assert "home_win" not in odds          # 'not-a-number' -> 丢弃，不猜测
    assert odds["draw"] == 3.3
    assert odds["away_win"] == 2.0
    assert has_usable_market_odds(rows[0]) is False


# ---------------------------------------------------------------------------
# HTTP 失败
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failure", [
    _Resp("<html>404</html>", status=404),
    _Resp("<html>500</html>", status=500),
    TimeoutError("timeout"),
])
def test_football_http_failure_is_controlled(monkeypatch, failure, isolated_data_dir):
    _patch(monkeypatch, {
        "live.500.com/": failure,
        "weekfixture.php": failure,
        "2h1.php": failure,
    })
    rows = fetcher_500.fetch_live_matches("football")

    assert rows == []
    diag = fetcher_500.source_diagnostics()["live_football"]
    assert diag["status"] == "unavailable"
    # 受控失败：不产生任何运行时文件
    assert os.listdir(isolated_data_dir) == []


def test_basketball_http_failure_is_controlled(monkeypatch, isolated_data_dir):
    _patch(monkeypatch, {"lq.php": _Resp("<html>404</html>", status=404)})
    rows = fetcher_500.fetch_live_basketball()

    assert rows == []
    assert fetcher_500.source_diagnostics()["live_basketball"]["status"] == "unavailable"
    assert os.listdir(isolated_data_dir) == []


def test_odds_http_failure_is_controlled(monkeypatch, isolated_data_dir):
    _patch(monkeypatch, {
        "pl_spf_2.xml": TimeoutError("timeout"),
        "pl_nspf_2.xml": _Resp("<html>404</html>", status=404),
    })
    rows = fetcher_500.fetch_jczq_xml("football")

    assert rows == []
    assert fetcher_500.source_diagnostics()["jczq_odds_football"]["status"] == "unavailable"
    assert os.listdir(isolated_data_dir) == []


def test_partial_source_failure_still_returns_available_data(monkeypatch):
    """一个候选页面失败不应影响其他可用页面的数据。"""
    _patch(monkeypatch, {
        "live.500.com/": _Resp("<html>err</html>", status=500),
        "weekfixture.php": _Resp(NEW_LAYOUT_HTML),
        "2h1.php": TimeoutError("timeout"),
    })
    rows = fetcher_500.fetch_live_matches("football")

    assert len(rows) == 2
    assert fetcher_500.source_diagnostics()["live_football"]["status"] == "ok"


# ---------------------------------------------------------------------------
# 契约与不可变性
# ---------------------------------------------------------------------------

def test_output_schema_preserved(monkeypatch):
    _patch(monkeypatch, {
        "live.500.com/": _Resp(NEW_LAYOUT_HTML),
        "weekfixture.php": _Resp(EMPTY_TABLE_HTML),
        "2h1.php": _Resp(EMPTY_TABLE_HTML),
    })
    row = fetcher_500.fetch_live_matches("football")[0]

    required = {
        "id", "sport", "league", "round", "date", "time", "kickoff_time_known",
        "status", "home", "away", "home_rank", "away_rank", "score", "odds",
        "jczq_no",
    }
    assert required.issubset(row.keys())
    assert row["sport"] == "football"
    assert row["odds"] is None                     # live 源不提供赔率，不伪造
    assert row["kickoff_time_known"] is True


def test_number_of_live_pages_called_is_cached(monkeypatch):
    stub = _patch(monkeypatch, {
        "live.500.com/": _Resp(EMPTY_TABLE_HTML),
        "weekfixture.php": _Resp(NEW_LAYOUT_HTML),
        "2h1.php": _Resp(EMPTY_TABLE_HTML),
    })
    first = fetcher_500.fetch_live_matches("football")
    calls_after_first = len(stub.calls)
    second = fetcher_500.fetch_live_matches("football")

    assert first == second
    assert len(stub.calls) == calls_after_first  # 命中缓存，不重复请求


def test_fetchers_do_not_mutate_config_or_globals(monkeypatch):
    config_before = copy.deepcopy(config.MODEL_CONFIG)
    versions_before = copy.deepcopy(config.MODEL_VERSIONS)

    _patch(monkeypatch, {
        "live.500.com/": _Resp(NEW_LAYOUT_HTML),
        "weekfixture.php": _Resp(EMPTY_TABLE_HTML),
        "2h1.php": _Resp(EMPTY_TABLE_HTML),
        "lq.php": _Resp(BASKETBALL_HTML),
        "pl_spf_2.xml": _Resp(ODDS_XML_SPF),
        "pl_nspf_2.xml": _Resp(ODDS_XML_NSPF),
    })
    fetcher_500.fetch_live_matches("football")
    fetcher_500.fetch_live_basketball()
    fetcher_500.fetch_jczq_xml("football")

    assert config.MODEL_CONFIG == config_before
    assert config.MODEL_VERSIONS == versions_before
