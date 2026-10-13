"""
可选启用的源证据保全抽取（Opt-In Provider Evidence Preservation）
================================================================
本模块在**不改变任何生产解析行为**的前提下，把「数据源实际提供了什么标识符」在
归一化**丢弃之前**捕获下来，并以只读、确定性的侧车（sidecar）形式记录。

为什么需要它
------------
`utils/fetcher_500.py` 的五个解析路径都会把 provider 自己的行/比赛标识符丢弃，只保留
一个**推导出的**源 ID（见 `utils.source_identity.PROVIDER_EVIDENCE_INVENTORY`）。
一旦丢弃，事后既无法审计、也无法重建。本模块提供一个**显式调用**的抽取入口，让证据
可以被保全，而运行期解析路径**完全不感知**它的存在。

严格边界
--------
- **不联网。** 只接受已经取得的源夹具（HTML / XML 文本）；绝不发起任何请求。
- **不修改输入。** 源夹具字符串与解析器产出的比赛行都不会被改写。
- **不改变既有返回值。** `utils/fetcher_500` 的公开函数（`fetch_live_matches` /
  `fetch_live_basketball` / `fetch_jczq_xml` / `fetch_finished_matches` /
  `fetch_jczq_live`）一个都不调用、一个都不修改；`scraper.refresh()` 的行为不变。
- **不合成 provider 标识符。** 只记录夹具中**确实存在**的值；缺失即缺失。
- **不写盘。** 不写 `data/*.json`，不写治理账本，不产生任何映射提案。
- **不把行位置提升为身份。** 行序号只作为「位置」事实记录，永不作为 provider 标识符。

原始标识符类型判定规则（刻意保守）
----------------------------------
====================  =========================================================
HTML ``id`` 属性        -> ``HTML_ELEMENT_ID``：语义在仓库内无法证实，
                          唯一性作用域一律 ``UNKNOWN``。
显式命名的 provider 字段  -> ``PROVIDER_MATCH_ID``：例如 XML ``<m id>``、
（``<m id>`` / matchList  ``matchList[_LQ_ID]``、``data-mid``。**仍然**按
比赛号下标 / ``data-mid``） ``UNKNOWN`` 作用域记录：源提供了它，但仓库没有
                          任何独立文档化的唯一性 / 跨刷新稳定性保证。
竞彩编号字段             -> ``COMPETITION_NUMBER``：作用域为
                          ``(sport, date, normalized_number)``。
====================  =========================================================

因此抽取出的观测**永远不会**产生一个「充分」的 provider 身份键：它们只能被观测、
复核与用于冲突检测，绝不授权活动绑定（fail closed）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from utils import fetcher_500
from utils.source_identity import (
    ProviderIdUniquenessScope,
    RawIdentifierType,
    SourceObservation,
    canonical_json,
    fingerprint,
    observe_source_row,
    resolve_provider_id_uniqueness_scope,
)

#: 侧车 schema 版本。任何字段语义变化都必须提升该版本。
PROVIDER_EVIDENCE_SCHEMA_VERSION = "1.0.0"

#: 支持抽取的适配器（与 ``utils.source_identity.SOURCE_ID_PATTERNS`` 一一对应）。
PROVIDER_EVIDENCE_ADAPTERS: tuple[str, ...] = (
    "live_football",
    "live_basketball",
    "market_xml",
    "finished_feed",
    "html_fallback",
)

#: 每个适配器对应的解析器标识（与溯源层的 ``extractor_identity`` 对齐）。
_EXTRACTOR_IDENTITY: dict[str, str] = {
    "live_football": "utils.fetcher_500._parse_live@1",
    "live_basketball": "utils.fetcher_500._parse_lq@1",
    "market_xml": "utils.fetcher_500.fetch_jczq_xml@1",
    "finished_feed": "utils.fetcher_500._parse_wanchang@1",
    "html_fallback": "utils.fetcher_500._parse_jczq_html@1",
}

_POSITION_NOTE = "行位置只作为位置事实记录，绝不提升为 provider 标识符。"


# =============================================================================
# 数据类型
# =============================================================================

@dataclass(frozen=True)
class ProviderEvidenceRecord:
    """
    一条**原始标识符证据**记录（侧车中的一行）。

    刻意把四种东西分开存放，绝不合并：

    - ``raw_provider_id``：显式 provider 字段的值（缺失为 ``None``）。
    - ``html_element_id``：HTML ``id`` 属性的值（缺失为 ``None``）；**不假定**它是
      稳定的比赛标识。
    - ``competition_number_raw``：竞彩编号原始值（缺失为 ``None``）。
    - ``row_ordinal``：该行在响应中的位置（**永不是**身份）。
    """

    row_index: int
    derived_source_id: str | None
    raw_provider_id: str | None
    raw_provider_id_type: str
    provider_id_uniqueness_scope: str
    competition_number_raw: str | None
    html_element_id: str | None
    row_ordinal: int
    raw_attributes: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "row_index": self.row_index,
            "derived_source_id": self.derived_source_id,
            "raw_provider_id": self.raw_provider_id,
            "raw_provider_id_type": self.raw_provider_id_type,
            "provider_id_uniqueness_scope": self.provider_id_uniqueness_scope,
            "competition_number_raw": self.competition_number_raw,
            "html_element_id": self.html_element_id,
            "row_ordinal": self.row_ordinal,
            "raw_attributes": dict(self.raw_attributes),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SourceProvenanceSidecar:
    """
    一次抽取的**源证据侧车**。

    它**不是**运行期比赛 schema 的一部分：绝不写入 `daily_matches.json`，也不被
    `scraper.refresh()` 读取。
    """

    evidence_schema_version: str
    adapter: str
    parser_identity: str
    source_fixture_sha256: str
    records: tuple[ProviderEvidenceRecord, ...]

    def as_dict(self) -> dict:
        return {
            "evidence_schema_version": self.evidence_schema_version,
            "adapter": self.adapter,
            "parser_identity": self.parser_identity,
            "source_fixture_sha256": self.source_fixture_sha256,
            "record_count": len(self.records),
            "records": [record.as_dict() for record in self.records],
        }

    def fingerprint(self) -> str:
        """侧车的确定性指纹（记录按 ``row_index`` 排序后哈希，与输入顺序无关）。"""
        ordered = sorted(self.records, key=lambda r: r.row_index)
        return fingerprint(
            {
                "evidence_schema_version": self.evidence_schema_version,
                "adapter": self.adapter,
                "parser_identity": self.parser_identity,
                "source_fixture_sha256": self.source_fixture_sha256,
                "records": [record.as_dict() for record in ordered],
            }
        )


@dataclass(frozen=True)
class SourceEvidenceExtraction:
    """
    一次可选启用抽取的完整结果。

    ``normalized_rows`` 与既有解析器对**同一夹具**的输出逐字一致；
    ``provenance_observations`` 是与之一一对应的只读证据观测。
    """

    adapter: str
    normalized_rows: tuple[dict, ...]
    provenance_observations: tuple[SourceObservation, ...]
    sidecar: SourceProvenanceSidecar

    def __iter__(self) -> Iterator[Any]:
        """允许 ``rows, observations = extract_source_evidence(...)`` 形式解包。"""
        yield list(self.normalized_rows)
        yield list(self.provenance_observations)

    def as_dict(self) -> dict:
        return {
            "evidence_schema_version": PROVIDER_EVIDENCE_SCHEMA_VERSION,
            "adapter": self.adapter,
            "normalized_row_count": len(self.normalized_rows),
            "observation_count": len(self.provenance_observations),
            "sidecar": self.sidecar.as_dict(),
            "sidecar_fingerprint": self.sidecar.fingerprint(),
            "observation_fingerprints": sorted(
                o.semantic_fingerprint() for o in self.provenance_observations
            ),
        }


# =============================================================================
# 通用工具
# =============================================================================

def _fixture_sha256(fixture: Any) -> str:
    """夹具内容的确定性指纹（字符串或 ``{play: xml}`` 映射都支持）。"""
    if isinstance(fixture, Mapping):
        payload = {str(k): str(v) for k, v in sorted(fixture.items())}
        return fingerprint({"kind": "MULTI_DOCUMENT_FIXTURE", "documents": payload})
    return fingerprint({"kind": "SINGLE_DOCUMENT_FIXTURE", "document": str(fixture)})


def _attributes_of(tag: str) -> dict[str, str]:
    """把一段属性串解析成 dict（复用既有属性正则）。"""
    return dict(fetcher_500._ATTR_RE.findall(tag))


def _pop_by_key(
    index: dict[str, list[Any]], key: str
) -> Any | None:
    """从 ``key -> [候选, ...]`` 索引中按出现顺序取出一个候选（确定性）。"""
    bucket = index.get(key)
    if not bucket:
        return None
    return bucket.pop(0)


def _index_by_key(items: Sequence[tuple[str, Any]]) -> dict[str, list[Any]]:
    index: dict[str, list[Any]] = {}
    for key, value in items:
        index.setdefault(key, []).append(value)
    return index


def _live_tr_records(html: str, prefix: str) -> list[dict[str, Any]]:
    """
    按文档顺序收集 ``<tr id="{prefix}{digits}" ...>`` 的原始属性。

    只读取属性，不修改输入，也不复现任何解析判定。
    """
    out: list[dict[str, Any]] = []
    for match in fetcher_500._LIVE_ROW_RE.finditer(html):
        if match.group(1) != prefix:
            continue
        attrs = _attributes_of(match.group(3))
        out.append(
            {
                "fid": match.group(2),
                "attrs": attrs,
                "order": (attrs.get("order") or "").strip(),
            }
        )
    return out


def _evidence_record(
    *,
    adapter: str,
    row_index: int,
    row: Mapping[str, Any],
    derived_source_id: str | None,
    raw_provider_id: str | None,
    raw_provider_id_type: str,
    competition_number_raw: str | None,
    html_element_id: str | None,
    raw_attributes: Sequence[tuple[str, str]],
    notes: Sequence[str],
) -> ProviderEvidenceRecord:
    uniqueness_scope = (
        ProviderIdUniquenessScope.NOT_APPLICABLE.value
        if raw_provider_id is None
        else resolve_provider_id_uniqueness_scope(
            adapter=adapter,
            provider_namespace=(derived_source_id or "").split("-", 1)[0],
            sport=str(row.get("sport") or ""),
            raw_identifier_type=raw_provider_id_type,
        )
    )
    return ProviderEvidenceRecord(
        row_index=row_index,
        derived_source_id=derived_source_id,
        raw_provider_id=raw_provider_id,
        raw_provider_id_type=raw_provider_id_type,
        provider_id_uniqueness_scope=uniqueness_scope,
        competition_number_raw=competition_number_raw,
        html_element_id=html_element_id,
        row_ordinal=row_index,
        raw_attributes=tuple(sorted(raw_attributes)),
        notes=tuple(notes),
    )


def _observations_for(
    adapter: str,
    rows: Sequence[Mapping[str, Any]],
    records: Sequence[ProviderEvidenceRecord],
    canonical_match_id_by_row: Mapping[int, str] | None,
) -> tuple[SourceObservation, ...]:
    """由「规范化行 + 证据记录」构造只读观测（一一对应，绝不改写行）。"""
    out: list[SourceObservation] = []
    for index, row in enumerate(rows):
        record = records[index]
        canonical_id = (
            canonical_match_id_by_row.get(index)
            if canonical_match_id_by_row is not None
            else None
        )
        out.append(
            observe_source_row(
                row,
                adapter=adapter,
                canonical_match_id=canonical_id,
                raw_provider_id=record.raw_provider_id,
                raw_provider_id_type=(
                    record.raw_provider_id_type if record.raw_provider_id else None
                ),
                raw_provider_id_uniqueness_scope=(
                    record.provider_id_uniqueness_scope
                    if record.raw_provider_id
                    else None
                ),
            )
        )
    return tuple(out)


def _build(
    *,
    adapter: str,
    fixture: Any,
    rows: Sequence[Mapping[str, Any]],
    records: Sequence[ProviderEvidenceRecord],
    canonical_match_id_by_row: Mapping[int, str] | None,
) -> SourceEvidenceExtraction:
    if len(rows) != len(records):
        raise ValueError(
            "provider evidence extraction must produce one record per normalized row "
            f"(adapter={adapter}, rows={len(rows)}, records={len(records)})"
        )
    sidecar = SourceProvenanceSidecar(
        evidence_schema_version=PROVIDER_EVIDENCE_SCHEMA_VERSION,
        adapter=adapter,
        parser_identity=_EXTRACTOR_IDENTITY[adapter],
        source_fixture_sha256=_fixture_sha256(fixture),
        records=tuple(records),
    )
    return SourceEvidenceExtraction(
        adapter=adapter,
        normalized_rows=tuple(dict(row) for row in rows),
        provenance_observations=_observations_for(
            adapter, rows, records, canonical_match_id_by_row
        ),
        sidecar=sidecar,
    )


# =============================================================================
# 各适配器的抽取实现
# =============================================================================

def _extract_live_football(fixture: str) -> tuple[list[dict], list[ProviderEvidenceRecord]]:
    rows, _errors = fetcher_500._parse_live(fixture, "a", "football")
    raw_trs = _live_tr_records(fixture, "a")
    index = _index_by_key(
        [(str(item["order"] or item["fid"]), item) for item in raw_trs]
    )
    # 位置兜底：仅当「解析行数 == 原始 <tr> 数」时才允许按出现顺序配对，
    # 这样既覆盖旧布局（order 属性缺失、编号取自单元格），又不会张冠李戴。
    positional = list(raw_trs) if len(raw_trs) == len(rows) else None

    records: list[ProviderEvidenceRecord] = []
    for index_in_rows, row in enumerate(rows):
        suffix = str(row.get("id") or "").rsplit("-", 1)[-1]
        raw = _pop_by_key(index, suffix)
        notes: list[str] = []
        if raw is None and positional is not None:
            raw = positional[index_in_rows]
            notes.append("按出现顺序定位原始 <tr>（旧布局无 order 属性）。")
        if raw is None:
            notes.append("未能在夹具中定位该行的原始 <tr>；原始属性记为缺失。")
            fid = None
            order = ""
            attrs: dict[str, str] = {}
        else:
            fid = raw["fid"]
            order = raw["order"]
            attrs = raw["attrs"]
        competition_raw = order or None
        if competition_raw is None:
            notes.append("该行的 order 属性缺失；竞彩编号原始字段记为缺失。")
        # HTML id 属性：语义未证实 -> 只按 HTML_ELEMENT_ID 记录，绝不提升为身份。
        html_element_id = f"a{fid}" if fid is not None else None
        raw_attributes = tuple(
            sorted((k, v) for k, v in attrs.items() if isinstance(v, str))
        )
        records.append(
            _evidence_record(
                adapter="live_football",
                row_index=index_in_rows,
                row=row,
                derived_source_id=str(row.get("id") or "") or None,
                raw_provider_id=html_element_id,
                raw_provider_id_type=RawIdentifierType.HTML_ELEMENT_ID.value,
                competition_number_raw=competition_raw,
                html_element_id=html_element_id,
                raw_attributes=raw_attributes,
                notes=notes,
            )
        )
    return rows, records


def _extract_live_basketball(
    fixture: str,
) -> tuple[list[dict], list[ProviderEvidenceRecord]]:
    rows, _errors = fetcher_500._parse_lq(fixture)

    raw_rows = fetcher_500._js_array(fixture, "matchList") or []
    candidates: list[tuple[str, dict[str, str]]] = []
    for raw in raw_rows:
        if not isinstance(raw, list):
            continue
        mid = str(raw[fetcher_500._LQ_ID]) if fetcher_500._LQ_ID < len(raw) else ""
        no = str(raw[fetcher_500._LQ_NO]) if fetcher_500._LQ_NO < len(raw) else ""
        candidates.append((no.strip() or mid, {"mid": mid, "no": no.strip()}))
    index = _index_by_key(candidates)

    records: list[ProviderEvidenceRecord] = []
    for index_in_rows, row in enumerate(rows):
        suffix = str(row.get("id") or "").rsplit("-", 1)[-1]
        raw = _pop_by_key(index, suffix)
        notes: list[str] = []
        if raw is None:
            notes.append("未能在夹具 matchList 中定位该行；provider 比赛号记为缺失。")
            mid = None
            competition_raw = None
        else:
            mid = raw["mid"] or None
            competition_raw = raw["no"] or None
        if competition_raw is None:
            notes.append("该行没有竞彩编号原始字段。")
        records.append(
            _evidence_record(
                adapter="live_basketball",
                row_index=index_in_rows,
                row=row,
                derived_source_id=str(row.get("id") or "") or None,
                raw_provider_id=mid,
                raw_provider_id_type=RawIdentifierType.PROVIDER_MATCH_ID.value,
                competition_number_raw=competition_raw,
                html_element_id=None,
                raw_attributes=tuple(
                    sorted(
                        (
                            ("_LQ_ID", str(mid or "")),
                            ("_LQ_NO", str(competition_raw or "")),
                        )
                    )
                ),
                notes=notes,
            )
        )
    return rows, records


def _parse_market_documents(
    documents: Mapping[str, str], *, sport: str
) -> tuple[list[dict], list[dict[str, str]]]:
    """
    **只读**复现 ``fetch_jczq_xml`` 的解析语义（不抓取、不写诊断、无副作用）。

    返回 ``(normalized_rows, raw_index)``；``raw_index[i]`` 给出第 ``i`` 行的
    XML ``<m id>``（provider 比赛号）与 ``<m matchnum>``（竞彩编号）。
    """
    cfg = fetcher_500.PLAY_CODES.get(sport)
    if not cfg:
        raise ValueError(f"unsupported market sport for evidence extraction: {sport!r}")

    by_id: dict[str, dict] = {}
    raw_by_id: dict[str, dict[str, str]] = {}

    for play in cfg["plays"]:
        xml = documents.get(play)
        if xml is None:
            continue
        for attrs, rows in fetcher_500._iter_xml_matches(xml):
            mid = attrs.get("id")
            if not mid:
                continue
            latest = rows[0] if rows else {}
            entry = by_id.get(mid)
            if entry is None:
                if not attrs.get("home") or not attrs.get("away"):
                    continue
                entry = {
                    "id": f"500j-{attrs.get('date', '')}-{attrs.get('matchnum', mid)}",
                    "sport": sport,
                    "league": attrs.get("league", "竞彩"),
                    "round": attrs.get("matchnum", ""),
                    "date": attrs.get("date", ""),
                    "time": "00:00",
                    "kickoff_time_known": False,
                    "status": "upcoming",
                    "home": attrs.get("home", "").strip(),
                    "away": attrs.get("away", "").strip(),
                    "home_rank": None,
                    "away_rank": None,
                    "score": None,
                    "odds": {},
                    "jczq_no": attrs.get("matchnum", ""),
                }
                by_id[mid] = entry
                raw_by_id[mid] = {
                    "provider_match_id": str(mid),
                    "competition_number": str(attrs.get("matchnum", "") or ""),
                }

            odds = entry["odds"]
            if play in ("spf", "nspf", "sf"):
                if odds.get("home_win") is None:
                    odds["home_win"] = fetcher_500._float(latest.get("win"))
                    odds["away_win"] = fetcher_500._float(latest.get("lost"))
                    if "draw" in latest:
                        odds["draw"] = fetcher_500._float(latest.get("draw"))
                    odds["matchnum"] = attrs.get("matchnum", "")
            elif play == "dxf":
                if odds.get("total_line") is None:
                    odds["total_line"] = fetcher_500._float(latest.get("yszf"))
                    odds["over"] = fetcher_500._float(latest.get("big"))
                    odds["under"] = fetcher_500._float(latest.get("small"))

    out: list[dict] = []
    raw_index: list[dict[str, str]] = []
    for mid, entry in by_id.items():
        odds = {k: v for k, v in (entry.get("odds") or {}).items() if v is not None}
        entry["odds"] = odds or None
        out.append(entry)
        raw_index.append(raw_by_id[mid])
    return out, raw_index


def _extract_market_xml(
    fixture: Any, *, sport: str
) -> tuple[list[dict], list[ProviderEvidenceRecord]]:
    if isinstance(fixture, Mapping):
        documents = {str(k): str(v) for k, v in fixture.items()}
    else:
        cfg = fetcher_500.PLAY_CODES.get(sport) or {"plays": []}
        first_play = cfg["plays"][0] if cfg["plays"] else "spf"
        documents = {first_play: str(fixture)}

    rows, raw_index = _parse_market_documents(documents, sport=sport)

    records: list[ProviderEvidenceRecord] = []
    for index_in_rows, row in enumerate(rows):
        raw = raw_index[index_in_rows]
        competition_raw = raw.get("competition_number") or None
        notes: list[str] = []
        if competition_raw is None:
            notes.append("该行没有竞彩编号原始字段。")
        records.append(
            _evidence_record(
                adapter="market_xml",
                row_index=index_in_rows,
                row=row,
                derived_source_id=str(row.get("id") or "") or None,
                raw_provider_id=raw.get("provider_match_id") or None,
                raw_provider_id_type=RawIdentifierType.PROVIDER_MATCH_ID.value,
                competition_number_raw=competition_raw,
                html_element_id=None,
                raw_attributes=(
                    ("m.id", str(raw.get("provider_match_id") or "")),
                    ("m.matchnum", str(competition_raw or "")),
                ),
                notes=notes,
            )
        )
    return rows, records


def _wanchang_tr_keys(html: str) -> list[tuple[str | None, tuple[str, str, str]]]:
    """
    按文档顺序给出 ``_parse_wanchang`` 会接受的 ``<tr>``：``(tr_id, (date, home, away))``。

    复用既有 ``_strip_tags`` / ``_parse_team_cell`` 助手，只读取、不修改输入。
    """
    year = fetcher_500.datetime.now(fetcher_500.BEIJING_TZ).year
    out: list[tuple[str | None, tuple[str, str, str]]] = []
    for raw_tr in re.findall(r"<tr([^>]*)>(.*?)</tr>", html, re.S):
        attr_string, body = raw_tr
        tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", body, re.S)
        if len(tds) < 8:
            continue
        cells = [fetcher_500._strip_tags(td) for td in tds]
        match = re.match(r"(\d{2})-(\d{2})\s+(\d{2}:\d{2})", cells[2])
        if not match:
            continue
        mdate = f"{year}-{match.group(1)}-{match.group(2)}"
        try:
            parsed = fetcher_500.datetime.strptime(mdate, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (
            parsed - fetcher_500.datetime.now(fetcher_500.BEIJING_TZ).date()
        ).days > 180:
            mdate = f"{year - 1}-{match.group(1)}-{match.group(2)}"
        home, _home_rank = fetcher_500._parse_team_cell(tds[4], "home")
        away, _away_rank = fetcher_500._parse_team_cell(tds[6], "away")
        if not home or not away:
            continue
        tr_id = _attributes_of(attr_string).get("id") or None
        out.append((tr_id, (mdate, home, away)))
    return out


def _extract_finished_feed(
    fixture: str,
) -> tuple[list[dict], list[ProviderEvidenceRecord]]:
    rows = fetcher_500._parse_wanchang(fixture)
    keys = _wanchang_tr_keys(fixture)
    index = _index_by_key(
        [((key[0], key[1], key[2]), tr_id) for tr_id, key in keys]
    )

    records: list[ProviderEvidenceRecord] = []
    for index_in_rows, row in enumerate(rows):
        key = (
            str(row.get("date") or ""),
            str(row.get("home") or ""),
            str(row.get("away") or ""),
        )
        tr_id = _pop_by_key(index, key)
        notes = [_POSITION_NOTE]
        if tr_id is None:
            notes.append("夹具中的 <tr> 未携带 id 属性；HTML 元素 id 记为缺失。")
        records.append(
            _evidence_record(
                adapter="finished_feed",
                row_index=index_in_rows,
                row=row,
                derived_source_id=str(row.get("id") or "") or None,
                # HTML id 属性绝不假定为稳定比赛标识：按 HTML_ELEMENT_ID + UNKNOWN 记录。
                raw_provider_id=tr_id,
                raw_provider_id_type=RawIdentifierType.HTML_ELEMENT_ID.value,
                competition_number_raw=None,
                html_element_id=tr_id,
                raw_attributes=(("tr.id", str(tr_id or "")),),
                notes=notes + ["完场 feed 不提供竞彩编号原始字段。"],
            )
        )
    return rows, records


def _extract_html_fallback(
    fixture: str,
) -> tuple[list[dict], list[ProviderEvidenceRecord]]:
    rows = fetcher_500._parse_jczq_html(fixture)

    candidates: list[tuple[str, dict[str, str]]] = []
    for block in re.split(r"<tr\s", fixture):
        home_m = re.search(r'homesxname="([^"]+)"', block)
        away_m = re.search(r'awaysxname="([^"]+)"', block)
        if not (home_m and away_m):
            continue
        attrs = _attributes_of(block.split(">", 1)[0])
        candidates.append(
            (
                f"{home_m.group(1).strip()}|{away_m.group(1).strip()}",
                {"data_mid": attrs.get("data-mid", ""), "id": attrs.get("id", "")},
            )
        )
    index = _index_by_key(candidates)

    records: list[ProviderEvidenceRecord] = []
    for index_in_rows, row in enumerate(rows):
        key = f"{row.get('home')}|{row.get('away')}"
        raw = _pop_by_key(index, key)
        notes = [_POSITION_NOTE]
        data_mid = ""
        element_id = ""
        if raw is None:
            notes.append("未能在夹具中定位该行的 <tr>；原始属性记为缺失。")
        else:
            data_mid = raw.get("data_mid") or ""
            element_id = raw.get("id") or ""
        if data_mid:
            notes.append(
                "data-mid 取自渲染页属性；其语义在仓库内未经验证，仅供复核。"
            )
        else:
            notes.append("渲染页未提供 data-mid；provider 比赛号记为缺失。")
        records.append(
            _evidence_record(
                adapter="html_fallback",
                row_index=index_in_rows,
                row=row,
                derived_source_id=str(row.get("id") or "") or None,
                raw_provider_id=(data_mid or None),
                raw_provider_id_type=RawIdentifierType.PROVIDER_MATCH_ID.value,
                competition_number_raw=None,
                html_element_id=(element_id or None),
                raw_attributes=(
                    ("data-mid", data_mid),
                    ("tr.id", element_id),
                ),
                notes=notes + ["HTML 兜底路径不提供竞彩编号原始字段。"],
            )
        )
    return rows, records


_EXTRACTORS = {
    "live_football": _extract_live_football,
    "live_basketball": _extract_live_basketball,
    "finished_feed": _extract_finished_feed,
    "html_fallback": _extract_html_fallback,
}


# =============================================================================
# 公开入口
# =============================================================================

def extract_source_evidence(
    fixture: Any,
    *,
    adapter: str,
    sport: str | None = None,
    canonical_match_id_by_row: Mapping[int, str] | None = None,
) -> SourceEvidenceExtraction:
    """
    **可选启用**地从源夹具抽取 provider 原始标识符证据（只读、离线、无副作用）。

    ``fixture`` 为已经取得的源文本：HTML 字符串，或 market XML 的
    ``{play: xml}`` 映射（也接受单个 XML 字符串，按该运动的首个玩法处理）。

    返回 ``SourceEvidenceExtraction``；它可被解包为
    ``(normalized_rows, provenance_observations)``。

    本函数绝不：联网、修改夹具或比赛行、改变既有解析器返回值、写任何文件、
    产生映射提案或账本条目。
    """
    if adapter not in PROVIDER_EVIDENCE_ADAPTERS:
        raise ValueError(
            f"unsupported adapter for provider evidence extraction: {adapter!r}; "
            f"expected one of {list(PROVIDER_EVIDENCE_ADAPTERS)}"
        )
    if not isinstance(fixture, (str, Mapping)):
        raise TypeError("fixture must be a source text or a {play: xml} mapping")

    if adapter == "market_xml":
        resolved_sport = sport or "football"
        rows, records = _extract_market_xml(fixture, sport=resolved_sport)
    else:
        if not isinstance(fixture, str):
            raise TypeError(f"adapter {adapter!r} requires a text fixture")
        rows, records = _EXTRACTORS[adapter](fixture)

    return _build(
        adapter=adapter,
        fixture=fixture,
        rows=rows,
        records=records,
        canonical_match_id_by_row=canonical_match_id_by_row,
    )


def extract_rows_and_observations(
    fixture: Any,
    *,
    adapter: str,
    sport: str | None = None,
    canonical_match_id_by_row: Mapping[int, str] | None = None,
) -> tuple[list[dict], list[SourceObservation]]:
    """
    便捷入口：返回 ``(normalized_rows, provenance_observations)`` 二元组。

    既有调用方仍可使用 ``utils.fetcher_500`` 的原始函数，其行为与输出形状完全不变。
    """
    extraction = extract_source_evidence(
        fixture,
        adapter=adapter,
        sport=sport,
        canonical_match_id_by_row=canonical_match_id_by_row,
    )
    return list(extraction.normalized_rows), list(extraction.provenance_observations)


def sidecar_json(extraction: SourceEvidenceExtraction) -> str:
    """侧车的规范 JSON（便于审计比对；本函数不写任何文件）。"""
    return canonical_json(extraction.sidecar.as_dict())
