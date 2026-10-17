/**
 * 赛事数据 TanStack Query 封装
 * 提供比赛列表、详情、历史快照、结算与日期的缓存管理
 */

import { useQuery } from '@tanstack/react-query'
import {
  fetchMatches,
  fetchMatch,
  fetchDates,
  fetchMatchSnapshots,
  fetchMatchOddsHistory,
  fetchMatchSettlements,
  fetchStatus,
  fetchHistory,
} from '@/api/matches'
import type {
  Match,
  DatesResponse,
  MatchSnapshot,
  OddsHistoryItem,
  SettlementRecord,
  SystemStatus,
  HistoryResponse,
} from '@/types/match'

/**
 * 统一的查询键管理，方便缓存控制与失效重载
 */
export const matchKeys = {
  all: ['matches'] as const,
  lists: () => [...matchKeys.all, 'list'] as const,
  list: (date?: string, sport?: string) =>
    [...matchKeys.lists(), { date, sport }] as const,
  details: () => [...matchKeys.all, 'detail'] as const,
  detail: (id: string) => [...matchKeys.details(), id] as const,
  snapshots: (id: string) => [...matchKeys.all, 'snapshots', id] as const,
  oddsHistory: (id: string) => [...matchKeys.all, 'odds-history', id] as const,
  settlements: (id: string) => [...matchKeys.all, 'settlements', id] as const,
  dates: ['dates'] as const,
  status: ['status'] as const,
  history: (sport?: string) => ['history', { sport }] as const,
}

/**
 * 查询指定日期与类别的比赛列表
 * @param date 比赛日 (YYYY-MM-DD 或 "all")
 * @param sport 运动类型 (football / basketball / all)
 */
export function useMatches(date?: string, sport?: string) {
  return useQuery<Match[], Error>({
    queryKey: matchKeys.list(date, sport),
    queryFn: () => fetchMatches(date, sport),
    staleTime: 5 * 60 * 1000, // 5 分钟
    refetchOnWindowFocus: true,
  })
}

/**
 * 查询单场比赛深度分析数据
 * @param id 比赛 ID
 */
export function useMatch(id: string) {
  return useQuery<Match, Error>({
    queryKey: matchKeys.detail(id),
    queryFn: () => fetchMatch(id),
    enabled: Boolean(id),
    staleTime: 5 * 60 * 1000, // 5 分钟
  })
}

/**
 * 查询系统可用日期与联赛分布
 */
export function useDates() {
  return useQuery<DatesResponse, Error>({
    queryKey: matchKeys.dates,
    queryFn: fetchDates,
    staleTime: 10 * 60 * 1000, // 10 分钟
  })
}

/**
 * 查询单场比赛赛前固化预测快照
 * @param id 比赛 ID
 */
export function useMatchSnapshots(id: string) {
  return useQuery<MatchSnapshot[], Error>({
    queryKey: matchKeys.snapshots(id),
    queryFn: () => fetchMatchSnapshots(id),
    enabled: Boolean(id),
    staleTime: 5 * 60 * 1000,
  })
}

/**
 * 查询单场比赛赛前赔率变动历史
 * @param id 比赛 ID
 */
export function useMatchOddsHistory(id: string) {
  return useQuery<OddsHistoryItem[], Error>({
    queryKey: matchKeys.oddsHistory(id),
    queryFn: () => fetchMatchOddsHistory(id),
    enabled: Boolean(id),
    staleTime: 5 * 60 * 1000,
  })
}

/**
 * 查询单场比赛赛后事实性结算记录
 * @param id 比赛 ID
 */
export function useMatchSettlements(id: string) {
  return useQuery<SettlementRecord[], Error>({
    queryKey: matchKeys.settlements(id),
    queryFn: () => fetchMatchSettlements(id),
    enabled: Boolean(id),
    staleTime: 5 * 60 * 1000,
  })
}

/**
 * 查询系统运行状态与球队池统计
 */
export function useStatus() {
  return useQuery<SystemStatus, Error>({
    queryKey: matchKeys.status,
    queryFn: fetchStatus,
    staleTime: 2 * 60 * 1000, // 2 分钟
  })
}

/** 查询与后端历史页完全一致的不可变证据视图。 */
export function useHistory(sport?: string) {
  return useQuery<HistoryResponse, Error>({
    queryKey: matchKeys.history(sport),
    queryFn: () => fetchHistory(sport),
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: true,
  })
}
