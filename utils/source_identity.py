"""
源身份溯源模型（Source Identity Provenance）
==========================================
本模块把「数据源实际提供了什么」与「解析器推导出了什么」严格分开，并以**只读、
确定性、可审计**的形式记录下来。

核心不变量
----------
- **一次源观测不是绑定或改写 Canonical 身份的依据。** 本模块只描述证据，不做决策。
- 三个标识符必须始终分开存放，绝不合并成一个字段：

  ==================  ==========================================================
  ``raw_provider_id``  数据源**实际提供**的标识符；源未提供时记为缺失（绝不用其他
                       字段合成）。
  ``derived_source_id`` 现有解析器**推导**出的源标识符（如 ``500l-{date}-{order}``）。
  ``canonical_match_id`` 已持久化的 Canonical 比赛 ID；只能来自既有的 Canonical
                       记录，本模块绝不生成。
  ==================  ==========================================================

- 溯源必须说明证据**建立了什么**，以及**没有建立什么**（``evidence_completeness``
  与 ``notes``）。
- 所有函数都是纯函数或只读的：不修改传入的源字典，不访问网络，不写任何文件。

标识符稳定性分类
----------------
``PROVIDER_SUPPLIED_UNVERIFIED``
    数据源确实提供了该标识符，但本仓库**没有**关于其全局唯一性/跨刷新稳定性的
    独立文档化保证。未经独立文档证实，不得等同于「全局稳定」。
``COMPETITION_NUMBER_SCOPED``
    该标识符由竞彩赛事编号构成，其身份作用域是 ``(sport, date, normalized_number)``。
``DERIVED_FROM_PROVIDER_FIELDS``
    由若干数据源字段推导而成，但无法确认其中嵌入的是哪个字段（因此也无法确认其
    稳定性）。
``ROW_POSITION_UNSTABLE``
    由**响应中的行位置**推导而成，跨刷新不稳定，绝不构成持久身份。
``SYNTHETIC_LOCAL``
    本地合成标识符，不对应任何数据源值。
``UNKNOWN``
    无法分类。

Provider ID 唯一性作用域（本模块新增，见 §Provider ID uniqueness scope）
------------------------------------------------------------------------
「数据源提供了某个标识符」与「该标识符在某个作用域内唯一、且跨刷新稳定」是**三个
互不等价**的保证：

======================  ==========================================================
provider-supplied       数据源确实给出了这个值（可观测事实）。
provider-unique         在**显式声明的作用域**内唯一（需要独立文档化的支持证据）。
provider-stable         跨刷新保持同一含义（同样需要独立证据）。
======================  ==========================================================

本仓库目前**没有**任何一份独立文档化的 provider 唯一性保证，因此
``VERIFIED_PROVIDER_ID_SCOPES`` 默认为空 —— 一切 provider 标识符一律按
``UNVERIFIED`` 处理，只能被**观测 / 复核**，绝不自动成为权威映射证据
（活动绑定 fail closed）。身份键取值显式携带唯一性作用域，因此
「同一 provider 数字在不同日期 / 不同运动」不再碰撞成同一个绑定身份。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from utils.match_identity import (
    competition_event_key,
    competition_number_for_match,
    normalize_competition_number,
)

#: 溯源 schema 版本。任何字段语义变化都必须提升该版本。
#:
#: ``2.0.0``：``SourceObservation`` 新增 ``raw_provider_id_uniqueness_scope``，
#: 语义指纹随之变化；旧 ``1.0.0`` 记录保持只读，绝不静默重新解释。
PROVENANCE_SCHEMA_VERSION = "2.0.0"

#: 历史溯源 schema 版本（只读；绝不静默迁移）。
LEGACY_PROVENANCE_SCHEMA_VERSION = "1.0.0"

#: 缺失证据的显式标记。源未提供即记为缺失，绝不用其他字段合成。
MISSING = "MISSING"


# ---------------------------------------------------------------------------
# 枚举：稳定性分类与原始标识符类型
# ---------------------------------------------------------------------------

class IdentifierStability(str, Enum):
    """源标识符的稳定性分类（见模块 docstring）。"""

    PROVIDER_SUPPLIED_UNVERIFIED = "PROVIDER_SUPPLIED_UNVERIFIED"
    COMPETITION_NUMBER_SCOPED = "COMPETITION_NUMBER_SCOPED"
    DERIVED_FROM_PROVIDER_FIELDS = "DERIVED_FROM_PROVIDER_FIELDS"
    ROW_POSITION_UNSTABLE = "ROW_POSITION_UNSTABLE"
    SYNTHETIC_LOCAL = "SYNTHETIC_LOCAL"
    UNKNOWN = "UNKNOWN"


class RawIdentifierType(str, Enum):
    """源标识符中嵌入值的类型维度。"""

    PROVIDER_MATCH_ID = "PROVIDER_MATCH_ID"
    COMPETITION_NUMBER = "COMPETITION_NUMBER"
    #: HTML 元素 ``id`` 属性。**不假定**它是稳定的 provider 比赛标识符：
    #: 其语义在仓库内无法证实，因此一律按未验证处理。
    HTML_ELEMENT_ID = "HTML_ELEMENT_ID"
    ROW_INDEX = "ROW_INDEX"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class ProviderIdUniquenessScope(str, Enum):
    """
    provider 标识符的**唯一性作用域**声明。

    ``VERIFIED_SCOPED``
        存在独立文档化的保证：该标识符在显式声明的作用域内唯一（当前仓库为空集）。
    ``UNVERIFIED``
        数据源提供了该值，但**没有**独立文档化的唯一性/跨刷新稳定性保证。
    ``UNKNOWN``
        无法判定其唯一性作用域 —— 与 ``UNVERIFIED`` 一样，绝不授权活动绑定。
    ``NOT_APPLICABLE``
        该身份键不涉及 provider 标识符（例如竞彩编号作用域身份）。
    """

    VERIFIED_SCOPED = "VERIFIED_SCOPED"
    UNVERIFIED = "UNVERIFIED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


#: 唯一性作用域中「已获独立文档支持」的那一档。
VERIFIED_UNIQUENESS_SCOPE = ProviderIdUniquenessScope.VERIFIED_SCOPED.value


@dataclass(frozen=True)
class ProviderIdScopeDeclaration:
    """
    一份**独立文档化**的 provider 标识符唯一性作用域声明。

    ``documentation_reference`` 必须指向仓库内可复核的证据（文档 / 契约 / 外部规范
    的固定引用）。没有该引用，任何声明都不得进入 ``VERIFIED_PROVIDER_ID_SCOPES``。
    """

    adapter: str
    provider_namespace: str
    sport: str
    raw_identifier_type: str
    uniqueness_scope: str
    documentation_reference: str

    def covers(self, *, sport: str, raw_identifier_type: str) -> bool:
        """该声明是否覆盖给定运动 / 原始标识符类型（``*`` 表示任意）。"""
        if self.sport not in ("*", sport):
            return False
        if self.raw_identifier_type not in ("*", raw_identifier_type):
            return False
        return True


#: **已独立文档化**的 provider 唯一性作用域注册表，键为 ``(adapter, namespace)``。
#:
#: 刻意**为空**：本仓库目前没有对任何 provider 标识符的全局唯一性 / 跨刷新稳定性
#: 作出独立文档化保证。因此一切 provider 标识符都按 ``UNVERIFIED`` / ``UNKNOWN``
#: 处理，绝不自动成为权威映射证据。新增条目必须同时给出可复核的文档引用。
VERIFIED_PROVIDER_ID_SCOPES: dict[tuple[str, str], ProviderIdScopeDeclaration] = {}


def resolve_provider_id_uniqueness_scope(
    *,
    adapter: str,
    provider_namespace: str,
    sport: str,
    raw_identifier_type: str,
) -> str:
    """
    解析 provider 标识符的唯一性作用域（只读，绝不推断未文档化的保证）。

    只有在注册表中存在**覆盖该 (adapter, namespace, sport, raw_type)** 的独立
    文档化声明时，才返回 ``VERIFIED_SCOPED``；否则返回 ``UNKNOWN``。

    刻意不依据 ID 的形状 / 长度 / 是否为数字来推断唯一性。
    """
    declaration = VERIFIED_PROVIDER_ID_SCOPES.get((adapter, provider_namespace))
    if declaration is None:
        return ProviderIdUniquenessScope.UNKNOWN.value
    if not declaration.covers(sport=sport, raw_identifier_type=raw_identifier_type):
        return ProviderIdUniquenessScope.UNKNOWN.value
    return declaration.uniqueness_scope


class ObservationCategory(str, Enum):
    """观测来源类别。"""

    LIVE_FOOTBALL = "LIVE_FOOTBALL"
    LIVE_BASKETBALL = "LIVE_BASKETBALL"
    MARKET_XML = "MARKET_XML"
    FINISHED_FEED = "FINISHED_FEED"
    HTML_FALLBACK = "HTML_FALLBACK"
    SYNTHETIC = "SYNTHETIC"


# ---------------------------------------------------------------------------
# 源身份清单（Source Identity Inventory）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceIdPattern:
    """
    一条源标识符构造路径的声明式描述（审计清单条目）。

    这是对 ``utils/fetcher_500.py`` 中实际构造点的**文档化清单**；
    对应解析器由 ``tests/test_source_identity_provenance.py`` 用合成夹具逐一验证。
    """

    adapter: str
    observation_category: str
    namespace: str
    pattern: str
    embedded_raw_value: str
    embedded_raw_type: str
    competition_number_field: str | None
    raw_provider_id_preserved: bool
    stability: str
    risk: str


#: 完整的源身份清单。新增任何源 ID 构造路径都必须在此登记。
SOURCE_ID_PATTERNS: tuple[SourceIdPattern, ...] = (
    SourceIdPattern(
        adapter="live_football",
        observation_category=ObservationCategory.LIVE_FOOTBALL.value,
        namespace="500l",
        pattern="500l-{date}-{order_or_fid}",
        embedded_raw_value="attrs['order']（竞彩编号） 或 <tr id> 的 provider 行号（fid）",
        embedded_raw_type="COMPETITION_NUMBER 或 PROVIDER_MATCH_ID（两者混用，行内无法区分）",
        competition_number_field="jczq_no",
        raw_provider_id_preserved=False,
        stability=IdentifierStability.DERIVED_FROM_PROVIDER_FIELDS.value,
        risk=(
            "同一 ID 形状承载两种语义不同的原始值；规范化的比赛行未保留 fid，"
            "因此事后无法判定某个 500l ID 嵌入的是竞彩编号还是 provider 行号。"
        ),
    ),
    SourceIdPattern(
        adapter="live_basketball",
        observation_category=ObservationCategory.LIVE_BASKETBALL.value,
        namespace="500lq",
        pattern="500lq-{date}-{number_or_mid}",
        embedded_raw_value="matchList[_LQ_NO]（竞彩编号） 或 matchList[_LQ_ID]（provider 比赛号）",
        embedded_raw_type="COMPETITION_NUMBER 或 PROVIDER_MATCH_ID（两者混用）",
        competition_number_field="jczq_no",
        raw_provider_id_preserved=False,
        stability=IdentifierStability.DERIVED_FROM_PROVIDER_FIELDS.value,
        risk=(
            "运动作用域通过 500lq 前缀保留，但规范化的比赛行未保留 provider 比赛号，"
            "同一 ID 形状承载两种语义。"
        ),
    ),
    SourceIdPattern(
        adapter="market_xml",
        observation_category=ObservationCategory.MARKET_XML.value,
        namespace="500j",
        pattern="500j-{date}-{matchnum_or_mid}",
        embedded_raw_value="XML <m matchnum>（竞彩编号） 或 XML <m id>（provider 比赛号）",
        embedded_raw_type="COMPETITION_NUMBER 或 PROVIDER_MATCH_ID（两者混用）",
        competition_number_field="jczq_no",
        raw_provider_id_preserved=False,
        stability=IdentifierStability.DERIVED_FROM_PROVIDER_FIELDS.value,
        risk=(
            "已确认风险：足球（jczq）与篮球（jclq）**共用 500j 前缀**，同一日期下"
            "两侧的竞彩编号序列相互独立，因此同一 500j 字符串可能代表两个不同运动的"
            "不同事件。既有摄取预检对该情形 fail closed。"
        ),
    ),
    SourceIdPattern(
        adapter="finished_feed",
        observation_category=ObservationCategory.FINISHED_FEED.value,
        namespace="500w",
        pattern="500w-{date}-{row_index}",
        embedded_raw_value="响应中的行序号 len(matches)",
        embedded_raw_type="ROW_INDEX",
        competition_number_field=None,
        raw_provider_id_preserved=False,
        stability=IdentifierStability.ROW_POSITION_UNSTABLE.value,
        risk=(
            "已确认风险：行序号取决于响应顺序，两次刷新可能把同一个 500w ID 分配给"
            "不同比赛。绝不构成持久身份。"
        ),
    ),
    SourceIdPattern(
        adapter="html_fallback",
        observation_category=ObservationCategory.HTML_FALLBACK.value,
        namespace="500j",
        pattern="500j-{today}-{row_index}",
        embedded_raw_value="渲染后 HTML 中的行序号 len(matches)",
        embedded_raw_type="ROW_INDEX",
        competition_number_field=None,
        raw_provider_id_preserved=False,
        stability=IdentifierStability.ROW_POSITION_UNSTABLE.value,
        risk=(
            "已确认风险：浏览器兜底路径同样以**行位置**构造 500j ID，与 market_xml 的"
            "竞彩编号语义完全不同却共享同一前缀，跨刷新不稳定。"
        ),
    ),
)


def pattern_for_adapter(adapter: str) -> SourceIdPattern | None:
    """按适配器名取清单条目（只读）。"""
    for pattern in SOURCE_ID_PATTERNS:
        if pattern.adapter == adapter:
            return pattern
    return None


# ---------------------------------------------------------------------------
# Provider 证据清单（Provider Evidence Inventory）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderEvidenceInventoryEntry:
    """
    一个源解析路径的**原始标识符证据清单**（只读审计记录）。

    与 ``SourceIdPattern`` 的分工：后者描述**推导出的**源 ID 构造路径，
    本清单描述**原始表示中实际存在什么标识符**，以及它当前是否被丢弃。
    """

    adapter: str
    parser: str
    input_field_or_attribute: str
    observed_meaning: str
    evidence_availability: str
    stability_limitation: str
    current_data_loss: str
    safe_preservation_mechanism: str


#: 五个已审计的源解析路径的原始标识符证据清单。
#:
#: 结论（**全部经合成夹具与真实解析器核对**）：
#: - live football / live basketball / market XML 的原始表示中**确实存在** provider
#:   行标识符与竞彩编号，但规范化比赛行**两者都不保留**（除竞彩编号有时落到
#:   ``jczq_no``）。
#: - finished feed 的规范化行**既无** provider 标识符**也无**竞彩编号，唯一可用的
#:   值是行序号；解析器甚至不读取 ``<tr>`` 上可能存在的 ``id``。
#: - HTML 兜底路径同样只有行序号；渲染页可能携带 ``data-mid``，但
#:   ``_parse_jczq_html`` 从不读取它。
#: 任何一条都**不**假定 provider 标识符全局唯一。
PROVIDER_EVIDENCE_INVENTORY: tuple[ProviderEvidenceInventoryEntry, ...] = (
    ProviderEvidenceInventoryEntry(
        adapter="live_football",
        parser="utils.fetcher_500._parse_live",
        input_field_or_attribute='<tr id="a{digits}">（provider 行号）与 <tr order="...">（竞彩编号）',
        observed_meaning=(
            "tr id 的数字部分是 provider 自己的行/比赛标识；order 属性是竞彩编号。"
            "二者语义不同，却在同一 ID 形状中混用。"
        ),
        evidence_availability="源确实提供了两种原始值；解析器可读到（fid / attrs['order']）。",
        stability_limitation=(
            "无任何独立文档证明 tr id 跨刷新稳定；order 只在当日竞彩编号作用域内有效。"
        ),
        current_data_loss=(
            "规范化行只保留派生 id（order 或 fid 二者其一），另一个原始值被丢弃；"
            "事后无法判定某个 500l ID 嵌入的是竞彩编号还是 provider 行号。"
        ),
        safe_preservation_mechanism=(
            "可选启用的侧车抽取（utils/provider_evidence.py）在归一化之前分别捕获 "
            "tr id 与 order，并显式标注唯一性作用域为 UNKNOWN。"
        ),
    ),
    ProviderEvidenceInventoryEntry(
        adapter="live_basketball",
        parser="utils.fetcher_500._parse_lq",
        input_field_or_attribute="matchList[_LQ_ID]（provider 比赛号）与 matchList[_LQ_NO]（竞彩编号）",
        observed_meaning="两个不同下标承载两种不同语义：provider 比赛号与竞彩编号。",
        evidence_availability="内联 JSON 数组中两者都存在且可读。",
        stability_limitation="无独立文档证明 provider 比赛号跨刷新稳定。",
        current_data_loss="规范化行只保留 _LQ_NO 或 _LQ_ID 之一（另一个被丢弃）。",
        safe_preservation_mechanism=(
            "侧车抽取分别捕获 _LQ_ID 与 _LQ_NO；provider 比赛号按 UNKNOWN 作用域记录。"
        ),
    ),
    ProviderEvidenceInventoryEntry(
        adapter="market_xml",
        parser="utils.fetcher_500.fetch_jczq_xml",
        input_field_or_attribute='XML <m id="...">（provider 比赛号）与 <m matchnum="...">（竞彩编号）',
        observed_meaning=(
            "XML id 是聚合键（provider 比赛号），matchnum 是竞彩编号；足球与篮球共用 "
            "500j 前缀但编号序列相互独立。"
        ),
        evidence_availability="两种属性都存在；解析器用 id 聚合，用 matchnum 派生 ID。",
        stability_limitation=(
            "无独立文档证明 XML id 跨刷新稳定；matchnum 仅在 (sport, date) 内有效。"
        ),
        current_data_loss="规范化行不保留 XML id；跨运动共用 500j 前缀的歧义被保留下来。",
        safe_preservation_mechanism=(
            "侧车抽取保留 XML id 与 matchnum，并显式标注 provider 比赛号作用域为 UNKNOWN。"
        ),
    ),
    ProviderEvidenceInventoryEntry(
        adapter="finished_feed",
        parser="utils.fetcher_500._parse_wanchang",
        input_field_or_attribute="响应中的行序号 len(matches)；<tr> 上可能存在的 id 属性未被读取",
        observed_meaning="唯一的可用标识符是响应中的行位置。",
        evidence_availability="行位置总是存在；provider 标识符与竞彩编号**均不可用**。",
        stability_limitation="行位置取决于响应顺序，两次刷新可能把同一 500w ID 分配给不同比赛。",
        current_data_loss=(
            "解析器不读取 <tr> 的 id 属性；即便源提供了元素 id，也被直接丢弃且语义未证实。"
        ),
        safe_preservation_mechanism=(
            "侧车抽取仅在源确实给出 <tr id> 时按 HTML_ELEMENT_ID 记录，并标注语义未证实；"
            "绝不把行位置提升为稳定 provider 标识符。"
        ),
    ),
    ProviderEvidenceInventoryEntry(
        adapter="html_fallback",
        parser="utils.fetcher_500._parse_jczq_html",
        input_field_or_attribute="渲染后 HTML 的行序号 len(matches)；data-mid 属性未被读取",
        observed_meaning="唯一可用标识符是渲染后 HTML 中的行位置。",
        evidence_availability=(
            "行位置总是存在；渲染页可能携带 data-mid（选择器 tr[data-mid] 的存在暗示），"
            "但解析器从不读取它，仓库内也没有对其语义的独立验证。"
        ),
        stability_limitation="行位置跨刷新不稳定，且与 market_xml 共享 500j 前缀却语义完全不同。",
        current_data_loss="data-mid（若存在）被丢弃；无法事后恢复。",
        safe_preservation_mechanism=(
            "侧车抽取在源确实给出 data-mid 时按 PROVIDER_MATCH_ID + UNKNOWN 作用域记录，"
            "仅用于复核，绝不用于活动绑定。"
        ),
    ),
)


def inventory_for_adapter(adapter: str) -> ProviderEvidenceInventoryEntry | None:
    """按适配器名取原始证据清单条目（只读）。"""
    for entry in PROVIDER_EVIDENCE_INVENTORY:
        if entry.adapter == adapter:
            return entry
    return None


# ---------------------------------------------------------------------------
# 确定性序列化与哈希（证据完整性）
# ---------------------------------------------------------------------------

def canonical_json(payload: Any) -> str:
    """稳定 JSON 序列化：键排序、UTF-8、无多余空白。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(payload: Any) -> str:
    """任意 JSON 结构的确定性 SHA-256 指纹。"""
    return sha256_text(canonical_json(payload))


