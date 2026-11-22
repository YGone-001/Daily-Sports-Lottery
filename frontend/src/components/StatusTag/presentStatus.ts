/**
 * 状态呈现的纯函数判定（Workstream E.2 / A.4）
 *
 * 落盘状态是事实，但**呈现**必须区分三种「upcoming」语义，避免误导：
 * - upcoming：开赛时间已知且未来——正常的「待赛」；
 * - kickoff_unknown：开赛时间未知但比赛日在今天或之后——「时间未定」；
 * - unverified：开赛时间未知且比赛日已过、源也未确认赛果——「未核实」。
 * 该判定只作用于展示层，不写库、不改变既有 STORED / DERIVED 语义（AGENTS.md §4.32）。
 */

export type StatusPresentation =
  | 'live'
  | 'finished'
  | 'upcoming'
  | 'kickoff_unknown'
  | 'unverified'

export function presentStatus(
  status: 'upcoming' | 'live' | 'finished',
  kickoffTimeKnown?: boolean,
  matchDate?: string,
  today?: string,
): StatusPresentation {
  if (status === 'live') return 'live'
  if (status === 'finished') return 'finished'
  if (kickoffTimeKnown === false) {
    if (matchDate && today && matchDate < today) return 'unverified'
    return 'kickoff_unknown'
  }
  return 'upcoming'
}

/** 北京时间「今天」的 ISO 日期（YYYY-MM-DD）。 */
export function beijingTodayIso(now: Date = new Date()): string {
  const beijing = new Date(now.getTime() + 8 * 60 * 60 * 1000)
  return beijing.toISOString().slice(0, 10)
}
