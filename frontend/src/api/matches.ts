/**
 * 赛事与系统状态相关 API
 * 对应 Flask /api/matches, /api/match/:id, /api/dates, /api/refresh, /api/status 等
 */

import { apiClient } from './client'
import type {
  Match,
  HistoryResponse,
  MatchSnapshot,
  OddsHistoryItem,
  SettlementRecord,
  DatesResponse,
  SystemStatus,
  RefreshResult,
} from '@/types/match'

/**
 * 获取赛事列表
 * @param date 比赛日（YYYY-MM-DD 或 "all"，不传默认取后端逻辑今日）
 * @param sport 运动类型（"football" | "basketball" | "all"）
 */
export async function fetchMatches(
  date?: string,
  sport?: string
): Promise<Match[]> {
  return apiClient.get<Match[]>('/api/matches', { date, sport })
}

/**
 * 获取今日实时赛事列表
 * @param sport 运动类型（"football" | "basketball"）
 */
export async function fetchToday(sport?: string): Promise<Match[]> {
  return apiClient.get<Match[]>('/api/today', { sport })
}

/**
 * 获取单场比赛详情（含预测、Elo评级与盘口分析）
 * @param id 比赛 ID（如 500l-2026-10-13-3001）
 */
export async function fetchMatch(id: string): Promise<Match> {
  return apiClient.get<Match>(`/api/match/${encodeURIComponent(id)}`)
}

/**
 * 只读：获取单场比赛已固化的赛前预测快照历史
 * @param id 比赛 ID
 */
export async function fetchMatchSnapshots(
  id: string
): Promise<MatchSnapshot[]> {
  return apiClient.get<MatchSnapshot[]>(
    `/api/match/${encodeURIComponent(id)}/snapshots`
  )
}

/**
 * 只读：获取单场比赛赛前赔率变动历史（按时间升序）
 * @param id 比赛 ID
 */
export async function fetchMatchOddsHistory(
  id: string
): Promise<OddsHistoryItem[]> {
  return apiClient.get<OddsHistoryItem[]>(
    `/api/match/${encodeURIComponent(id)}/odds-history`
  )
}

/**
 * 只读：获取单场比赛全部事实性结算记录
 * @param id 比赛 ID
 */
export async function fetchMatchSettlements(
  id: string
): Promise<SettlementRecord[]> {
  return apiClient.get<SettlementRecord[]>(
    `/api/match/${encodeURIComponent(id)}/settlements`
  )
}

/**
 * 获取全部可用比赛日与各运动涉及的联赛列表
 */
export async function fetchDates(): Promise<DatesResponse> {
  return apiClient.get<DatesResponse>('/api/dates')
}

/**
 * 手动触发后台即时抓取刷新（500.com 数据源与盘口校准）
 */
export async function triggerRefresh(): Promise<RefreshResult> {
  return apiClient.post<RefreshResult>('/api/refresh')
}

/**
 * 获取系统当前健康状态、元数据与球队池统计
 */
export async function fetchStatus(): Promise<SystemStatus> {
  return apiClient.get<SystemStatus>('/api/status')
}

/**
 * 获取历史复盘的不可变赛前证据视图。
 * 不使用当前模型重算历史预测，结果与 Flask /history 页面一致。
 */
export async function fetchHistory(sport?: string): Promise<HistoryResponse> {
  return apiClient.get<HistoryResponse>('/api/history', { sport })
}
