import React, { useState, useEffect } from 'react';
import styles from './ProbBar.module.css';

export interface ProbBarProps {
  homeWin: number;
  draw: number;
  awayWin: number;
  animate?: boolean;
  large?: boolean;
  className?: string;
}

export const ProbBar: React.FC<ProbBarProps> = ({
  homeWin,
  draw,
  awayWin,
  animate = true,
  large = false,
  className,
}) => {
  const [isRendered, setIsRendered] = useState(!animate);

  useEffect(() => {
    if (animate) {
      const raf = requestAnimationFrame(() => {
        setIsRendered(true);
      });
      return () => cancelAnimationFrame(raf);
    }
  }, [animate]);

  const total = homeWin + draw + awayWin;

  const homePct = total > 0 ? (homeWin / total) * 100 : 0;
  const drawPct = total > 0 ? (draw / total) * 100 : 0;
  const awayPct = total > 0 ? (awayWin / total) * 100 : 0;

  const currentHomeWidth = isRendered ? `${homePct}%` : '0%';
  const currentDrawWidth = isRendered ? `${drawPct}%` : '0%';
  const currentAwayWidth = isRendered ? `${awayPct}%` : '0%';

  const containerClass = [
    styles.bar,
    large ? styles.largeBar : '',
    className || '',
  ]
    .filter(Boolean)
    .join(' ');

  const accessibleLabel =
    draw > 0
      ? `概率分布：主胜 ${homePct.toFixed(1)}%，平局 ${drawPct.toFixed(1)}%，客胜 ${awayPct.toFixed(1)}%`
      : `概率分布：主胜 ${homePct.toFixed(1)}%，客胜 ${awayPct.toFixed(1)}%`;

  return (
    <div
      className={containerClass}
      role="progressbar"
      aria-label={accessibleLabel}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      {homePct > 0 && (
        <span
          className={`${styles.segment} ${styles.segHome}`}
          style={{ width: currentHomeWidth }}
          title={`主胜: ${homePct.toFixed(1)}%`}
        />
      )}
      {drawPct > 0 && (
        <span
          className={`${styles.segment} ${styles.segDraw}`}
          style={{ width: currentDrawWidth }}
          title={`平局: ${drawPct.toFixed(1)}%`}
        />
      )}
      {awayPct > 0 && (
        <span
          className={`${styles.segment} ${styles.segAway}`}
          style={{ width: currentAwayWidth }}
          title={`客胜: ${awayPct.toFixed(1)}%`}
        />
      )}
    </div>
  );
};

export default ProbBar;