# ---------------------------------------------------------------------------
# 源身份作用域
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceIdentityScope:
    """
    源身份的完整作用域维度。

    §5 要求：绝不可只把「原始数字竞彩编号」当作全局唯一事件键；一个有效的竞彩
    身份必须保留 ``(sport, date, normalized_competition_number)``，并且足球与
    篮球不得共享一个不安全的全局数字命名空间。
    """

    adapter: str
    sport: str
    event_date: str | None
    provider_namespace: str
    raw_identifier_type: str

    def as_key(self) -> tuple[str, str, str, str, str]:
        """作用域键（元组，可直接排序/比较）。"""
        return (
            self.adapter,
            self.sport,
            self.event_date or MISSING,
            self.provider_namespace,
            self.raw_identifier_type,
        )

    def as_dict(self) -> dict:
        return {
            "adapter": self.adapter,
            "sport": self.sport,
            "event_date": self.event_date,
            "provider_namespace": self.provider_namespace,
            "raw_identifier_type": self.raw_identifier_type,
        }


def competition_identity_key(
    sport: str | None, event_date: str | None, normalized_number: str | None
) -> str | None:
    """
    竞彩身份键：``sport|date|jczq|normalized_number``。

    与既有摄取规划器 ``utils.match_identity.competition_event_key`` 语义**完全一致**
    （本函数只是把三段显式传入，便于在没有比赛字典时使用）。
    """
    if not sport or not event_date or normalized_number is None:
        return None
    return f"{sport}|{event_date}|jczq|{normalized_number}"


