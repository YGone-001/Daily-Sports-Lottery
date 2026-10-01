"""
时间加权的 Dixon-Coles rho 拟合
==============================
在**不可变评估样本**（`data/evaluation_rows.json`）之上做离线诊断：

    给定历史足球模型在开赛前**实际产出**的期望进球，
    哪一个 Dixon-Coles 低比分相关系数 rho 能让时间加权的历史负对数似然最小？

本模块是**纯离线拟合 / 诊断基础设施**：

- 只读评估样本，不重建历史 λ（不调用 `predict_match` / `expected_goals` / `enrich_match`）。
- 不写任何运行时文件，不修改 `config.py`。
- **不把拟合结果应用到线上预测**：线上足球模型仍使用
  `config.MODEL_CONFIG["dixon_coles_rho"] = -0.15`，模型版本仍为 `football-ad-1`。
  如果运行期自动重拟合，同一个 `model_version` 的含义会随时间漂移，破坏既有的
  版本化历史对比体系；因此拟合与部署必须分离。

权威输入
--------
每条观测只使用评估样本中已冻结的历史字段：

    expected_score_data.expected_goals.home / .away   （历史 λ / μ）
    final_score.home / .away                          （实际比分）
    kickoff_at                                        （用于时间衰减）
    sport / model_name / model_version / evaluation_id

权重基准时间取该分组内**最晚的 kickoff_at**，不使用当前时钟，因此同一批历史数据
无论哪天拟合都得到完全相同的结果。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable, Sequence

from models.poisson_model import poisson_pmf

# 默认时间衰减半衰期（天）
DEFAULT_HALF_LIFE_DAYS = 180.0

# 默认候选网格
DEFAULT_RHO_MIN = -0.25
DEFAULT_RHO_MAX = 0.25
DEFAULT_RHO_STEP = 0.005

# 会收到非单位 Dixon-Coles 修正的比分单元（其余比分 tau = 1）
LOW_SCORE_CELLS: tuple[tuple[int, int], ...] = ((0, 0), (0, 1), (1, 0), (1, 1))

# 一个拟合观测：(lambda_home, lambda_away, home_goals, away_goals, weight)
Observation = tuple[float, float, int, int, float]


class DixonColesFittingError(Exception):
    """评估样本无法用于 rho 拟合时抛出（显式失败，不静默跳过）。"""

    def __init__(
        self,
        reason: str,
        *,
        evaluation_id: str | None = None,
        match_id: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        detail: str = "",
    ) -> None:
        self.reason = reason
        self.evaluation_id = evaluation_id
        self.match_id = match_id
        self.model_name = model_name
        self.model_version = model_version
        self.detail = detail
        message = (
            f"Dixon-Coles 拟合错误({reason}) model={model_name} version={model_version} "
            f"match={match_id} evaluation={evaluation_id}"
        )
        if detail:
            message = f"{message} :: {detail}"
        super().__init__(message)


# ---------------------------------------------------------------------------
# 基础数值
# ---------------------------------------------------------------------------

def _is_number(value: object) -> bool:
    """bool 在本语义下不算数值。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    return _is_number(value) and math.isfinite(float(value))


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# ---------------------------------------------------------------------------
# Dixon-Coles 低比分修正
# ---------------------------------------------------------------------------

def dixon_coles_tau(
    home_goals: int,
    away_goals: int,
    lambda_home: float,
    lambda_away: float,
    rho: float,
) -> float:
    """
    低比分修正因子，与 `models.poisson_model.score_matrix()` 的约定完全一致：

        0-0 -> 1 - λ * μ * ρ
        0-1 -> 1 + λ * ρ
        1-0 -> 1 + μ * ρ
        1-1 -> 1 - ρ
        其他 -> 1
    """
    if home_goals == 0 and away_goals == 0:
        return 1.0 - lambda_home * lambda_away * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + lambda_home * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + lambda_away * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def is_valid_rho_for_observation(observation: Observation, rho: float) -> bool:
    """
    候选 rho 是否在该观测的 λ/μ 域上让**四个**低比分修正都为正且有限。

    任一项 <= 0 或非有限 -> 该候选对该观测无效（不裁剪、不取 log(0)/log(负)）。
    """
    lambda_home, lambda_away = observation[0], observation[1]
    for home_goals, away_goals in LOW_SCORE_CELLS:
        tau = dixon_coles_tau(home_goals, away_goals, lambda_home, lambda_away, rho)
        if not math.isfinite(tau) or tau <= 0.0:
            return False
    return True


