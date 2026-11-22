import React from 'react';
import styles from './StatusTag.module.css';
import { beijingTodayIso, presentStatus, type StatusPresentation } from './presentStatus';

export type MatchStatus = 'upcoming' | 'live' | 'finished';

export interface StatusTagProps {
  status: MatchStatus;
  /** 落盘的开赛时间已知性；未知时不得把赛事呈现为正常「待赛」。 */
  kickoffTimeKnown?: boolean;
  /** 落盘比赛日（YYYY-MM-DD），用于区分「时间未定」与「未核实」。 */
  matchDate?: string;
  className?: string;
}

const LABELS: Record<StatusPresentation, { text: string; aria: string; cls: keyof typeof styles }> = {
  live: { text: '进行中', aria: '进行中', cls: 'tagLive' },
  finished: { text: '完赛', aria: '完赛', cls: 'tagDone' },
  upcoming: { text: '待赛', aria: '待赛', cls: 'tagUp' },
  kickoff_unknown: { text: '时间未定', aria: '开赛时间未定', cls: 'tagUnset' },
  unverified: { text: '未核实', aria: '赛果未核实', cls: 'tagUnverified' },
};

export const StatusTag: React.FC<StatusTagProps> = ({
  status,
  kickoffTimeKnown,
  matchDate,
  className,
}) => {
  const presentation = presentStatus(status, kickoffTimeKnown, matchDate, beijingTodayIso());
  const label = LABELS[presentation];
  return (
    <span
      className={`${styles.tag} ${styles[label.cls]}${className ? ` ${className}` : ''}`}
      role="status"
      aria-label={label.aria}
    >
      {presentation === 'live' && <span className={styles.pulse} aria-hidden="true" />}
      {label.text}
    </span>
  );
};

export default StatusTag;
