/**
 * 智能策略与价值投注类型定义
 * 对应 /api/strategy 接口响应
 */

// 价值单注推荐
export interface ValueBet {
  match_id: string
  match_name: string
  league: string
  outcome: string
  odds: number
  ev: number
  kelly_pct: number
  model_prob: number
  implied_prob: number
}

// 串关单腿
export interface ComboLeg {
  match_id: string
  match_name: string
  outcome: string
  odds: number
  prob: number
}

// 串关组合结果 (2串1, 3串1, 4串1)
export interface ComboResult {
  legs: ComboLeg[]
  combined_prob: number
  combined_odds: number
  ev: number
  kelly_pct: number
}

// 策略聚合结果
export interface StrategyResult {
  single_bets: ValueBet[]
  best_double?: ComboResult
  best_treble?: ComboResult
  best_four?: ComboResult
}
