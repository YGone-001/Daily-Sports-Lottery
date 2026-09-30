"""
500.com 数据抓取适配器
========================
数据源优先级：
1. trade.500.com/static/public/{lot}/newxml/pl/pl_{play}_2.xml
   —— 竞彩实时赛程 + 赔率（XML，SSR，稳定，无需浏览器）★主数据源
2. live.500.com/wanchang.php  —— 完场/赛果（SSR，稳定）

关于 play 玩法代码：
    足球 jczq: spf(胜平负) / nspf(让球胜平负)
    篮球 jclq: sf(胜负) / sfc(胜分差) / dxf(大小分, 含 yszf 总分盘)

统一输出格式（内部标准化）：
{
    "id": "500j-2026-10-01-3303",
    "sport": "football" | "basketball",
    "league": "英超",
    "round": "",                      # 竞彩无轮次，用编号 matchnum 填充
    "date": "2026-10-01",
    "time": "20:00",
    "status": "upcoming" | "live" | "finished",
    "home": "曼城",
    "away": "阿森纳",
    "home_rank": null | int,
    "away_rank": null | int,
    "score": {"ft": [2, 1]} | None,
    "odds": {
        "home_win": 1.85,
        "draw": 3.40,                 # 篮球无平局, 为 None
        "away_win": 4.20,
        "handicap_line": -1.0,        # 让分（可选）
        "total_line": 215.5,          # 大小分盘口（可选）
        "over": 1.90, "under": 1.90,  # 大小分赔率（可选）
        "matchnum": "3001",           # 竞彩编号
    } | None,
}
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_CACHE: dict[str, tuple[float, object]] = {}
CACHE_TTL = 120  # 秒


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _cached(key: str, ttl: int, loader):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = loader()
    _CACHE[key] = (now, value)
    return value


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _parse_rank(token: str):
    """把 '[05]' 解析为 5，失败返回 None"""
    m = re.match(r"\[(\d+)\]", token.strip())
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 数据源 1: live.500.com 即时比分（SSR，含开赛时间 + 竞彩编号）★主数据源
# ---------------------------------------------------------------------------

# tr 属性: id="a1495377" order="3001" status="4" gy="联赛,主队,客队" lid="328"
_LIVE_TR_RE = re.compile(r'<tr\s+id="([ab])(\d+)"[^>]*?>', re.S)
_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')
_STATUS_MAP = {
    "0": "upcoming",   # 未开
    "1": "live",       # 上半场
    "2": "live",       # 中场
    "3": "live",       # 下半场
    "4": "finished",   # 完场
    "-1": "upcoming",  # 取消/推迟
    "-10": "upcoming",
    "-11": "upcoming",
    "-12": "upcoming",
    "-13": "upcoming",
    "-14": "upcoming",
}


def fetch_live_matches(sport: str = "football") -> list[dict]:
    """
    抓取 live.500.com 即时比分（足球 a / 篮球 b）。

    优势：SSR 直出，含**开赛时间**与**竞彩编号**，可与 fetch_jczq_xml
    的赔率数据按编号精确合并 —— 解决 XML 缺少开赛时间的问题。
    """
    key = f"live:{sport}"

    def loader():
        prefix = "b" if sport == "basketball" else "a"
        try:
            resp = requests.get(
                "https://live.500.com/", headers=REQUEST_HEADERS, timeout=20
            )
            resp.encoding = "gb2312"
            html = resp.text
        except Exception as exc:  # noqa: BLE001
            print(f"[500fetcher] live.500.com 抓取失败: {exc}")
            return []

        return _parse_live(html, prefix, sport)

    return _cached(key, 90, loader)


def _parse_live(html: str, prefix: str, sport: str) -> list[dict]:
    """解析即时比分页的比赛行"""
    out: list[dict] = []
    year = datetime.now(BEIJING_TZ).year

    # 按 <tr id="a123456" ...>...</tr> 切块
    for m in re.finditer(r'<tr\s+id="%s(\d+)"([^>]*)>(.*?)</tr>' % prefix, html, re.S):
        fid = m.group(1)
        attrs = dict(_ATTR_RE.findall(m.group(2)))
        body = m.group(3)

        tds = re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)
        if len(tds) < 9:
            continue

        def _text(idx):
            return _strip_tags(tds[idx]) if idx < len(tds) else ""

        # 联赛 / 轮次 / 时间 / 状态
        league = _text(1)
        round_name = _text(2)
        dt_raw = _text(3)                       # '09-30 14:00'
        status_raw = _text(4)
        matchnum = re.sub(r"[^\d]", "", _text(0))  # '周三001' -> '001'

        tm = re.match(r"(\d{2})-(\d{2})\s+(\d{2}:\d{2})", dt_raw)
        if not tm:
            continue
        mdate = f"{year}-{tm.group(1)}-{tm.group(2)}"
        hhmm = tm.group(3)
        try:
            parsed = datetime.strptime(mdate, "%Y-%m-%d").date()
            if (parsed - datetime.now(BEIJING_TZ).date()).days > 180:
                mdate = f"{year - 1}-{tm.group(1)}-{tm.group(2)}"
        except ValueError:
            continue

        # 队名：就近取 <a> 文本
        home = _team_from_td(tds[5]) if len(tds) > 5 else ""
        away = _team_from_td(tds[7]) if len(tds) > 7 else ""
        if not home or not away:
            continue

        # 比分：td[6] 形如 '2-1' 或空
        score = None
        sc_raw = _text(6)
        sc_m = re.search(r"(\d+)\s*-\s*(\d+)", sc_raw)
        if sc_m:
            score = {"ft": [int(sc_m.group(1)), int(sc_m.group(2))]}

        status = _STATUS_MAP.get(attrs.get("status", "0"), "upcoming")
        # 有比分且标记完场才认为 finished
        if status == "finished" and not score:
            status = "upcoming"

        out.append(
            {
                "id": f"500l-{mdate}-{attrs.get('order') or matchnum or fid}",
                "sport": sport,
                "league": league,
                "round": round_name,
                "date": mdate,
                "time": hhmm,
                "status": status,
                "home": home,
                "away": away,
                "home_rank": None,
                "away_rank": None,
                "score": score,
                "odds": None,
                "jczq_no": attrs.get("order") or "",  # 竞彩编号，用于与赔率合并
            }
        )
    return out


def _team_from_td(td_html: str) -> str:
    """从球队 <td> 提取队名：优先取 class=mainName，其次第一个 <a> 文本"""
    m = re.search(r'class="mainName"[^>]*>(.*?)</span>', td_html, re.S)
    if m:
        return _strip_tags(m.group(1))
    anchors = re.findall(r"<a[^>]*>(.*?)</a>", td_html, re.S)
    if anchors:
        return _strip_tags(anchors[0])
    return _strip_tags(td_html)


# ---------------------------------------------------------------------------
# 数据源 1b: live.500.com 篮球即时比分（内联 matchList + oddsList）
# ---------------------------------------------------------------------------

# matchList 字段下标（实测）
_LQ_STATUS = 0        # 1=未开, 11=完场, 其他=进行中
_LQ_ID = 1
_LQ_DATE = 3
_LQ_TIME = 4
_LQ_LEAGUE = 6
_LQ_ROUND = 9
_LQ_HOME = 13
_LQ_AWAY = 18
_LQ_HOME_SCORE = 22
_LQ_AWAY_SCORE = 23
_LQ_NO = 26

# 篮球状态码：1=未开赛；2/3/4=进行中(1/2/3节)；11=完场；-1/-10~=取消延期
_LQ_STATUS_MAP = {
    "1": "upcoming",
    "11": "finished",
    "12": "finished",
    "13": "finished",
    "-1": "upcoming",
    "-10": "upcoming",
    "-11": "upcoming",
    "-12": "upcoming",
    "-13": "upcoming",
    "-14": "upcoming",
}


def fetch_live_basketball() -> list[dict]:
    """
    抓取 live.500.com 篮球即时比分。

    篮球比分页把 matchList（赛程）与 oddsList（多机构赔率）直接内联在 HTML
    的 <script> 中，无需浏览器渲染。取「竞彩官方」赔率作为主赔率。
    """
    key = "live:basketball"

    def loader():
        try:
            resp = requests.get(
                "https://live.500.com/lq.php", headers=REQUEST_HEADERS, timeout=20
            )
            html = resp.content.decode("gb18030", errors="replace")
        except Exception as exc:  # noqa: BLE001
            print(f"[500fetcher] 篮球即时比分抓取失败: {exc}")
            return []

        return _parse_lq(html)

    return _cached(key, 90, loader)


def _js_array(html: str, name: str):
    """从 HTML 中提取 `var <name>=[...]` 并 JSON 解析"""
    m = re.search(r"var\s+%s\s*=\s*(\[.*?\])\s*;" % re.escape(name), html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def _js_object(html: str, name: str):
    """从 HTML 中提取 `var <name>={...}` 并 JSON 解析"""
    m = re.search(r"var\s+%s\s*=\s*(\{.*?\})\s*;" % re.escape(name), html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def _parse_lq(html: str) -> list[dict]:
    rows = _js_array(html, "matchList") or []
    odds_map = _js_object(html, "oddsList") or {}

    def _at(row, idx, default=""):
        return row[idx] if idx < len(row) else default

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, list):  # noqa: UP038
            continue
        mid = str(_at(row, _LQ_ID))
        home = str(_at(row, _LQ_HOME)).strip()
        away = str(_at(row, _LQ_AWAY)).strip()
        if not home or not away:
            continue

        mdate = str(_at(row, _LQ_DATE)).strip()
        hhmm = str(_at(row, _LQ_TIME)).strip() or "00:00"
        if not re.match(r"\d{4}-\d{2}-\d{2}", mdate):
            continue

        raw_status = str(_at(row, _LQ_STATUS))
        status = _LQ_STATUS_MAP.get(raw_status, "live")

        # 比分（篮球为 '18-17-25-15' 分段，取总分；'-' 表示未开始）
        score = None
        hs, as_ = str(_at(row, _LQ_HOME_SCORE)), str(_at(row, _LQ_AWAY_SCORE))
        hnums = re.findall(r"\d+", hs)
        anums = re.findall(r"\d+", as_)
        if hnums and anums and status in ("finished", "live"):
            score = {
                "ft": [sum(int(x) for x in hnums), sum(int(x) for x in anums)],
                "periods": {"home": hnums, "away": anums},
            }

        # 竞彩官方赔率（oddsList 中第一项 provider="1"）
        odds = None
        book = odds_map.get(mid) if isinstance(odds_map, dict) else None
        if isinstance(book, list):
            for entry in book:
                if isinstance(entry, list) and entry and str(entry[0]) == "1":
                    vals = list(entry) + [""] * (10 - len(entry))
                    try:
                        hw = float(vals[2]) if vals[2] else None
                        aw = float(vals[3]) if vals[3] else None
                    except (TypeError, ValueError):
                        hw = aw = None
                    if hw and aw:
                        odds = {"home_win": hw, "away_win": aw}
                    break

        out.append(
            {
                "id": f"500lq-{mdate}-{_at(row, _LQ_NO) or mid}",
                "sport": "basketball",
                "league": str(_at(row, _LQ_LEAGUE)).strip(),
                "round": str(_at(row, _LQ_ROUND)).strip(),
                "date": mdate,
                "time": hhmm,
                "status": status,
                "home": home,
                "away": away,
                "home_rank": None,
                "away_rank": None,
                "score": score,
                "odds": odds,
                "jczq_no": str(_at(row, _LQ_NO)).strip(),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 数据源 2: live.500.com 完场比分 (SSR)
# ---------------------------------------------------------------------------

def fetch_finished_matches(date: str | None = None) -> list[dict]:
    """
    抓取 500.com 完场比分页。
    date 形如 '2026-09-29'，None 表示页面默认（最近完赛）。
    返回标准化比赛列表。
    """
    key = f"wanchang:{date or 'default'}"

    def loader():
        url = "https://live.500.com/wanchang.php"
        params = {}
        if date:
            params["e"] = date
        try:
            resp = requests.get(
                url, params=params, headers=REQUEST_HEADERS, timeout=20
            )
            resp.encoding = "gb2312"
            return _parse_wanchang(resp.text)
        except Exception as exc:  # noqa: BLE001
            print(f"[500fetcher] wanchang 抓取失败: {exc}")
            return []

    return _cached(key, CACHE_TTL, loader)


# 行头正则: 联赛 [轮次] 日期 时间 状态
# 样例: '中北美国联 第2轮 09-29 10:00 完 [05] 1 危地马拉 6 - 0 萨尔瓦多 1 [02] 3 - 0 析 亚 欧 情'
# 变化: 轮次可选；排名"[05]"和数字"1"可出现在队名前后，也可能不存在
_HEAD_RE = re.compile(
    r"^(?P<league>\S+)\s+"
    r"(?:(?P<round>第\d+轮|小组赛|半决赛|决赛|1/\d+决赛|\d+/\d+决赛|决赛轮|附加赛)\s+)?"
    r"(?P<date>\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2})\s+"
    r"(?P<status>完|未|中|延|改期|取消|推迟|腰斩)\s+"
    r"(?P<body>.+)$"
)

# 比分分隔符: 主队 <比分> 客队，比分可能是 '6 - 0' 或 '-'（未开赛/改期）
_SCORE_SPLIT_RE = re.compile(r"\s(\d+\s*-\s*\d+|-)\s")


def _parse_team_cell(raw_html: str, order: str) -> tuple[str, int | None]:
    """
    从球队单元格原始 HTML 精确提取。

    结构（实测）:
        主队: <span class="gray">[05]</span><span class="yellowcard">1</span>
              <a ...><span class="mainName">危地马拉</span></a>
        客队: <a ...><span class="mainName">萨尔瓦多</span></a>
              <span class="yellowcard">1</span><span class="gray">[02]</span>

    - mainName   -> 主队名（权威来源）
    - clientName -> 客队名（权威来源）
    - gray       -> [排名]
    - yellowcard -> 黄牌/序号（丢弃）
    """
    # 队名: 主队 mainName / 客队 clientName，两者都兜底匹配
    name_m = (
        re.search(r'class="(?:mainName|clientName)"[^>]*>(.*?)</span>', raw_html, re.S)
    )
    if name_m:
        name = _strip_tags(name_m.group(1))
    else:
        # 兜底：取最后一个 <a> 标签内文本
        anchors = re.findall(r"<a[^>]*>(.*?)</a>", raw_html, re.S)
        name = _strip_tags(anchors[-1]) if anchors else _strip_tags(raw_html)

    # 排名
    rank = None
    gray_m = re.search(r'class="gray"[^>]*>\s*\[(\d+)\]', raw_html)
    if gray_m:
        rank = int(gray_m.group(1))

    if not name:
        # 兜底：按文本剥离
        name, rank = _parse_team_cell_fallback(_strip_tags(raw_html), order)

    return name.strip(), rank


def _parse_team_cell_fallback(cell: str, order: str) -> tuple[str, int | None]:
    """当 HTML 结构不含 mainName 时的文本兜底解析"""
    t = cell.strip()
    rank = None
    if order == "home":
        m = re.match(r"^\[(\d+)\]\s*", t)
        if m:
            rank = int(m.group(1))
            t = t[m.end():]
        t = re.sub(r"^\d{1,2}\s+", "", t)  # 去前置序号
    else:
        m = re.search(r"\[(\d+)\]\s*$", t)
        if m:
            rank = int(m.group(1))
            t = t[: m.start()]
        t = re.sub(r"\s+\d{1,2}$", "", t)  # 去尾部序号
    return t.strip(), rank


def _parse_wanchang(html: str) -> list[dict]:
    matches: list[dict] = []
    year = datetime.now(BEIJING_TZ).year

    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
        if len(tds) < 8:
            continue

        cells = [_strip_tags(td) for td in tds]

        league = cells[0]
        round_name = cells[1]
        dt_raw = cells[2]
        status_raw = cells[3]

        # 日期时间: '09-29 10:00'
        m = re.match(r"(\d{2})-(\d{2})\s+(\d{2}:\d{2})", dt_raw)
        if not m:
            continue
        mm, dd, hhmm = m.group(1), m.group(2), m.group(3)

        mdate = f"{year}-{mm}-{dd}"
        try:
            parsed = datetime.strptime(mdate, "%Y-%m-%d").date()
            if (parsed - datetime.now(BEIJING_TZ).date()).days > 180:
                mdate = f"{year - 1}-{mm}-{dd}"
        except ValueError:
            continue

        home, home_rank = _parse_team_cell(tds[4], "home")
        away, away_rank = _parse_team_cell(tds[6], "away")
        if not home or not away:
            continue

        # 全场比分: '6-0' 或 '-' / 'vs'
        score = None
        if re.match(r"^\d+\s*-\s*\d+$", cells[5].strip()):
            nums = [int(x) for x in re.split(r"\s*-\s*", cells[5].strip())]
            score = {"ft": nums}
            # 半场比分（可选）
            if len(cells) > 7 and re.match(r"^\d+\s*-\s*\d+$", cells[7].strip()):
                ht = [int(x) for x in re.split(r"\s*-\s*", cells[7].strip())]
                score["ht"] = ht

        status = {"完": "finished", "中": "live"}.get(status_raw, "upcoming")

        matches.append(
            {
                "id": f"500w-{mdate}-{len(matches)}",
                "sport": "football",
                "league": league,
                "round": round_name,
                "date": mdate,
                "time": hhmm,
                "status": status,
                "home": home,
                "away": away,
                "home_rank": home_rank,
                "away_rank": away_rank,
                "score": score,
                "odds": None,
            }
        )
    return matches


# ---------------------------------------------------------------------------
# 数据源 2: 竞彩实时赛程 + 赔率（trade.500.com XML，SSR）★主数据源
# ---------------------------------------------------------------------------

# 玩法代码 -> (彩种, sport, 主玩法)
PLAY_CODES = {
    "football": {"lot": "jczq", "plays": ["spf", "nspf"]},
    "basketball": {"lot": "jclq", "plays": ["sf", "dxf"]},
}

_XML_BASE = "https://trade.500.com/static/public/{lot}/newxml/pl/pl_{play}_2.xml"


def _fetch_xml(lot: str, play: str) -> str:
    """抓取竞彩 XML（返回 utf-8 文本）"""
    url = _XML_BASE.format(lot=lot, play=play)
    headers = dict(REQUEST_HEADERS)
    headers["Referer"] = "https://trade.500.com/"
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")


def _xml_attrs(tag: str) -> dict:
    """把 '<m a="1" b="2">' 属性串解析成 dict"""
    return dict(re.findall(r'([\w]+)="([^"]*)"', tag))


def _iter_xml_matches(xml: str):
    """迭代 XML 中的 <m> 比赛节点，返回 (attrs, [row_attrs, ...])"""
    for m in re.finditer(r"<m\s([^>]*?)>(.*?)</m>", xml, re.S):
        attrs = _xml_attrs(m.group(1))
        rows = [_xml_attrs(r.group(1)) for r in re.finditer(r"<row\s([^>]*?)/?>", m.group(2))]
        yield attrs, rows


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_jczq_xml(sport: str = "football") -> list[dict]:
    """
    抓取竞彩实时赛程（含 1X2 赔率、让分、大小分盘口）。

    使用 trade.500.com 的静态 XML（服务端直出，无需浏览器），
    是本系统「前瞻赛程」的主数据源。
    """
    key = f"jczqxml:{sport}"

    def loader():
        cfg = PLAY_CODES.get(sport)
        if not cfg:
            return []

        by_id: dict[str, dict] = {}      # 按竞彩 match id 聚合

        for play in cfg["plays"]:
            try:
                xml = _fetch_xml(cfg["lot"], play)
            except Exception as exc:  # noqa: BLE001
                print(f"[500fetcher] 竞彩XML {cfg['lot']}/{play} 失败: {exc}")
                continue

            for attrs, rows in _iter_xml_matches(xml):
                mid = attrs.get("id")
                if not mid:
                    continue
                latest = rows[0] if rows else {}

                entry = by_id.get(mid)
                if entry is None:
                    if not attrs.get("home") or not attrs.get("away"):
                        continue
                    entry = {
                        "id": f"500j-{attrs.get('date','')}-{attrs.get('matchnum', mid)}",
                        "sport": sport,
                        "league": attrs.get("league", "竞彩"),
                        "round": attrs.get("matchnum", ""),
                        "date": attrs.get("date", ""),
                        "time": "00:00",
                        "status": "upcoming",
                        "home": attrs.get("home", "").strip(),
                        "away": attrs.get("away", "").strip(),
                        "home_rank": None,
                        "away_rank": None,
                        "score": None,
                        "odds": {},
                    }
                    by_id[mid] = entry

                odds = entry["odds"]

                if play in ("spf", "nspf", "sf"):
                    # 胜平负（足球含平） / 胜负（篮球无平）
                    if odds.get("home_win") is None:
                        odds["home_win"] = _float(latest.get("win"))
                        odds["away_win"] = _float(latest.get("lost"))
                        if "draw" in latest:
                            odds["draw"] = _float(latest.get("draw"))
                        odds["matchnum"] = attrs.get("matchnum", "")
                elif play == "dxf":
                    # 大小分：yszf 为总分盘口
                    if odds.get("total_line") is None:
                        odds["total_line"] = _float(latest.get("yszf"))
                        odds["over"] = _float(latest.get("big"))
                        odds["under"] = _float(latest.get("small"))

        # 清洗：去掉空 dict / None 值
        out = []
        for entry in by_id.values():
            odds = {k: v for k, v in (entry.get("odds") or {}).items() if v is not None}
            entry["odds"] = odds or None
            if entry["odds"] and entry["odds"].get("matchnum"):
                # 竞彩编号 <= 竞赛日，开赛时间由 live 源或页脚补全；先用占位
                pass
            out.append(entry)

        return out

    return _cached(key, 180, loader)


# ---------------------------------------------------------------------------
# 数据源 3: 竞彩实时数据（浏览器渲染，已降级为兜底）
# ---------------------------------------------------------------------------

def fetch_jczq_live(playid: str = "269") -> list[dict]:
    """
    [兜底] 使用浏览器渲染抓取竞彩实时赛程+赔率。
    正常情况下应使用 fetch_jczq_xml（更快、更稳、无需浏览器）。
    playid: 269 = 竞彩足球, 275/313 = 竞彩篮球
    需要 playwright。若不可用则返回空列表。
    """
    key = f"jczq:{playid}"

    def loader():
        try:
            return _fetch_with_playwright(playid)
        except Exception as exc:  # noqa: BLE001
            print(f"[500fetcher] 浏览器抓取失败: {exc}")
            return []

    return _cached(key, 300, loader)


def _fetch_with_playwright(playid: str) -> list[dict]:
    from playwright.sync_api import sync_playwright

    url = f"https://trade.500.com/jczq/?playid={playid}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=REQUEST_HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
        )
        page.goto(url, wait_until="networkidle", timeout=30000)
        # 等待比赛行渲染
        try:
            page.wait_for_selector("tr[data-mid], .bet-tb-tr", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        html = page.content()
        browser.close()

    return _parse_jczq_html(html)


def _parse_jczq_html(html: str) -> list[dict]:
    """解析竞彩页面（渲染后 HTML），提取对阵 + 赔率"""
    matches: list[dict] = []
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    # 竞彩行通常带 data-mid 属性；退化为按 homesxname/awaysxname 解析
    blocks = re.split(r"<tr\s", html)
    for block in blocks:
        home_m = re.search(r'homesxname="([^"]+)"', block)
        away_m = re.search(r'awaysxname="([^"]+)"', block)
        if not (home_m and away_m):
            continue
        league_m = re.search(r'class="[^"]*league[^"]*"[^>]*>([^<]+)<', block)
        time_m = re.search(r"(\d{2}:\d{2})", block)
        odds = re.findall(r'class="pl"[^>]*>([\d\.]+)<', block)
        if len(odds) < 3:
            odds = re.findall(r">(\d\.\d{2})<", block)

        matches.append(
            {
                "id": f"500j-{today}-{len(matches)}",
                "sport": "football",
                "league": (league_m.group(1).strip() if league_m else "竞彩"),
                "round": "",
                "date": today,
                "time": time_m.group(1) if time_m else "00:00",
                "status": "upcoming",
                "home": home_m.group(1).strip(),
                "away": away_m.group(1).strip(),
                "home_rank": None,
                "away_rank": None,
                "score": None,
                "odds": (
                    {
                        "home_win": float(odds[0]),
                        "draw": float(odds[1]),
                        "away_win": float(odds[2]),
                    }
                    if len(odds) >= 3
                    else None
                ),
            }
        )
    return matches


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def fetch_daily_matches(date: str | None = None) -> dict:
    """抓取指定日期（默认今天）的赛事，聚合所有数据源。"""
    target = date or datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    result = {
        "date": target,
        "football": [],
        "basketball": [],
        "fetched_at": datetime.now(BEIJING_TZ).isoformat(),
        "source": "500.com",
    }

    # 竞彩实时（浏览器）
    for playid, sport in (("269", "football"), ("275", "basketball")):
        for m in fetch_jczq_live(playid):
            m["sport"] = sport
            result[sport].append(m)

    # 若无实时数据，用完场数据补齐历史
    if not result["football"]:
        finished = fetch_finished_matches(target)
        result["football"] = [m for m in finished if m["date"] == target]

    return result


if __name__ == "__main__":
    import json

    print("=" * 60)
    print("测试 1: 抓取完场比分 (SSR)")
    print("=" * 60)
    fin = fetch_finished_matches()
    print(f"抓到 {len(fin)} 场完赛")
    for m in fin[:5]:
        print(
            f"  [{m['league']}] {m['date']} {m['time']} "
            f"{m['home']}({m['home_rank']}) {m['score']['ft'][0]}-{m['score']['ft'][1]} "
            f"{m['away']}({m['away_rank']})"
        )

    print()
    print("=" * 60)
    print("测试 2: 竞彩实时 (浏览器)")
    print("=" * 60)
    live = fetch_jczq_live("269")
    print(f"抓到 {len(live)} 场竞彩")
    for m in live[:5]:
        print(f"  {m['time']} {m['home']} vs {m['away']}  赔率={m['odds']}")
