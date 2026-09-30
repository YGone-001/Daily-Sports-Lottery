"""
泊松分布模型
用于预测比赛比分概率矩阵
"""
import math
from typing import Tuple


def poisson_pmf(lam: float, k: int) -> float:
    """泊松分布概率质量函数: P(X=k) = (λ^k * e^-λ) / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k * math.exp(-lam)) / math.factorial(k)


def expected_goals(attack: float, defense_opponent: float,
                   base_rate: float = 1.35) -> float:
    """
    计算期望进球数(λ)
    - attack: 攻击力 [0-1]
    - defense_opponent: 对手防守力 [0-1]
    - base_rate: 世界杯场均进球基准
    """
    # 攻击力增强进球，对手防守削弱进球
    # 公式: 基准进球 * (1 + 攻击力加成) * (1 - 对手防守削弱)
    attack_boost = 1.0 + (attack - 0.5) * 1.5   # 攻击力偏移量放大
    defense_reduction = 1.0 - (defense_opponent - 0.5) * 0.8  # 防守削弱
    lam = base_rate * attack_boost * defense_reduction
    return max(0.35, min(lam, 4.0))  # 限制在合理范围


def score_matrix(lambda_a: float, lambda_b: float,
                 max_goals: int = 6, rho: float = -0.15) -> dict:
    """
    生成比分概率矩阵 (加入 Dixon-Coles 修正)
    返回 {(i,j): probability} 字典
    """
    matrix = {}
    total_prob = 0.0
    for i in range(max_goals):
        for j in range(max_goals):
            prob = poisson_pmf(lambda_a, i) * poisson_pmf(lambda_b, j)
            
            # Dixon-Coles correction for low-scoring matches
            if i == 0 and j == 0:
                tau = 1 - lambda_a * lambda_b * rho
            elif i == 0 and j == 1:
                tau = 1 + lambda_a * rho
            elif i == 1 and j == 0:
                tau = 1 + lambda_b * rho
            elif i == 1 and j == 1:
                tau = 1 - rho
            else:
                tau = 1.0
                
            prob = max(0.0, prob * tau)
            matrix[(i, j)] = prob
            total_prob += prob

    # Normalize matrix so it sums to 1.0
    if total_prob > 0:
        for k in matrix:
            matrix[k] /= total_prob

    return matrix


def win_draw_loss(matrix: dict) -> Tuple[float, float, float]:
    """从比分矩阵计算胜/平/负概率"""
    win = draw = loss = 0.0
    for (i, j), prob in matrix.items():
        if i > j:
            win += prob
        elif i == j:
            draw += prob
        else:
            loss += prob
    total = win + draw + loss
    return win / total, draw / total, loss / total


def top_scores(matrix: dict, n: int = 3) -> list:
    """获取概率最高的n个比分"""
    sorted_scores = sorted(matrix.items(), key=lambda x: x[1], reverse=True)
    results = []
    for (i, j), prob in sorted_scores[:n]:
        results.append({
            "score": f"{i}-{j}",
            "home": i,
            "away": j,
            "probability": round(prob * 100, 1)
        })
    return results


def expected_total_goals(lambda_a: float, lambda_b: float) -> dict:
    """预测总进球数分布"""
    matrix = score_matrix(lambda_a, lambda_b)
    totals = {}
    for (i, j), prob in matrix.items():
        total = i + j
        totals[total] = totals.get(total, 0) + prob

    return {
        "most_likely": max(totals, key=totals.get),
        "over_2_5": sum(p for g, p in totals.items() if g >= 3),
        "under_2_5": sum(p for g, p in totals.items() if g < 3),
    }
