/**
 * 智能策略与价值投注相关 API
 * 对应 Flask /api/strategy
 */

import { apiClient } from './client'
import type { StrategyResult } from '@/types/strategy'

/**
 * 获取智能策略推荐（包含正期望单注、最佳2串1/3串1/4串1串关组合）
 * @param sport 运动类型（"football" | "basketball"，默认 "football"）
 */
export async function fetchStrategy(sport?: string): Promise<StrategyResult> {
  return apiClient.get<StrategyResult>('/api/strategy', { sport })
}
