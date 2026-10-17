/**
 * 北京时间 (UTC+8) 实时时钟 Hook
 * 每秒自动更新，保障终端显示标准的体彩业务时区时间
 */

import { useState, useEffect } from 'react'

/**
 * 格式化当前北京时间为 "YYYY-MM-DD HH:mm:ss"
 * 无论客户端处于哪个操作系统时区，强制锁定 Asia/Shanghai
 */
export function getBeijingTimeString(date: Date = new Date()): string {
  const formatter = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })

  const parts = formatter.formatToParts(date)
  let year = '2026'
  let month = '01'
  let day = '01'
  let hour = '00'
  let minute = '00'
  let second = '00'

  for (const part of parts) {
    if (part.type === 'year') year = part.value
    else if (part.type === 'month') month = part.value
    else if (part.type === 'day') day = part.value
    else if (part.type === 'hour') hour = part.value
    else if (part.type === 'minute') minute = part.value
    else if (part.type === 'second') second = part.value
  }

  return `${year}-${month}-${day} ${hour}:${minute}:${second}`
}

/**
 * 每秒更新一次的北京时间 Hook
 * @returns 格式化后的北京时间字符串，如 "2026-10-14 00:00:00"
 */
export function useClock(): string {
  const [time, setTime] = useState<string>(() => getBeijingTimeString())

  useEffect(() => {
    setTime(getBeijingTimeString())

    const intervalId = window.setInterval(() => {
      setTime(getBeijingTimeString())
    }, 1000)

    return () => {
      window.clearInterval(intervalId)
    }
  }, [])

  return time
}