# ---------------------------------------------------------------------------
# 作用域身份键（ScopedSourceIdentityKey）
# ---------------------------------------------------------------------------

#: 绑定键契约版本。任何影响绑定键**含义**的变化都必须提升该版本。
#:
#: ``3.0.0``：``RAW_PROVIDER_ID`` 的取值显式携带 provider 唯一性作用域与
#: ``(sport, date)``，因此「同一 provider 数字在不同日期 / 不同运动」不再碰撞成
#: 同一个绑定身份；同时新增 ``UNVERIFIED_PROVIDER_ID``（永不充分）。
#: 旧 ``2.0.0`` 键的含义因此改变，**绝不**被静默重新解释。
IDENTITY_KEY_SCHEMA_VERSION = "3.0.0"

#: 旧契约的版本标记：缺失 ``identity_key_schema_version`` 即为旧契约（仅作用域元数据）。
LEGACY_IDENTITY_KEY_SCHEMA_VERSION = "1.0.0"

#: 已被本契约取代的版本（``2.0.0`` 的 RAW_PROVIDER_ID 取值不含 sport/date/作用域）。
#: 只读保留：其历史工件仍然可审计，但**不再**被解释为当前契约。
SUPERSEDED_IDENTITY_KEY_SCHEMA_VERSION = "2.0.0"


