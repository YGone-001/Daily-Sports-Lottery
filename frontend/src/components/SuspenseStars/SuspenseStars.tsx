import React from 'react';
import styles from './SuspenseStars.module.css';

export interface SuspenseStarsProps {
  stars: number;
  maxStars?: number;
  showLabel?: boolean;
  label?: string;
  className?: string;
}

export const SuspenseStars: React.FC<SuspenseStarsProps> = ({
  stars,
  maxStars = 5,
  showLabel = false,
  label,
  className,
}) => {
  const safeMax = Math.max(1, maxStars);
  const clampedStars = Math.max(0, Math.min(Math.round(stars), safeMax));
  const emptyStars = safeMax - clampedStars;

  const containerClass = className
    ? `${styles.suspenseWrap} ${className}`
    : styles.suspenseWrap;

  return (
    <span
      className={containerClass}
      title={`悬念指数：${stars}/${safeMax}`}
      aria-label={`悬念指数：${stars}星，满分${safeMax}星`}
    >
      {Array.from({ length: clampedStars }).map((_, index) => (
        <span key={`on-${index}`} className={styles.sOn} aria-hidden="true">
          ★
        </span>
      ))}
      {Array.from({ length: emptyStars }).map((_, index) => (
        <span key={`off-${index}`} className={styles.sOff} aria-hidden="true">
          ★
        </span>
      ))}
      {(showLabel || label) && (
        <span className={styles.sTxt}>
          {label ?? `${stars}星悬念`}
        </span>
      )}
    </span>
  );
};

export default SuspenseStars;