def observation_probability(observation: Observation, rho: float) -> float:
    """
    观测比分在给定 rho 下的 Dixon-Coles 概率：

        P = Poisson(x | λ) * Poisson(y | μ) * tau(x, y, λ, μ, ρ)

    不对完整比分矩阵做重归一化——似然只在**实际发生的比分**上求值。
    """
    lambda_home, lambda_away, home_goals, away_goals, _weight = observation
    tau = dixon_coles_tau(home_goals, away_goals, lambda_home, lambda_away, rho)
    probability = (
        poisson_pmf(lambda_home, home_goals)
        * poisson_pmf(lambda_away, away_goals)
        * tau
    )
    if not math.isfinite(probability) or probability <= 0.0:
        raise DixonColesFittingError(
            "invalid_tau", detail=f"rho={rho} 下概率非正: {probability!r}"
        )
    return probability


def weighted_negative_log_likelihood(observations: Sequence[Observation], rho: float) -> float:
    """
    加权负对数似然：

        Σ_i weight_i * -ln(P_DC_i)

    拟合判据就是这个**求和**，不按样本数取平均。任一观测在该 rho 下无效则抛错。
    """
    total = 0.0
    for observation in observations:
        if not is_valid_rho_for_observation(observation, rho):
            raise DixonColesFittingError(
                "invalid_tau", detail=f"rho={rho} 使低比分修正非正"
            )
        total += observation[4] * -math.log(observation_probability(observation, rho))
    return total


# ---------------------------------------------------------------------------
# 时间衰减
# ---------------------------------------------------------------------------