class IdentityKeyKind(str, Enum):
    """
    绑定键中承载的判别性身份证据种类。

    ``COMPETITION_NUMBER`` / ``RAW_PROVIDER_ID`` 是**充分**证据；
    ``UNVERIFIED_PROVIDER_ID`` / ``AMBIGUOUS_SHARED_SOURCE_ID`` / ``INSUFFICIENT``
    **不足以**授权活动绑定。
    """

    COMPETITION_NUMBER = "COMPETITION_NUMBER"
    RAW_PROVIDER_ID = "RAW_PROVIDER_ID"
    #: 数据源确实提供了标识符，但其唯一性作用域**未经验证**（或未知）。
    #: 该标识符仍被记录、仍可用于复核与冲突检测，但绝不授权活动绑定。
    UNVERIFIED_PROVIDER_ID = "UNVERIFIED_PROVIDER_ID"
    AMBIGUOUS_SHARED_SOURCE_ID = "AMBIGUOUS_SHARED_SOURCE_ID"
    INSUFFICIENT = "INSUFFICIENT"


#: 足以支撑一个活动绑定的身份证据种类。
#:
#: ``RAW_PROVIDER_ID`` 只在**唯一性作用域已验证**（``VERIFIED_SCOPED``）时才可达；
#: 未验证的 provider 标识符一律落到 ``UNVERIFIED_PROVIDER_ID``，因此这里的集合
#: 成员关系本身就是充分性判定的唯一权威。
SUFFICIENT_IDENTITY_KEY_KINDS: frozenset[str] = frozenset(
    {IdentityKeyKind.COMPETITION_NUMBER.value, IdentityKeyKind.RAW_PROVIDER_ID.value}
)


def identity_kind_is_sufficient(identity_kind: str) -> bool:
    """身份证据种类是否足以授权活动绑定（单一权威判定）。"""
    return identity_kind in SUFFICIENT_IDENTITY_KEY_KINDS


