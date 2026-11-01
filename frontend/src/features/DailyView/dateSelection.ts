/**
 * 运动感知的日期选择（纯函数，便于单元测试）。
 *
 * 背景：`/api/dates` 过去只返回全局日期列表，前端无脑取 `dates[0]`。
 * 当最新日期只有足球时，切到「篮球」会显示一个必然为空的日期，
 * 把「当日没有篮球」误报成「没有比赛」——即使更早的日期确实有 NBA。
 */

export type Sport = 'football' | 'basketball'

export interface DatesBySport {
  football: string[]
  basketball: string[]
}

/**
 * 返回当前运动可用的日期列表（降序）。
 *
 * - `sport === 'all'` 或缺少分组数据时，退回全局列表；
 * - 该运动没有任何日期时也退回全局列表（宁可让用户看到日期，也不要空选择器）。
 */
export function availableDatesFor(
  sport: string,
  allDates: string[],
  datesBySport?: DatesBySport
): string[] {
  if (sport !== 'football' && sport !== 'basketball') {
    return allDates
  }
  const own = datesBySport?.[sport as Sport] ?? []
  return own.length > 0 ? own : allDates
}

/**
 * 解析真正用于查询的日期。
 *
 * 显式 `date` 只在它属于当前运动的可用日期时保留，否则回落到该运动最新的日期。
 */
export function resolveDisplayDate(
  rawDate: string | undefined,
  availableDates: string[]
): string | undefined {
  if (rawDate && availableDates.includes(rawDate)) {
    return rawDate
  }
  return availableDates[0]
}

/**
 * 换运动后是否需要清掉已选日期（该日期在新运动下没有比赛）。
 */
export function shouldClearDate(
  selected: string | null,
  sport: string,
  allDates: string[],
  datesBySport?: DatesBySport
): boolean {
  if (!selected) {
    return false
  }
  const next = availableDatesFor(sport, allDates, datesBySport)
  return next.length > 0 && !next.includes(selected)
}