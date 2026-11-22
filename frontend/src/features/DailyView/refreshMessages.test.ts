import { describe, expect, it } from 'vitest'
import { ApiClientError } from '@/api/client'
import { describeRefreshError } from './refreshMessages'

describe('refresh error messages', () => {
  it('explains runtime writes disabled as an explicit denial', () => {
    const err = new ApiClientError('RUNTIME_WRITES_DISABLED', 403, 'FORBIDDEN')
    const msg = describeRefreshError(err)
    expect(msg).toContain('同步未启用')
    expect(msg).toContain('只读模式')
  })

  it('explains missing refresh token as unauthorized', () => {
    const err = new ApiClientError('REFRESH_AUTHORIZATION_REQUIRED', 403, 'FORBIDDEN')
    const msg = describeRefreshError(err)
    expect(msg).toContain('同步未授权')
    expect(msg).toContain('刷新令牌')
  })

  it('explains 409 busy as a concurrent refresh', () => {
    const err = new ApiClientError('Refresh in progress', 409, 'CONFLICT')
    const msg = describeRefreshError(err)
    expect(msg).toContain('刷新锁被占用')
  })

  it('explains network failure separately from server denial', () => {
    const err = new ApiClientError('网络请求失败，请检查网络连接', 0, 'NETWORK_ERROR')
    const msg = describeRefreshError(err)
    expect(msg).toContain('网络请求失败')
    expect(msg).not.toContain('只读模式')
  })

  it('falls back to the server message for other statuses', () => {
    const err = new ApiClientError('SOME_OTHER_ERROR', 500, 'INTERNAL')
    expect(describeRefreshError(err)).toBe('同步失败：SOME_OTHER_ERROR')
  })

  it('handles non-ApiClientError values', () => {
    expect(describeRefreshError(new Error('boom'))).toContain('未知错误')
    expect(describeRefreshError(undefined)).toContain('未知错误')
  })
})