@dataclass(frozen=True)
class ScopedSourceIdentityKey:
    """
    **一个具体源身份声明**的判别性身份键。

    ``SourceIdentityScope`` 描述的是**域**（adapter / sport / date / namespace /
    raw_identifier_type 等宽泛元数据），本身并不识别某一场具体赛事。本类型在其之上
    给出**判别性身份证据**：

    - ``COMPETITION_NUMBER``：带 sport + date 作用域的规范化竞彩编号
      （``sport|date|jczq|number``）；绝不使用裸数字编号。
    - ``RAW_PROVIDER_ID``：**数据源确实提供**的 provider 标识符，且其唯一性作用域
      已有独立文档支持（``VERIFIED_SCOPED``）。取值显式携带 adapter / namespace /
      sport / date / 类型 / 唯一性作用域 / 原始值；绝不凭空合成。
    - ``UNVERIFIED_PROVIDER_ID``：数据源确实提供了标识符，但唯一性作用域**未经验证**
      （或未知）。只可观测 / 复核，**不得**授权活动绑定。
    - ``AMBIGUOUS_SHARED_SOURCE_ID``：一条被判定为歧义的共享源字符串
      （仅用于复核类提案，**不得**用于活动绑定）。
    - ``INSUFFICIENT``：缺乏受支持的稳定证据（行位置派生、含义不可确认等）
      -> fail closed，只能观测 / 复核，**不得**授权活动绑定。
    """

    scope: SourceIdentityScope
    identity_kind: str
    identity_value: str
    supporting_evidence_fingerprint: str
    identity_key_schema_version: str = IDENTITY_KEY_SCHEMA_VERSION
    provider_id_uniqueness_scope: str = ProviderIdUniquenessScope.NOT_APPLICABLE.value
    detail: tuple[tuple[str, str], ...] = ()

    # -- 充分性 -----------------------------------------------------------

    @property
    def is_sufficient(self) -> bool:
        """是否承载足以授权活动绑定的判别性身份证据。"""
        return identity_kind_is_sufficient(self.identity_kind)

    @property
    def requires_review(self) -> bool:
        return not self.is_sufficient

    # -- 序列化 -----------------------------------------------------------

    def as_dict(self) -> dict[str, str]:
        """
        绑定键载荷。

        刻意**只**包含版本、证据种类、判别性取值与 provider 唯一性作用域 ——
        宽泛的命名空间元数据不构成身份键，但「该 provider 标识符的唯一性是否
        经过验证」是身份含义的一部分，必须随键一起被签名。
        """
        return {
            "identity_key_schema_version": self.identity_key_schema_version,
            "identity_kind": self.identity_kind,
            "identity_value": self.identity_value,
            "provider_id_uniqueness_scope": self.provider_id_uniqueness_scope,
        }

    def source_key(self) -> str:
        """稳定的绑定键字符串（人类可读的规范 JSON）。"""
        return canonical_json(self.as_dict())

    def audit_payload(self) -> dict:
        """供审计报告使用的完整视图（含作用域与支撑证据）。"""
        payload = self.as_dict()
        payload.update(
            {
                "scope": self.scope.as_dict(),
                "supporting_evidence_fingerprint": self.supporting_evidence_fingerprint,
                "detail": dict(self.detail),
                "is_sufficient": self.is_sufficient,
            }
        )
        return payload

    def fingerprint(self) -> str:
        return fingerprint(self.as_dict())

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def for_observation(cls, observation: SourceObservation) -> ScopedSourceIdentityKey:
        """
        由一次源观测推导其判别性身份键（纯函数，只读）。

        优先级：带作用域的竞彩编号 > **唯一性已验证**的 provider 标识符 > 证据不足。

        provider 标识符若其唯一性作用域未经独立文档验证，则落到
        ``UNVERIFIED_PROVIDER_ID``（不充分）—— 仍被记录、仍参与冲突检测，
        但绝不授权活动绑定。
        """
        scope = observation.stability_scope

        if (
            observation.competition_number_normalized is not None
            and observation.sport
            and observation.event_date
        ):
            value = competition_identity_key(
                observation.sport,
                observation.event_date,
                observation.competition_number_normalized,
            )
            if value is not None:
                return cls(
                    scope=scope,
                    identity_kind=IdentityKeyKind.COMPETITION_NUMBER.value,
                    identity_value=value,
                    supporting_evidence_fingerprint=observation.semantic_fingerprint(),
                    provider_id_uniqueness_scope=(
                        ProviderIdUniquenessScope.NOT_APPLICABLE.value
                    ),
                    detail=(
                        ("sport", observation.sport),
                        ("event_date", observation.event_date),
                        (
                            "normalized_competition_number",
                            observation.competition_number_normalized,
                        ),
                    ),
                )

        if observation.raw_provider_id is not None:
            uniqueness_scope = observation.provider_id_uniqueness_scope
            if uniqueness_scope not in (
                ProviderIdUniquenessScope.VERIFIED_SCOPED.value,
                ProviderIdUniquenessScope.UNVERIFIED.value,
                ProviderIdUniquenessScope.UNKNOWN.value,
            ):
                # 缺失 / 非法作用域 -> 保守地按 UNKNOWN（= 不可验证）处理。
                uniqueness_scope = ProviderIdUniquenessScope.UNKNOWN.value
            # 取值显式携带 (sport, date)：同一 provider 数字在不同日期 / 不同运动
            # 不再碰撞成同一个绑定身份。
            value = "|".join(
                (
                    scope.adapter,
                    scope.provider_namespace,
                    scope.sport or MISSING,
                    scope.event_date or MISSING,
                    scope.raw_identifier_type,
                    uniqueness_scope,
                    observation.raw_provider_id,
                )
            )
            verified = uniqueness_scope == ProviderIdUniquenessScope.VERIFIED_SCOPED.value
            return cls(
                scope=scope,
                identity_kind=(
                    IdentityKeyKind.RAW_PROVIDER_ID.value
                    if verified
                    else IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value
                ),
                identity_value=value,
                supporting_evidence_fingerprint=observation.semantic_fingerprint(),
                provider_id_uniqueness_scope=uniqueness_scope,
                detail=(
                    ("raw_provider_id", observation.raw_provider_id),
                    ("provider_id_uniqueness_scope", uniqueness_scope),
                    ("sport", scope.sport or MISSING),
                    ("event_date", scope.event_date or MISSING),
                ),
            )

        # 缺乏受支持的稳定证据 -> fail closed（不得授权活动绑定）。
        return cls(
            scope=scope,
            identity_kind=IdentityKeyKind.INSUFFICIENT.value,
            identity_value=observation.derived_source_id or MISSING,
            supporting_evidence_fingerprint=observation.semantic_fingerprint(),
            provider_id_uniqueness_scope=ProviderIdUniquenessScope.NOT_APPLICABLE.value,
            detail=(
                ("derived_source_id", observation.derived_source_id or MISSING),
                ("identifier_stability", observation.identifier_stability),
            ),
        )

    @classmethod
    def for_ambiguous_source(
        cls,
        *,
        shared_source_id: str,
        member_scopes: Sequence[Mapping[str, Any]],
        conflict_category: str,
    ) -> ScopedSourceIdentityKey:
        """
        为一条被判定为**歧义**的共享源字符串构造身份键。

        这类键**永远不充分**，只用于复核类提案。
        """
        scopes = sorted({canonical_json(dict(scope)) for scope in member_scopes})
        first = dict(member_scopes[0]) if len(member_scopes) else {}
        scope = SourceIdentityScope(
            adapter=str(first.get("adapter", MISSING)),
            sport=str(first.get("sport", MISSING)),
            event_date=(str(first["event_date"]) if first.get("event_date") else None),
            provider_namespace=str(first.get("provider_namespace", MISSING)),
            raw_identifier_type=str(first.get("raw_identifier_type", MISSING)),
        )
        return cls(
            scope=scope,
            identity_kind=IdentityKeyKind.AMBIGUOUS_SHARED_SOURCE_ID.value,
            identity_value=shared_source_id or MISSING,
            supporting_evidence_fingerprint=fingerprint(
                {"shared_source_id": shared_source_id, "member_scopes": scopes}
            ),
            detail=(
                ("conflict_category", conflict_category),
                ("member_scopes", canonical_json(scopes)),
            ),
        )


def identity_key_for_observation(observation: SourceObservation) -> ScopedSourceIdentityKey:
    """便捷入口：由观测推导身份键（只读）。"""
    return ScopedSourceIdentityKey.for_observation(observation)


