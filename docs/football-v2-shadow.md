# Football V2 影子候选（league-environment）契约与边界

> 状态：**EXPERIMENTAL — 不进生产**。生产足球模型仍是 `football-coldstart-1`，
> `config.MODEL_VERSIONS` 不变；生产路径不 import 本模块。

## 1. 动机（来自 V1 数学审计）

V1 审计（本机 `reports/phase6b/football-v1-mathematical-audit/`）确认：

- 平局概率在 113 场队列上只分布在 **0.26–0.30**（std 0.0076）——几乎不携带判别
  信息（draw AUC 0.4411、0/29 平局进入 argmax）；
- 期望进球被结构性压缩：λ_home std ≈ 0.13、λ_away std ≈ 0.11；
- Elo → 攻防评分斜率 0.15/400，且 1165/1168 队样本低于 5 场先验收缩阈值；
- 模型没有联赛进球环境项（base_rate = 1.35 为全局常数）；
- Dixon-Coles rho 只移动平局"水平"（0.24→0.29），AUC 对 rho 完全不敏感——
  调 rho 无法修复排序。

## 2. 候选设计（一件事，可解释，无自由参数）

`models/football_model_v2.py`：

1. **因果联赛环境先验**：对每个联赛维护"场均总进球"先验，只允许
   `settled_at < prediction_generated_at` 的**真实全场比分**进入；Laplace α=1
   向全局先验 2.7（= base_rate×2）收缩。两个常数都是文档化值，**不从队列拟合**。
2. **总量重校**：把 V1 预测时刻已持久化的 λ_home/λ_away **等比**缩放到先验总量
   （缩放因子有界 [0.5, 2.0]，防极端先验）。
3. **复用生产比分矩阵**：`models.poisson_model.score_matrix` + Dixon-Coles rho
   （不改数学）重推胜平负。

**纯模型边界**：不读赔率、不融合市场概率；市场信息属于另一候选家族（未实现）。
**点时特征边界**：攻防评分未持久化，本候选只使用评估行里已有的
λ/联赛/Elo/`settled_at`/比分——绝不从今天的 `team_strength.json` 重建历史输入。

## 3. 回溯走查结果（113 场，RETROSPECTIVE_WALK_FORWARD_RESEARCH）

| 指标 | V1（`football-coldstart-1`） | V2 shadow |
| --- | ---: | ---: |
| accuracy | 0.5398 | 0.5398 |
| multiclass Brier | 0.6331 | **0.6294** |
| LogLoss | 1.0500 | **1.0436** |
| macro ECE | 0.0677 | 0.0960 |
| draw AUC | 0.4475 | **0.5386** |
| argmax 平局数 | 0 | 2 |

配对自助（canonical `paired_bootstrap`，2000 次）：V1−V2 的 Brier 差 +0.0037
（95% CI **[−0.0008, +0.0089]**）、LogLoss 差 +0.0059（CI [−0.0013, +0.0140]) ——
**区间均含 0，改进不显著**。泄漏守卫：严格 `settled_at < prediction_generated_at`、
逐联赛先验计数单调、自 excl、零参数拟合。

## 4. 结论与晋升

- `FOOTBALL_V2_INCREMENTAL_VALUE = PRELIMINARY`：Brier/LogLoss 点估计略优但
  不显著；draw AUC 由 <0.5 转为 >0.5 值得继续观察；ECE 变差。
- **不晋升**：生产模型不变；前瞻对照必须满足
  `reports/phase6b/prospective-shadow-readiness/promotion-criteria.md` 的事前门。
- 本候选的意义在于**机制验证**：联赛环境先验是可因果化、可解释、零拟合的
  最小增量；若未来前瞻队列成立，它是第一个待测对象。

## 5. 测试

`tests/test_football_model_v2.py`：因果性（只允许 settled_at < as_of 的真实比分
入先验）、空先验收缩、等比缩放与有界缩放因子、概率合法性、确定性、
无赔率输入（签名级断言）、缺特征行排除、顺序无关、无比分不入池、
版本命名空间隔离（生产版本不变）。
