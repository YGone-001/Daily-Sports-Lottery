/**
 * 智能策略与价值投注 TanStack Query 封装
 */

import { useQuery } from '@tanstack/react-query'
import { fetchStrategy } from '@/api/strategy'
import type { StrategyResult } from '@/types/strategy'

export const strategyKeys = {
  all: ['strategy'] as const,
  bySport: (sport?: string) => [...strategyKeys.all, { sport }] as const,
}

/**
 * 查询智能策略推荐（正期望单注与最优串关方案）
 * @param sport 运动类型 (football / basketball)
 */
export function useStrategy(sport?: string) {
  return useQuery<StrategyResult, Error>({
    queryKey: strategyKeys.bySport(sport),
    queryFn: () => fetchStrategy(sport),
    staleTime: 5 * 60 * 1000, // 5 分钟
    refetchOnWindowFocus: true,
  })
}