# ---------------------------------------------------------------------------
# 源观测（SourceObservation）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceObservation:
    """
    一次源观测的不可变、可审计记录。

    字段刻意冗余地保留「原始值」与「规范化值」，使得规范化过程事后可解释：
    ``raw_values`` 保存实际读到的原始字段，``*_normalized`` 保存规范化结果。
    """

    provenance_schema_version: str
    observation_id: str
    adapter: str
    observation_category: str
    source_namespace: str
    sport: str
    event_date: str | None
    raw_provider_id: str | None
    raw_provider_id_type: str
    derived_source_id: str | None
    canonical_match_id: str | None
    competition_number_raw: str | None
    competition_number_normalized: str | None
    home: str | None
    away: str | None
    kickoff_time: str | None
    kickoff_time_known: bool
    identifier_stability: str
    stability_scope: SourceIdentityScope
    extractor_identity: str
    raw_evidence_sha256: str
    raw_values: tuple[tuple[str, str], ...] = ()
    evidence_completeness: tuple[tuple[str, bool], ...] = ()
    notes: tuple[str, ...] = ()
    identity_stability_reason: str = ""
    #: provider 标识符的唯一性作用域（见模块 docstring）。
    #: 未提供 provider 标识符时为 ``NOT_APPLICABLE``；提供了但无独立文档保证时为
    #: ``UNKNOWN`` / ``UNVERIFIED``。该值参与语义指纹，因此「未验证」与「已验证」
    #: 的同一原始值得到**不同**的证据指纹。
    raw_provider_id_uniqueness_scope: str = (
        ProviderIdUniquenessScope.NOT_APPLICABLE.value
    )

    # -- 派生视图 ---------------------------------------------------------

    @property
    def raw_provider_id_present(self) -> bool:
        """源是否**实际提供**了 provider 标识符（缺失即缺失，绝不合成）。"""
        return self.raw_provider_id is not None

    @property
    def provider_id_uniqueness_scope(self) -> str:
        """provider 标识符的唯一性作用域（只读视图）。"""
        return self.raw_provider_id_uniqueness_scope

    @property
    def provider_id_uniqueness_verified(self) -> bool:
        """该 provider 标识符的唯一性是否已有独立文档支持。"""
        return (
            self.raw_provider_id is not None
            and self.raw_provider_id_uniqueness_scope
            == ProviderIdUniquenessScope.VERIFIED_SCOPED.value
        )

    @property
    def is_row_position_unstable(self) -> bool:
        return self.identifier_stability == IdentifierStability.ROW_POSITION_UNSTABLE.value

    @property
    def competition_identity(self) -> str | None:
        return competition_identity_key(
            self.sport, self.event_date, self.competition_number_normalized
        )

    def raw_values_dict(self) -> dict[str, str]:
        return dict(self.raw_values)

    def completeness_dict(self) -> dict[str, bool]:
        return dict(self.evidence_completeness)

    def missing_evidence(self) -> tuple[str, ...]:
        """明确列出**未建立**的证据维度（缺失必须保持显式缺失）。"""
        return tuple(sorted(k for k, v in self.evidence_completeness if not v))

    # -- 指纹 -------------------------------------------------------------

    def semantic_payload(self) -> dict:
        """
        语义证据载荷：只包含「该观测断言了什么」。

        刻意**排除** ``observation_id`` / ``raw_evidence_sha256`` 等派生字段，
        使语义指纹只反映证据内容本身。
        """
        return {
            "provenance_schema_version": self.provenance_schema_version,
            "adapter": self.adapter,
            "observation_category": self.observation_category,
            "source_namespace": self.source_namespace,
            "sport": self.sport,
            "event_date": self.event_date,
            "raw_provider_id": self.raw_provider_id,
            "raw_provider_id_type": self.raw_provider_id_type,
            "raw_provider_id_uniqueness_scope": self.raw_provider_id_uniqueness_scope,
            "derived_source_id": self.derived_source_id,
            "competition_number_raw": self.competition_number_raw,
            "competition_number_normalized": self.competition_number_normalized,
            "home": self.home,
            "away": self.away,
            "kickoff_time": self.kickoff_time,
            "kickoff_time_known": self.kickoff_time_known,
            "identifier_stability": self.identifier_stability,
            "stability_scope": self.stability_scope.as_dict(),
            "extractor_identity": self.extractor_identity,
        }

    def semantic_fingerprint(self) -> str:
        """规范化的**语义**证据指纹（与原始字节无关）。"""
        return fingerprint(self.semantic_payload())

    def as_dict(self) -> dict:
        """稳定序列化的完整审计记录（含原始值与缺失证据）。"""
        payload = self.semantic_payload()
        payload.update(
            {
                "observation_id": self.observation_id,
                "canonical_match_id": self.canonical_match_id,
                "raw_evidence_sha256": self.raw_evidence_sha256,
                "raw_values": dict(self.raw_values),
                "evidence_completeness": dict(self.evidence_completeness),
                "notes": list(self.notes),
                "identity_stability_reason": self.identity_stability_reason,
            }
        )
        return payload


# ---------------------------------------------------------------------------
# 稳定性分类
# ---------------------------------------------------------------------------

def classify_identifier_stability(
    *,
    adapter: str,
    derived_source_id: str | None,
    competition_number_normalized: str | None,
    raw_provider_id_present: bool,
) -> tuple[str, str, str]:
    """
    对**推导出的**源标识符做稳定性分类。

    返回 ``(stability, raw_identifier_type, reason)``。

    分类只依据可观测事实，不推断来源中不存在的保证。
    """
    if not derived_source_id:
        return (
            IdentifierStability.SYNTHETIC_LOCAL.value,
            RawIdentifierType.NONE.value,
            "无推导源标识符；本地合成标识符，不对应任何数据源值。",
        )

    suffix = derived_source_id.rsplit("-", 1)[-1]
    suffix_number = normalize_competition_number(suffix)
    looks_like_competition_number = (
        competition_number_normalized is not None
        and suffix_number is not None
        and suffix_number == competition_number_normalized
    )

    if adapter in ("finished_feed", "html_fallback"):
        return (
            IdentifierStability.ROW_POSITION_UNSTABLE.value,
            RawIdentifierType.ROW_INDEX.value,
            "标识符末段来自响应中的行序号，跨刷新不稳定。",
        )

    if looks_like_competition_number:
        return (
            IdentifierStability.COMPETITION_NUMBER_SCOPED.value,
            RawIdentifierType.COMPETITION_NUMBER.value,
            "标识符末段归一化后与竞彩编号一致；身份作用域为 (sport, date, 编号)。",
        )

    if raw_provider_id_present:
        return (
            IdentifierStability.PROVIDER_SUPPLIED_UNVERIFIED.value,
            RawIdentifierType.PROVIDER_MATCH_ID.value,
            "数据源提供了该标识符，但仓库内无其全局唯一性/跨刷新稳定性的独立文档保证。",
        )

    if adapter in ("live_football", "live_basketball", "market_xml"):
        return (
            IdentifierStability.DERIVED_FROM_PROVIDER_FIELDS.value,
            RawIdentifierType.UNKNOWN.value,
            "标识符由多个源字段推导，无法确认末段嵌入的是竞彩编号还是 provider 行号。",
        )

    return (
        IdentifierStability.UNKNOWN.value,
        RawIdentifierType.UNKNOWN.value,
        "无法分类该标识符的稳定性。",
    )


# ---------------------------------------------------------------------------
# 观测构造（纯函数，不修改输入）
# ---------------------------------------------------------------------------

#: 每种适配器读取的原始字段（用于保留原始证据）。
_RAW_FIELDS: dict[str, tuple[str, ...]] = {
    "live_football": ("id", "date", "time", "home", "away", "jczq_no"),
    "live_basketball": ("id", "date", "time", "home", "away", "jczq_no"),
    "market_xml": ("id", "date", "time", "home", "away", "jczq_no", "round"),
    "finished_feed": ("id", "date", "time", "home", "away"),
    "html_fallback": ("id", "date", "time", "home", "away"),
}

_EXTRACTOR_IDENTITY: dict[str, str] = {
    "live_football": "utils.fetcher_500._parse_live@1",
    "live_basketball": "utils.fetcher_500._parse_lq@1",
    "market_xml": "utils.fetcher_500.fetch_jczq_xml@1",
    "finished_feed": "utils.fetcher_500._parse_wanchang@1",
    "html_fallback": "utils.fetcher_500._parse_jczq_html@1",
}


