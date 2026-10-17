/**
 * 赛事与预测数据类型定义
 * 对应 Flask 后端数据模型与 API 响应结构
 */

// 核心比赛数据结构，对应 /api/matches 与 /api/today
export interface Match {
  id: string
  sport: 'football' | 'basketball'
  league: string
  date: string        // "2026-10-13"
  time: string        // "20:00"
  home: string
  away: string
  home_rank?: number
  away_rank?: number
  status: 'upcoming' | 'live' | 'finished'
  score?: MatchScore
  odds?: Odds
  prediction?: Prediction
  round?: string
  home_team?: TeamInfo
  away_team?: TeamInfo
  jczq_no?: string
  market_tracked?: boolean
}

export interface MatchScore {
  ft?: [number, number]
  ht?: [number, number]
}

export interface Odds {
  home_win: number
  draw: number
  away_win: number
  over_2_5?: number
  under_2_5?: number
  handicap_line?: number
  total_line?: number
  matchnum?: string
}

export interface TeamInfo {
  name: string
  code: string
  flag: string
  strength: number
  elo_rating?: number
  rank?: number
  estimated?: boolean
}

export interface EVDetail {
  ev: number
  is_value: boolean
  kelly_pct: number
}

export interface ScoreEntry {
  score: string
  probability: number
}

export interface GoalsPrediction {
  most_likely: number
  over_2_5: number
  under_2_5: number
}

export interface BasketballDetail {
  expected_home_points: number
  expected_away_points: number
  expected_total: number
  spread: number
  over_line: number
  over_prob: number
  under_prob: number
}

export interface RadarData {
  labels: string[]
  home: number[]
  away: number[]
}

export interface KeyInfo {
  home_notes: string[]
  away_notes: string[]
  home_injuries: string[]
  away_injuries: string[]
  home_key_players: string[]
  away_key_players: string[]
  home_rest_days: number
  away_rest_days: number
}

export interface Prediction {
  sport: string
  home_team: TeamInfo
  away_team: TeamInfo
  probabilities: { home_win: number; draw: number; away_win: number }
  model_probabilities: { home_win: number; draw: number; away_win: number }
  market: {
    odds: Odds
    implied_probabilities: { home_win: number | null; draw: number | null; away_win: number | null }
    weight: number
  }
  ev_analysis: Record<'home' | 'draw' | 'away', EVDetail>
  top_scores?: ScoreEntry[]
  expected_goals?: { home: number; away: number }
  goals_prediction?: GoalsPrediction
  suspense: { stars: number; label: string }
  basketball?: BasketballDetail
  radar: RadarData
  key_info: KeyInfo
}

export interface MatchGroup {
  league: string
  sport: 'football' | 'basketball'
  matches: Match[]
}

export interface DailyStats {
  total: number
  live: number
  upcoming: number
  finished: number
  with_odds: number
}

export interface Meta {
  last_refresh: string
  match_count?: number
  total?: number
  sources?: Record<string, number>
  [key: string]: unknown
}

// 赛前预测快照（对应 /api/match/:id/snapshots）
export interface MatchSnapshot {
  snapshot_id: string
  match_id: string
  sport: string
  league: string
  home_team: string
  away_team: string
  match_date: string
  match_time: string
  kickoff_at?: string | null
  generated_at?: string | null
  model_name: string
  model_version: string
  home_elo?: number | null
  away_elo?: number | null
  model_probabilities: { home_win?: number; draw?: number; away_win?: number }
  display_probabilities: { home_win?: number; draw?: number; away_win?: number }
  expected_values: Record<string, EVDetail>
  market_odds: Odds
  market_implied_probabilities: Record<string, number | null>
  expected_score_data?: unknown
}

// 赔率历史记录（对应 /api/match/:id/odds-history）
export interface OddsHistoryItem {
  snapshot_id: string
  match_id: string
  sport: string
  league: string
  home_team: string
  away_team: string
  match_date: string
  match_time: string
  kickoff_at?: string | null
  captured_at: string
  source: string
  jczq_no?: string
  odds: Odds
  odds_fingerprint: string
}

// 结算记录（对应 /api/match/:id/settlements）
export interface SettlementRecord {
  settlement_id: string
  snapshot_id: string
  match_id: string
  sport: string
  league: string
  home_team: string
  away_team: string
  model_name: string
  model_version: string
  prediction_generated_at?: string | null
  kickoff_at?: string | null
  settled_at: string
  final_score?: { home: number; away: number }
  actual_outcome?: 'home_win' | 'draw' | 'away_win'
  result_fingerprint?: string
  is_valid_prematch?: boolean
}

// 可用日期与联赛列表响应（对应 /api/dates）
export interface DatesResponse {
  dates: string[]
  leagues: {
    football: string[]
    basketball: string[]
  }
}

// 系统状态（对应 /api/status）
export interface SystemStatus {
  meta: Meta
  dates: string[]
  team_pool: {
    total: number
    estimated: number
    calibrated: number
  }
  today_stats: DailyStats
}

export type HistoryEvidenceStatus =
  | 'verified'
  | 'historical_version'
  | 'no_evidence'
  | 'ambiguous'
  | 'invalid'

export interface HistoryComparison {
  actual_score: string
  actual_outcome: string
  actual_label: string
  evidence_status: HistoryEvidenceStatus
  is_verified: boolean
  model_name: string
  model_version: string
  prediction_generated_at: string
  kickoff_at: string
  predicted_outcome: string
  predicted_label: string
  confidence: number | null
  correct: boolean | null
  score_eligible: boolean
  score_hit: boolean | null
  top_candidates: string[]
}

export interface HistoryMatch extends Match {
  comparison: HistoryComparison
  evidence_status: HistoryEvidenceStatus
  is_verified: boolean
}

export interface HistorySportStats {
  total_finished: number
  verified_evaluations: number
  historical_evaluations: number
  no_evidence_matches: number
  ambiguous_evaluations: number
  unverified_matches: number
  verified_hits: number
  accuracy_pct: number | null
  accuracy_str: string
  ratio_str: string
  score_eligible: number
  score_hits: number
  score_coverage_pct: number | null
  score_coverage_str: string
  score_ratio_str: string
  model_version: string
}

export interface HistoryStats {
  total_finished: number
  verified_evaluations: number
  historical_version_evaluations: number
  no_evidence_matches: number
  ambiguous_evaluations: number
  unverified_matches: number
  football: HistorySportStats
  basketball: HistorySportStats
}

export interface HistoryResponse {
  matches: HistoryMatch[]
  total_finished: number
  visible_count: number
  current_sport: 'all' | 'football' | 'basketball'
  stats: HistoryStats
}

// 抓取刷新结果（对应 /api/refresh）
export interface RefreshResult {
  status?: string
  message?: string
  added?: number
  updated?: number
  calibrated?: number
  odds_snapshots_added?: number
  prediction_snapshots_added?: number
  settlements_added?: number
  evaluation_rows_added?: number
  total?: number
  finished_at?: string
  football?: number
  basketball?: number
  [key: string]: unknown
}
