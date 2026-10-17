/**
 * 抓取刷新 Mutation 封装
 * 调用 /api/refresh 并自动失效关联的缓存数据
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { triggerRefresh } from '@/api/matches'
import { matchKeys } from './useMatches'
import { strategyKeys } from './useStrategy'
import type { RefreshResult } from '@/types/match'

/**
 * 触发后端全量抓取更新并重载比赛与策略缓存
 */
export function useRefresh() {
  const queryClient = useQueryClient()

  return useMutation<RefreshResult, Error, void>({
    mutationFn: () => triggerRefresh(),
    onSuccess: () => {
      // 刷新成功后，批量失效比赛、日期、策略与系统状态缓存
      queryClient.invalidateQueries({ queryKey: matchKeys.all })
      queryClient.invalidateQueries({ queryKey: matchKeys.dates })
      queryClient.invalidateQueries({ queryKey: matchKeys.status })
      queryClient.invalidateQueries({ queryKey: strategyKeys.all })
    },
  })
}