def observe_source_row(
    row: Mapping[str, Any],
    *,
    adapter: str,
    canonical_match_id: str | None = None,
    raw_provider_id: str | None = None,
    raw_provider_id_type: str | None = None,
    raw_provider_id_uniqueness_scope: str | None = None,
) -> SourceObservation:
    """
    把一条规范化后的源比赛记录转成 ``SourceObservation``。

    - ``row`` 由现有解析器产出（``utils/fetcher_500``），本函数**不修改**它。
    - ``raw_provider_id`` 只在**数据源确实提供**时才传入；默认 ``None`` 表示源未提供
      该字段 —— 缺失即缺失，绝不用其他字段合成。
    - ``raw_provider_id_type`` 显式给出该原始标识符的类型（例如 HTML 元素 id）。
      省略时沿用既有稳定性分类结果，行为与历史版本完全一致。
    - ``raw_provider_id_uniqueness_scope`` 显式给出唯一性作用域；省略时由
      ``VERIFIED_PROVIDER_ID_SCOPES`` 解析，**默认 UNKNOWN**（不推断未文档化的保证）。
    - ``canonical_match_id`` 只能来自既有 Canonical 记录，本函数绝不生成。
    """
    if not isinstance(row, Mapping):
        raise TypeError("observe_source_row expects a mapping")

    pattern = pattern_for_adapter(adapter)
    category = (
        pattern.observation_category
        if pattern
        else ObservationCategory.SYNTHETIC.value
    )

    derived_source_id = row.get("id")
    derived_source_id = str(derived_source_id) if derived_source_id else None

    raw_cno = row.get("jczq_no")
    raw_cno = str(raw_cno).strip() if raw_cno not in (None, "") else None
    if raw_cno is None:
        # 兼容既有 canonical 记录形态（读只读，不改写）
        fallback = competition_number_for_match(dict(row))
        raw_cno = fallback
    normalized_cno = normalize_competition_number(raw_cno)

    namespace = (
        derived_source_id.split("-", 1)[0] if derived_source_id else MISSING
    )
    kickoff_time = row.get("time")
    kickoff_known = bool(row.get("kickoff_time_known", False))

    stability, classified_raw_type, reason = classify_identifier_stability(
        adapter=adapter,
        derived_source_id=derived_source_id,
        competition_number_normalized=normalized_cno,
        raw_provider_id_present=raw_provider_id is not None,
    )
    raw_type = raw_provider_id_type or classified_raw_type

    if raw_provider_id is None:
        uniqueness_scope = ProviderIdUniquenessScope.NOT_APPLICABLE.value
    else:
        uniqueness_scope = raw_provider_id_uniqueness_scope or (
            resolve_provider_id_uniqueness_scope(
                adapter=adapter,
                provider_namespace=namespace,
                sport=str(row.get("sport") or MISSING),
                raw_identifier_type=raw_type,
            )
        )

    scope = SourceIdentityScope(
        adapter=adapter,
        sport=str(row.get("sport") or MISSING),
        event_date=(str(row["date"]) if row.get("date") else None),
        provider_namespace=namespace,
        raw_identifier_type=raw_type,
    )

    raw_fields = _RAW_FIELDS.get(adapter, ("id", "date", "time", "home", "away"))
    raw_values = tuple(
        (name, MISSING if row.get(name) is None else str(row.get(name)))
        for name in raw_fields
    )

    completeness = (
        ("raw_provider_id", raw_provider_id is not None),
        ("derived_source_id", derived_source_id is not None),
        ("competition_number", normalized_cno is not None),
        ("home_team", bool(row.get("home"))),
        ("away_team", bool(row.get("away"))),
        ("event_date", bool(row.get("date"))),
        ("kickoff_time_known", kickoff_known),
        ("canonical_match_id", canonical_match_id is not None),
        (
            "provider_id_uniqueness_verified",
            uniqueness_scope == ProviderIdUniquenessScope.VERIFIED_SCOPED.value,
        ),
    )

    notes: list[str] = []
    if raw_provider_id is None:
        notes.append(
            "源未提供可保留的 provider 标识符；记为缺失（MISSING），未用其他字段合成。"
        )
    else:
        notes.append(
            "provider 标识符由数据源提供，但其唯一性/跨刷新稳定性未经独立文档验证"
            f"（uniqueness_scope={uniqueness_scope}）；不得据此授权活动绑定。"
        )
    if pattern is not None and not pattern.raw_provider_id_preserved:
        notes.append(f"{adapter}: 规范化比赛行未保留 provider 原始标识符。")
    if stability == IdentifierStability.ROW_POSITION_UNSTABLE.value:
        notes.append("该标识符由行位置派生，不得用作持久身份依据。")

    raw_evidence_sha256 = fingerprint({name: value for name, value in raw_values})

    observation = SourceObservation(
        provenance_schema_version=PROVENANCE_SCHEMA_VERSION,
        observation_id="",
        adapter=adapter,
        observation_category=category,
        source_namespace=namespace,
        sport=scope.sport,
        event_date=scope.event_date,
        raw_provider_id=raw_provider_id,
        raw_provider_id_type=raw_type,
        derived_source_id=derived_source_id,
        canonical_match_id=canonical_match_id,
        competition_number_raw=raw_cno,
        competition_number_normalized=normalized_cno,
        home=(str(row["home"]) if row.get("home") else None),
        away=(str(row["away"]) if row.get("away") else None),
        kickoff_time=(str(kickoff_time) if kickoff_time else None),
        kickoff_time_known=kickoff_known,
        identifier_stability=stability,
        stability_scope=scope,
        extractor_identity=_EXTRACTOR_IDENTITY.get(adapter, f"{adapter}@UNKNOWN"),
        raw_evidence_sha256=raw_evidence_sha256,
        raw_values=raw_values,
        evidence_completeness=completeness,
        notes=tuple(notes),
        identity_stability_reason=reason,
        raw_provider_id_uniqueness_scope=uniqueness_scope,
    )

    # observation_id 由语义指纹派生：同一证据得到同一 ID，与输入顺序无关。
    object.__setattr__(observation, "observation_id", observation.semantic_fingerprint())
    return observation


