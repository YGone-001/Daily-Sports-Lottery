import React from 'react';
import { Icon } from '../Icon';
import styles from './SportFilter.module.css';

export interface SportFilterProps {
  current: string;
  onChange: (sport: string) => void;
  showAll?: boolean;
  className?: string;
}

export const SportFilter: React.FC<SportFilterProps> = ({
  current,
  onChange,
  showAll = true,
  className,
}) => {
  const containerClass = className
    ? `${styles.chipRow} ${className}`
    : styles.chipRow;

  const isAll = current === 'all' || current === '';

  return (
    <div className={containerClass} role="group" aria-label="运动项目筛选">
      {showAll && (
        <button
          type="button"
          className={`${styles.chip} ${isAll ? styles.chipOn : ''}`}
          onClick={() => onChange('all')}
          aria-pressed={isAll}
        >
          全部
        </button>
      )}

      <button
        type="button"
        className={`${styles.chip} ${current === 'football' ? styles.chipOn : ''}`}
        onClick={() => onChange('football')}
        aria-pressed={current === 'football'}
      >
        <Icon name="football" className={styles.chipIcon} />
        足球
      </button>

      <button
        type="button"
        className={`${styles.chip} ${current === 'basketball' ? styles.chipOn : ''}`}
        onClick={() => onChange('basketball')}
        aria-pressed={current === 'basketball'}
      >
        <Icon name="basketball" className={styles.chipIcon} />
        篮球
      </button>
    </div>
  );
};

export default SportFilter;
