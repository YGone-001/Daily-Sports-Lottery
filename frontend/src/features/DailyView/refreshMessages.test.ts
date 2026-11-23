import { describe, expect, it } from 'vitest'
import { ApiClientError } from '@/api/client'
import type { RuntimeCapability } from '@/types/match'
import { describeOperatingState, describeRefreshError } from './refreshMessages'

function runtime(overrides: Partial<RuntimeCapability>): RuntimeCapability {
  return {
    runtime_writes_enabled: false,
    background_refresh_enabled: false,
    local_ui_refresh_enabled: false,
    refresh_available_via_ui: false,
    ...overrides,
  }
}

describe('operating state messages', () => {
  it('distinguishes read-only application from local refresh availability', () => {
    const readOnly = describeOperatingState(runtime({ operating_state: 'READ_ONLY_APPLICATION' }))
    expect(readOnly?.state).toBe('READ_ONLY_APPLICATION')
    expect(readOnly?.text).toContain('只读运行')
    expect(readOnly?.text).toContain('--refresh')

    const available = describeOperatingState(
      runtime({
        runtime_writes_enabled: true,
        local_ui_refresh_enabled: true,
        refresh_available_via_ui: true,
        operating_state: 'LOCAL_REFRESH_AVAILABLE',
      }),
    )
    expect(available).toBeNull()
  })

  it('explains token-only refresh without claiming the UI can write', () => {
    const notice = describeOperatingState(runtime({ operating_state: 'TOKEN_REQUIRED' }))
    expect(notice?.state).toBe('TOKEN_REQUIRED')
    expect(notice?.text).toContain('刷新令牌')
    expect(notice?.text).toContain('界面不提供同步按钮')
  })

  it('reports in-progress and completed with advisory wording', () => {
    const progress = describeOperatingState(
      runtime({
        operating_state: 'REFRESH_IN_PROGRESS',
        last_refresh: {
          status: 'IN_PROGRESS',
          generation: 12,
          started_at: '2030-01-01T10:00:00+08:00',
          finished_at: null,
          error: null,
          marker_error: null,
          advisory: true,
        },
      }),
    )
    expect(progress?.tone).toBe('progress')
    expect(progress?.text).toContain('同步正在进行')

    const completed = describeOperatingState(
      runtime({
        operating_state: 'REFRESH_COMPLETED',
        last_refresh: {
          status: 'COMPLETED',
          generation: 12,
          started_at: '2030-01-01T10:00:00+08:00',
          finished_at: '2030-01-01T10:01:00+08:00',
          error: null,
          marker_error: null,
          advisory: true,
        },
      }),
    )
    expect(completed?.tone).toBe('success')
    expect(completed?.text).toContain('第 12 代')
    expect(completed?.text).toContain('顾问性观测')
  })

  it('surfaces refresh failure with its recorded reason', () => {
    const notice = describeOperatingState(
      runtime({
        operating_state: 'REFRESH_FAILED',
        last_refresh: {
          status: 'FAILED',
          generation: 13,
          started_at: null,
          finished_at: '2030-01-01T10:00:00+08:00',
          error: 'CanonicalIdentityCollisionError',
          marker_error: null,
          advisory: true,
        },
      }),
    )
    expect(notice?.tone).toBe('error')
    expect(notice?.text).toContain('上次同步失败')
    expect(notice?.text).toContain('CanonicalIdentityCollisionError')
  })

  it('returns null when the backend does not report an operating state', () => {
    expect(describeOperatingState(undefined)).toBeNull()
    expect(describeOperatingState(runtime({}))).toBeNull()
  })
})

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