def time_decay_weight(
    age_days: float,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """
    指数半衰期衰减：

        weight = exp(-ln(2) * age_days / half_life_days)

    age 0 -> 1；age = half_life -> 0.5；age = 2 * half_life -> 0.25。
    `half_life_days` 必须有限且 > 0。
    """
    if not _is_finite_number(half_life_days) or float(half_life_days) <= 0.0:
        raise DixonColesFittingError(
            "invalid_half_life", detail=f"half_life_days={half_life_days!r}"
        )
    return math.exp(-math.log(2.0) * age_days / float(half_life_days))


def _parse_kickoff(value: object, row: dict) -> datetime:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DixonColesFittingError("missing_kickoff", **_row_context(row))
    if not isinstance(value, str):
        raise DixonColesFittingError("invalid_kickoff", **_row_context(row), detail=repr(value))
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise DixonColesFittingError(
            "invalid_kickoff", **_row_context(row), detail=repr(value)
        ) from None


# ---------------------------------------------------------------------------
# 输入抽取与校验
# ---------------------------------------------------------------------------

def _row_context(row: dict) -> dict:
    return {
        "evaluation_id": row.get("evaluation_id"),
        "match_id": row.get("match_id"),
        "model_name": row.get("model_name"),
        "model_version": row.get("model_version"),
    }


def _extract_expected_goals(row: dict) -> tuple[float, float]:
    expected = row.get("expected_score_data")
    goals = expected.get("expected_goals") if isinstance(expected, dict) else None
    if not isinstance(goals, dict):
        raise DixonColesFittingError("missing_expected_goals", **_row_context(row))

    home = goals.get("home")
    away = goals.get("away")
    for label, value in (("home", home), ("away", away)):
        if value is None:
            raise DixonColesFittingError(
                "missing_expected_goals", **_row_context(row), detail=f"expected_goals.{label}"
            )
        if not _is_finite_number(value) or float(value) <= 0.0:
            raise DixonColesFittingError(
                "invalid_expected_goals", **_row_context(row), detail=f"{label}={value!r}"
            )
    return float(home), float(away)


def _extract_final_score(row: dict) -> tuple[int, int]:
    score = row.get("final_score")
    if not isinstance(score, dict):
        raise DixonColesFittingError("missing_final_score", **_row_context(row))

    home = score.get("home")
    away = score.get("away")
    for label, value in (("home", home), ("away", away)):
        if value is None:
            raise DixonColesFittingError(
                "missing_final_score", **_row_context(row), detail=f"final_score.{label}"
            )
        if not _is_non_negative_int(value):
            raise DixonColesFittingError(
                "invalid_final_score", **_row_context(row), detail=f"{label}={value!r}"
            )
    return int(home), int(away)


def _extract_observation(row: dict) -> tuple[float, float, int, int, datetime]:
    """从一条评估样本抽取拟合所需的历史字段（不含权重）。"""
    if row.get("sport") != "football":
        raise DixonColesFittingError(
            "unsupported_sport", **_row_context(row), detail=f"sport={row.get('sport')!r}"
        )
    lambda_home, lambda_away = _extract_expected_goals(row)
    home_goals, away_goals = _extract_final_score(row)
    kickoff = _parse_kickoff(row.get("kickoff_at"), row)
    return lambda_home, lambda_away, home_goals, away_goals, kickoff


# ---------------------------------------------------------------------------
# 候选网格
# ---------------------------------------------------------------------------

def build_rho_candidates(
    rho_min: float = DEFAULT_RHO_MIN,
    rho_max: float = DEFAULT_RHO_MAX,
    rho_step: float = DEFAULT_RHO_STEP,
) -> list[float]:
    """
    确定性构造候选 rho（整数索引构造，避免逐步累加的浮点漂移）。

    端点采用容差安全的包含处理；返回列表内不含重复值。
    """
    if not _is_finite_number(rho_min) or not _is_finite_number(rho_max):
        raise DixonColesFittingError("invalid_grid", detail=f"min={rho_min!r} max={rho_max!r}")
    if not _is_finite_number(rho_step) or float(rho_step) <= 0.0:
        raise DixonColesFittingError("invalid_grid", detail=f"step={rho_step!r}")
    if float(rho_max) < float(rho_min):
        raise DixonColesFittingError("invalid_grid", detail="rho_max < rho_min")

    span = float(rho_max) - float(rho_min)
    count = int(math.floor(span / float(rho_step) + 1e-9)) + 1
    return [float(rho_min) + index * float(rho_step) for index in range(count)]


def _choose_rho(valid_candidates: Sequence[float], losses: dict[float, float]) -> float:
    """
    确定性选择：加权 NLL 最小 -> |rho| 更小 -> 数值更小。
    """
    return min(valid_candidates, key=lambda rho: (losses[rho], abs(rho), rho))


# ---------------------------------------------------------------------------
# 分组拟合
# ---------------------------------------------------------------------------

def fit_rho_group(
    rows: Iterable[dict],
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    rho_min: float = DEFAULT_RHO_MIN,
    rho_max: float = DEFAULT_RHO_MAX,
    rho_step: float = DEFAULT_RHO_STEP,
) -> dict:
    """
    对一组**同质**足球评估样本做时间加权 rho 拟合。

    要求组内 sport / model_name / model_version 完全一致（否则 `mixed_group`），
    仅支持足球（否则 `unsupported_sport`），`evaluation_id` 不得重复，空组抛 `empty_group`。
    不做最小样本量拒绝，不返回任何排名 / 评级。

    返回值为纯内存诊断摘要，不落盘。
    """
    rows = list(rows)
    if not rows:
        raise DixonColesFittingError("empty_group", detail="拟合组为空")

    if not _is_finite_number(half_life_days) or float(half_life_days) <= 0.0:
        raise DixonColesFittingError(
            "invalid_half_life", detail=f"half_life_days={half_life_days!r}"
        )

    first = rows[0]
    key = (first.get("sport"), first.get("model_name"), first.get("model_version"))
    if any(
        (row.get("sport"), row.get("model_name"), row.get("model_version")) != key
        for row in rows[1:]
    ):
        raise DixonColesFittingError("mixed_group", detail=f"期望 {key}，收到混合分组")

    seen_ids: set = set()
    extracted: list[tuple[float, float, int, int, datetime]] = []
    for row in rows:
        evaluation_id = row.get("evaluation_id")
        if evaluation_id is not None:
            if evaluation_id in seen_ids:
                raise DixonColesFittingError(
                    "duplicate_evaluation_id", **_row_context(row), detail=str(evaluation_id)
                )
            seen_ids.add(evaluation_id)
        extracted.append(_extract_observation(row))

    candidates = build_rho_candidates(rho_min, rho_max, rho_step)

    # 基准时间：组内最晚开赛时间（不使用当前时钟）
    reference_kickoff = max(item[4] for item in extracted)

    observations: list[Observation] = []
    for lambda_home, lambda_away, home_goals, away_goals, kickoff in extracted:
        age_days = (reference_kickoff - kickoff).total_seconds() / 86400.0
        observations.append(
            (
                lambda_home,
                lambda_away,
                home_goals,
                away_goals,
                time_decay_weight(age_days, half_life_days),
            )
        )

    # 候选有效性：必须对**所有**观测都有效，否则整体剔除（不做部分拟合）
    valid_candidates = [
        rho
        for rho in candidates
        if all(is_valid_rho_for_observation(obs, rho) for obs in observations)
    ]
    if not valid_candidates:
        raise DixonColesFittingError(
            "no_valid_rho_candidates",
            detail=f"grid=[{rho_min}, {rho_max}] step={rho_step}",
        )

    losses = {rho: weighted_negative_log_likelihood(observations, rho) for rho in valid_candidates}
    fitted_rho = _choose_rho(valid_candidates, losses)

    rho_zero_nll = weighted_negative_log_likelihood(observations, 0.0)

    low_score_sample_count = sum(
        1 for obs in observations if (obs[2], obs[3]) in LOW_SCORE_CELLS
    )

    return {
        "sport": key[0],
        "model_name": key[1],
        "model_version": key[2],
        "sample_count": len(observations),
        "low_score_sample_count": low_score_sample_count,
        "reference_kickoff_at": reference_kickoff.isoformat(),
        "half_life_days": float(half_life_days),
        "effective_sample_weight": sum(obs[4] for obs in observations),
        "rho_grid": {
            "minimum": float(rho_min),
            "maximum": float(rho_max),
            "step": float(rho_step),
            "candidate_count": len(candidates),
            "valid_candidate_count": len(valid_candidates),
        },
        "fitted_rho": fitted_rho,
        "weighted_nll": losses[fitted_rho],
        "rho_zero_weighted_nll": rho_zero_nll,
        "weighted_nll_improvement_vs_zero": rho_zero_nll - losses[fitted_rho],
    }


def build_rho_fit_summaries(rows: Iterable[dict]) -> list[dict]:
    """
    按 (sport, model_name, model_version) 分组拟合，**只输出足球分组**。

    篮球分组直接跳过（不会产生 Dixon-Coles 摘要）；直接对篮球行调用
    `fit_rho_group` 则会抛 `unsupported_sport`。
    返回顺序按 sport / model_name / model_version 字典序，不按拟合质量排序。
    输入为空时返回 `[]`。
    """
    rows = list(rows)
    if not rows:
        return []

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("sport") != "football":
            continue
        key = (row.get("sport"), row.get("model_name"), row.get("model_version"))
        groups.setdefault(key, []).append(row)

    return [
        fit_rho_group(groups[key])
        for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), str(k[2])))
    ]


def fit_all_rho() -> list[dict]:
    """
    便捷入口：读取全部评估样本并做分组 rho 拟合（只输出足球分组）。

    只做「读取 + 纯计算」，不写入任何存储，也不改动线上模型配置。
    """
    from utils.evaluation_rows import get_all_evaluation_rows

    return build_rho_fit_summaries(get_all_evaluation_rows())
