import { describe, expect, it } from 'vitest'
import {
  availableDatesFor,
  resolveDisplayDate,
  shouldClearDate,
  type DatesBySport,
} from './dateSelection'

const allDates = ['2026-11-03', '2026-11-02', '2026-11-01']
const datesBySport: DatesBySport = {
  football: ['2026-11-03', '2026-11-02', '2026-11-01'],
  basketball: ['2026-11-01', '2026-10-30'],
}

describe('basketball date selection', () => {
  it('does not default to a football-only date when basketball is selected', () => {
    const dates = availableDatesFor('basketball', allDates, datesBySport)
    expect(dates[0]).toBe('2026-11-01')
    expect(dates).not.toContain('2026-11-03')
    expect(resolveDisplayDate(undefined, dates)).toBe('2026-11-01')
  })

  it('keeps an explicit date only when that sport has matches on it', () => {
    const dates = availableDatesFor('basketball', allDates, datesBySport)
    expect(resolveDisplayDate('2026-11-01', dates)).toBe('2026-11-01')
    // 2026-11-03 只有足球：篮球视图不得停在这一天
    expect(resolveDisplayDate('2026-11-03', dates)).toBe('2026-11-01')
  })

  it('falls back to the global list when the sport has no dates at all', () => {
    const empty: DatesBySport = { football: allDates, basketball: [] }
    expect(availableDatesFor('basketball', allDates, empty)).toEqual(allDates)
    expect(availableDatesFor('all', allDates, datesBySport)).toEqual(allDates)
  })

  it('clears a stale date when switching sport', () => {
    expect(shouldClearDate('2026-11-03', 'basketball', allDates, datesBySport)).toBe(true)
    expect(shouldClearDate('2026-11-01', 'basketball', allDates, datesBySport)).toBe(false)
    expect(shouldClearDate(null, 'basketball', allDates, datesBySport)).toBe(false)
    expect(shouldClearDate('2026-11-03', 'all', allDates, datesBySport)).toBe(false)
  })
})