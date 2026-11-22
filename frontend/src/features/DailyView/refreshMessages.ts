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
