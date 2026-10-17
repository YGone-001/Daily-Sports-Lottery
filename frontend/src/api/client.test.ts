import { describe, expect, it } from 'vitest'
import { buildQueryString } from './client'

describe('buildQueryString', () => {
  it('keeps meaningful filters while omitting absent values', () => {
    expect(
      buildQueryString({
        date: '2026-10-14',
        sport: 'football',
        page: undefined,
        includeFinished: false,
        empty: '',
      })
    ).toBe('?date=2026-10-14&sport=football&includeFinished=false')
  })
})
