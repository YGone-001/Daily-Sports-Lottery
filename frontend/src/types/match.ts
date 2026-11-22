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
  kickoff_time_known?: boolean
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
  /** 按运动分组的比赛日：选择「篮球」时不应默认落到只有足球的日期 */
  dates_by_sport?: {
    football: string[]
    basketball: string[]
  }
  leagues: {
    football: string[]
    basketball: string[]
  }
}

// 系统状态（对应 /api/status）
export interface RuntimeCapability {
  runtime_writes_enabled: boolean
  background_refresh_enabled: boolean
  local_ui_refresh_enabled: boolean
  /** 主写开关与本地 UI 开关同时开启时，界面同步才可用（不含任何令牌信息）。 */
  refresh_available_via_ui: boolean
}

export interface SystemStatus {
  meta: Meta
  dates: string[]
  team_pool: {
    total: number
    estimated: number
    calibrated: number
  }
  today_stats: DailyStats
  runtime?: RuntimeCapability
  frontend_build_identity?: {
    index_sha256: string
    entry_asset: string | null
  } | null
  freshness?: FreshnessSummary
}

export interface SportFreshness {
  stored_upcoming_count: number
  verified_future_count: number
  stale_upcoming_count: number
  /** Workstream D：互斥且完备的待赛四分区 + 提供商核实二分。 */
  known_future_kickoff: number
  future_kickoff_unknown: number
  past_kickoff_stored_upcoming: number
  unknown_kickoff_past_date: number
  /** 仅统计携带 kickoff_provenance = SCHEDULE_PROVIDER_VERIFIED 的记录。 */
  provider_schedule_verified: number
  provider_schedule_unverified: number
  latest_market_event_date: string | null
}

export interface FreshnessSummary {
  generated_at: string
  per_sport: Record<string, SportFreshness>
  note?: string
}

export interface MarketBaselineComparison {
  status: 'VERIFIED' | 'MARKET_BASELINE_NOT_AVAILABLE'
  /** Workstream D.1：计算完成 ≠ 历史市场方向已独立核实。 */
  computation_status?: 'CALCULATION_COMPLETE' | 'NO_ELIGIBLE_ROWS'
  market_source_identity?: 'MARKET_SOURCE_IDENTITY_VERIFIED' | 'MARKET_SOURCE_IDENTITY_UNRESOLVED'
  independent_orientation_check?: 'INDEPENDENT_ORIENTATION_CHECK_PENDING' | 'COMPLETED'
  same_fixture_samples: number
  model_direction_accuracy?: number
  market_direction_accuracy?: number
  model_brier?: number
  market_brier?: number
}

export interface EvaluationModelSummary {
  sport: 'football' | 'basketball'
  model_name: string
  model_version: string
  sample_count: number
  accuracy: number
  brier_score: number
  multiclass_log_loss: number
  macro_expected_calibration_error: number | null
  snapshot_date_from: string
  snapshot_date_to: string
  market_baseline: MarketBaselineComparison
}

export interface EvaluationSummary {
  generated_at: string
  probability_source: string
  convention: { brier: string; logloss: string; note: string }
  models: EvaluationModelSummary[]
}

export type HistoryEvidenceStatus =
  | 'verified'
  | 'historical_version'
  | 'no_evidence'
  | 'ambiguous'
  | 'invalid'

export type HistoryNoEvidenceReason =
  | 'NO_SNAPSHOT_EVIDENCE'
  | 'SETTLEMENT_WITHOUT_SNAPSHOT'
  | 'SNAPSHOT_PRESENT_SETTLEMENT_MISSING'
  | 'SETTLEMENT_PRESENT_EVALUATION_UNVERIFIED'

export type LifecyclePresentation =
  | 'SOURCE_CONFIRMED_FINISHED'
  | 'LIVE_WITH_TERMINAL_RESULT_EVIDENCE'
  | 'LIVE_WITHOUT_TERMINAL_RESULT_EVIDENCE'
  | 'FINAL_RESULT_UNVERIFIED'
  | 'HISTORICAL_EVIDENCE_CONFLICT'

export interface HistoryComparison {
  actual_score: string
  actual_outcome: string
  actual_label: string
  evidence_status: HistoryEvidenceStatus
  /** no_evidence 时的缺失证据形态（Workstream C.4 区分措辞）。 */
  no_evidence_reason?: HistoryNoEvidenceReason
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
  lifecycle_presentation?: LifecyclePresentation
  state_inconsistency_warning?: boolean
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
