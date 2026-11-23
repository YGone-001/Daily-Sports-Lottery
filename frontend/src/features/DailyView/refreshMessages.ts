/**
 * 同步（刷新）结果的用户可见反馈
 *
 * 服务端对 POST /api/refresh 的拒绝是**明确状态**，不是静默失败：
 * - 403 RUNTIME_WRITES_DISABLED：部署为默认只读，运行时写开关未开启；
 * - 403 REFRESH_AUTHORIZATION_REQUIRED：主写开关已开，但该调用方未持有效刷新令牌；
 * - 409 busy：另一刷新正在进行（刷新锁被占用）。
 * 界面必须把这三类结果与网络故障分开陈述，不得让按钮悄悄回到空闲状态。
 */

import { ApiClientError } from '@/api/client'
import type { OperatingState, RuntimeCapability } from '@/types/match'

export interface OperatingNotice {
  state: OperatingState
  tone: 'info' | 'success' | 'warning' | 'error' | 'progress'
  text: string
}

/**
 * 把后端权威的运行状态翻译成可见提示。
 *
 * - 后端是权限判定的唯一来源：`mode` / `operating_state` 都由 `/api/status` 给出，
 *   浏览器请求头或按钮状态不构成授权。
 * - `LOCAL_REFRESH_AVAILABLE` 不需要常驻提示（按钮本身已表明可用）。
 * - 刷新标记只是**顾问性**观测，`REFRESH_COMPLETED` 不代表七库事务快照。
 */
export function describeOperatingState(runtime?: RuntimeCapability): OperatingNotice | null {
  if (!runtime) return null
  const state = runtime.operating_state
  if (!state) return null
  const last = runtime.last_refresh
  const generation =
    typeof last?.generation === 'number' ? `第 ${last.generation} 代` : '上一次刷新'
  const finished = last?.finished_at ? `，完成于 ${last.finished_at}` : ''

  switch (state) {
    case 'REFRESH_IN_PROGRESS':
      return {
        state,
        tone: 'progress',
        text: '同步正在进行：完成后页面会自动更新，请勿重复提交。',
      }
    case 'REFRESH_FAILED':
      return {
        state,
        tone: 'error',
        text: `上次同步失败（${generation}）：${last?.error || '原因未记录'}。请检查服务端日志后重试。`,
      }
    case 'REFRESH_COMPLETED':
      return {
        state,
        tone: 'success',
        text: `同步已完成（${generation}${finished}）。刷新标记是顾问性观测，不代表七个库是事务快照。`,
      }
    case 'READ_ONLY_APPLICATION':
      return {
        state,
        tone: 'info',
        text: '只读运行：启动应用不会自动写入生产数据，界面同步未启用。需要同步请使用 --refresh 启动。',
      }
    case 'TOKEN_REQUIRED':
      return {
        state,
        tone: 'info',
        text: '主写开关已开启，但本地界面同步未启用：界面不提供同步按钮，程序化刷新需要有效的刷新令牌。',
      }
    case 'LOCAL_REFRESH_AVAILABLE':
    default:
      return null
  }
}

export function describeRefreshError(error: unknown): string {
  if (error instanceof ApiClientError) {
    if (error.status === 403 && error.message.includes('RUNTIME_WRITES_DISABLED')) {
      return '同步未启用：当前部署为只读模式，服务端已拒绝刷新。如需从界面同步，需在服务端显式开启运行时写开关与本地界面开关（见 docs/runtime-readonly.md）。'
    }
    if (error.status === 403 && error.message.includes('REFRESH_AUTHORIZATION_REQUIRED')) {
      return '同步未授权：该请求未携带有效的刷新令牌，服务端已拒绝执行刷新。'
    }
    if (error.status === 409) {
      return '已有同步正在进行（刷新锁被占用），请等待其完成后再试。'
    }
    if (error.status === 0) {
      return '网络请求失败：无法连接到服务，请确认服务正在运行后重试。'
    }
    return `同步失败：${error.message}`
  }
  return '同步失败：未知错误，请稍后重试。'
}
