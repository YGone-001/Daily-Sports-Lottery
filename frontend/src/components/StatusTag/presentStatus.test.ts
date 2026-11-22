import { describe, expect, it } from 'vitest'
import { beijingTodayIso, presentStatus } from './presentStatus'

describe('presentStatus', () => {
  it('keeps live and finished as-is', () => {
    expect(presentStatus('live')).toBe('live')
    expect(presentStatus('finished')).toBe('finished')
  })

  it('shows plain upcoming when kickoff is known', () => {
    expect(presentStatus('upcoming', true, '2026-11-23', '2026-11-22')).toBe('upcoming')
    expect(presentStatus('upcoming', undefined, '2026-11-23', '2026-11-22')).toBe('upcoming')
  })

  it('shows kickoff_unknown for a future fixture with unknown kickoff', () => {
    expect(presentStatus('upcoming', false, '2026-11-24', '2026-11-22')).toBe('kickoff_unknown')
    expect(presentStatus('upcoming', false, '2026-11-22', '2026-11-22')).toBe('kickoff_unknown')
  })

  it('shows unverified for a past-date fixture that was never confirmed', () => {
    expect(presentStatus('upcoming', false, '2026-10-14', '2026-11-22')).toBe('unverified')
  })

  it('treats unknown date as kickoff_unknown rather than unverified', () => {
    expect(presentStatus('upcoming', false, undefined, '2026-11-22')).toBe('kickoff_unknown')
  })
})

describe('beijingTodayIso', () => {
  it('converts a UTC instant to the Beijing calendar date', () => {
    // 2026-11-22 17:00 UTC = 2026-11-23 01:00 北京
    expect(beijingTodayIso(new Date('2026-11-22T17:00:00Z'))).toBe('2026-11-23')
    // 2026-11-22 15:00 UTC = 2026-11-22 23:00 北京
    expect(beijingTodayIso(new Date('2026-11-22T15:00:00Z'))).toBe('2026-11-22')
  })
})