def observe_batch(
    rows: Iterable[Mapping[str, Any]],
    *,
    adapter: str,
    canonical_match_id_by_row: Mapping[int, str] | None = None,
) -> tuple[SourceObservation, ...]:
    """批量观测（只读）。返回顺序与输入一致；语义指纹与顺序无关。"""
    out: list[SourceObservation] = []
    for index, row in enumerate(rows):
        canonical_id = None
        if canonical_match_id_by_row is not None:
            canonical_id = canonical_match_id_by_row.get(index)
        out.append(
            observe_source_row(
                row, adapter=adapter, canonical_match_id=canonical_id
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# 批次语义指纹（与输入顺序无关）
# ---------------------------------------------------------------------------

def batch_semantic_fingerprint(observations: Sequence[SourceObservation]) -> str:
    """
    批次语义证据指纹。

    对每一条观测的语义指纹**排序后**再哈希，因此与输入行序无关（§7.5）。
    """
    digests = sorted(obs.semantic_fingerprint() for obs in observations)
    return fingerprint(
        {
            "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
            "observation_count": len(digests),
            "observation_fingerprints": digests,
        }
    )


def batch_raw_evidence_fingerprint(observations: Sequence[SourceObservation]) -> str:
    """批次原始证据指纹（同样与输入行序无关）。"""
    digests = sorted(obs.raw_evidence_sha256 for obs in observations)
    return fingerprint(
        {
            "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
            "observation_count": len(digests),
            "raw_evidence_digests": digests,
        }
    )


def build_provenance_report(
    observations: Sequence[SourceObservation],
) -> dict:
    """
    只读的确定性溯源报告（供审计与影子校验消费）。

    报告同时给出原始证据指纹与语义指纹，二者的区别是显式的。
    """
    ordered = sorted(observations, key=lambda o: o.observation_id)
    return {
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "observation_count": len(ordered),
        "batch_semantic_fingerprint": batch_semantic_fingerprint(observations),
        "batch_raw_evidence_fingerprint": batch_raw_evidence_fingerprint(observations),
        "adapter_histogram": _histogram(o.adapter for o in ordered),
        "stability_histogram": _histogram(o.identifier_stability for o in ordered),
        "namespace_histogram": _histogram(o.source_namespace for o in ordered),
        "observations": [o.as_dict() for o in ordered],
    }


def _histogram(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


# ---------------------------------------------------------------------------
# 冲突检测（只读；仅分类，不做决策）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdentityConflict:
    """一条只读的身份冲突分类结论。"""

    category: str
    subject: str
    severity: str
    detail: str
    observation_ids: tuple[str, ...]
    evidence: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "subject": self.subject,
            "severity": self.severity,
            "detail": self.detail,
            "observation_ids": list(self.observation_ids),
            "evidence": dict(self.evidence),
        }


#: 冲突类别
CONFLICT_CROSS_SPORT_NAMESPACE = "CROSS_SPORT_SHARED_NAMESPACE"
CONFLICT_ROW_POSITION_REUSE = "ROW_POSITION_REUSE"
CONFLICT_STRONG_NUMBER_CONFLICT = "STRONG_NUMBER_CONFLICT"
CONFLICT_CANONICAL_OWNER_CONFLICT = "CANONICAL_OWNER_CONFLICT"

#: 严重度（决策由治理层作出；本层只分类）
SEVERITY_AMBIGUOUS = "AMBIGUOUS"
SEVERITY_REJECT = "REJECT"


def detect_cross_sport_namespace_collision(
    observations: Sequence[SourceObservation],
) -> tuple[IdentityConflict, ...]:
    """
    检测**同一推导源 ID 跨运动/跨日期**的碰撞（§6/§12）。

    足球与篮球共用 ``500j`` 前缀，同一日期下两侧竞彩编号序列相互独立，因此同一个
    500j 字符串可能代表两个不同运动的不同事件。二者必须保持为**不同的作用域身份**；
    共享的旧字符串必须被认定为歧义，绝不静默授权两个不同的 Canonical 所有者。
    """
    groups: dict[str, list[SourceObservation]] = {}
    for obs in observations:
        if obs.derived_source_id:
            groups.setdefault(obs.derived_source_id, []).append(obs)

    conflicts: list[IdentityConflict] = []
    for source_id in sorted(groups):
        members = groups[source_id]
        scopes = {
            (o.sport, o.event_date or MISSING) for o in members
        }
        if len(scopes) < 2:
            continue
        conflicts.append(
            IdentityConflict(
                category=CONFLICT_CROSS_SPORT_NAMESPACE,
                subject=source_id,
                severity=SEVERITY_AMBIGUOUS,
                detail=(
                    "同一源标识符字符串出现在多个 (sport, date) 作用域中；"
                    "该字符串本身不足以确立唯一事件身份。"
                ),
                observation_ids=tuple(sorted(o.observation_id for o in members)),
                evidence=tuple(
                    sorted(
                        (o.sport, o.event_date or MISSING) for o in members
                    )
                ),
            )
        )
    return tuple(conflicts)


def detect_row_position_reuse(
    observations: Sequence[SourceObservation],
) -> tuple[IdentityConflict, ...]:
    """
    检测 **ROW_POSITION_UNSTABLE 标识符被复用于不同比赛**（§6/§12）。

    结论是 REVIEW_REQUIRED 或 REJECTED 级别的事实认定；本函数**不**改写任何映射。
    """
    groups: dict[str, list[SourceObservation]] = {}
    for obs in observations:
        if obs.derived_source_id and obs.is_row_position_unstable:
            groups.setdefault(obs.derived_source_id, []).append(obs)

    conflicts: list[IdentityConflict] = []
    for source_id in sorted(groups):
        members = groups[source_id]
        events = {
            (o.event_date or MISSING, o.home or MISSING, o.away or MISSING)
            for o in members
        }
        if len(events) < 2:
            continue
        conflicts.append(
            IdentityConflict(
                category=CONFLICT_ROW_POSITION_REUSE,
                subject=source_id,
                severity=SEVERITY_AMBIGUOUS,
                detail=(
                    "行位置派生的标识符被分配给多个不同比赛；"
                    "不得据此自动覆盖任何既有映射。"
                ),
                observation_ids=tuple(sorted(o.observation_id for o in members)),
                evidence=tuple(sorted((e[1], e[2]) for e in events)),
            )
        )
    return tuple(conflicts)


def detect_strong_number_conflict(
    observations: Sequence[SourceObservation],
) -> tuple[IdentityConflict, ...]:
    """
    检测**同一源身份试图代表两个强编号互斥的事件**（§12）。

    结论级别为 REJECTED。
    """
    groups: dict[tuple, list[SourceObservation]] = {}
    for obs in observations:
        if obs.derived_source_id:
            groups.setdefault(obs.stability_scope.as_key(), []).append(obs)

    conflicts: list[IdentityConflict] = []
    for key in sorted(groups, key=lambda k: tuple(str(x) for x in k)):
        members = groups[key]
        numbers = {
            o.competition_number_normalized
            for o in members
            if o.competition_number_normalized is not None
        }
        if len(numbers) < 2:
            continue
        conflicts.append(
            IdentityConflict(
                category=CONFLICT_STRONG_NUMBER_CONFLICT,
                subject="|".join(str(part) for part in key),
                severity=SEVERITY_REJECT,
                detail=(
                    "同一作用域内的源记录携带互斥的强竞彩编号；"
                    "强编号冲突绝不退化为队名匹配。"
                ),
                observation_ids=tuple(sorted(o.observation_id for o in members)),
                evidence=tuple(sorted((n, n) for n in numbers)),
            )
        )
    return tuple(conflicts)


def detect_canonical_owner_conflict(
    observations: Sequence[SourceObservation],
) -> tuple[IdentityConflict, ...]:
    """
    检测**一个源身份被观测到对应多个不同 Canonical ID**（§12）。

    结论级别为 REVIEW_REQUIRED 或 REJECTED；**绝不**自动采用「最新一次声明」。
    """
    groups: dict[str, list[SourceObservation]] = {}
    for obs in observations:
        if obs.derived_source_id and obs.canonical_match_id:
            groups.setdefault(obs.derived_source_id, []).append(obs)

    conflicts: list[IdentityConflict] = []
    for source_id in sorted(groups):
        members = groups[source_id]
        owners = {o.canonical_match_id for o in members}
        if len(owners) < 2:
            continue
        conflicts.append(
            IdentityConflict(
                category=CONFLICT_CANONICAL_OWNER_CONFLICT,
                subject=source_id,
                severity=SEVERITY_AMBIGUOUS,
                detail=(
                    "同一源身份被观测到对应多个 Canonical ID；"
                    "必须经治理决策处理，绝不自动采用最新声明。"
                ),
                observation_ids=tuple(sorted(o.observation_id for o in members)),
                evidence=tuple(sorted((o, o) for o in owners)),
            )
        )
    return tuple(conflicts)


def detect_all_conflicts(
    observations: Sequence[SourceObservation],
) -> tuple[IdentityConflict, ...]:
    """执行全部只读冲突检测，返回按 (category, subject) 排序的确定性结论。"""
    found = (
        list(detect_cross_sport_namespace_collision(observations))
        + list(detect_row_position_reuse(observations))
        + list(detect_strong_number_conflict(observations))
        + list(detect_canonical_owner_conflict(observations))
    )
    return tuple(sorted(found, key=lambda c: (c.category, c.subject)))


def conflict_report(observations: Sequence[SourceObservation]) -> dict:
    """只读的确定性冲突报告。"""
    conflicts = detect_all_conflicts(observations)
    return {
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "observation_count": len(observations),
        "batch_semantic_fingerprint": batch_semantic_fingerprint(observations),
        "conflict_count": len(conflicts),
        "conflicts": [c.as_dict() for c in conflicts],
    }


def planner_competition_key(row: Mapping[str, Any]) -> str | None:
    """
    只读复用既有摄取规划器的强编号键（不复制、不替换其实现）。
    """
    return competition_event_key(dict(row))
