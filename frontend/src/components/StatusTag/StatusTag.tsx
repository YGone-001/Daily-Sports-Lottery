import React from 'react';
import styles from './StatusTag.module.css';

export type MatchStatus = 'upcoming' | 'live' | 'finished';

export interface StatusTagProps {
  status: MatchStatus;
  className?: string;
}

export const StatusTag: React.FC<StatusTagProps> = ({ status, className }) => {
  if (status === 'live') {
    return (
      <span
        className={`${styles.tag} ${styles.tagLive}${className ? ` ${className}` : ''}`}
        role="status"
        aria-label="进行中"
      >
        <span className={styles.pulse} aria-hidden="true" />
        进行中
      </span>
    );
  }

  if (status === 'finished') {
    return (
      <span
        className={`${styles.tag} ${styles.tagDone}${className ? ` ${className}` : ''}`}
        role="status"
        aria-label="完赛"
      >
        完赛
      </span>
    );
  }

  // 默认：upcoming
  return (
    <span
      className={`${styles.tag} ${styles.tagUp}${className ? ` ${className}` : ''}`}
      role="status"
      aria-label="待赛"
    >
      待赛
    </span>
  );
};

export default StatusTag;
