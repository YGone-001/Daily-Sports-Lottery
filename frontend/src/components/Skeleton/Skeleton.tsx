import React from 'react';
import styles from './Skeleton.module.css';

export interface SkeletonProps {
  lines?: number;
  height?: string | number;
  width?: string | number;
  circle?: boolean;
  className?: string;
  style?: React.CSSProperties;
}

export const Skeleton: React.FC<SkeletonProps> = ({
  lines = 1,
  height,
  width,
  circle = false,
  className,
  style,
}) => {
  const formatDimension = (dim?: string | number, fallback?: string): string => {
    if (dim === undefined) return fallback || '100%';
    return typeof dim === 'number' ? `${dim}px` : dim;
  };

  const baseHeight = formatDimension(height, circle ? '36px' : '16px');
  const baseWidth = formatDimension(width, circle ? baseHeight : '100%');

  if (circle || lines <= 1) {
    const singleStyle: React.CSSProperties = {
      height: baseHeight,
      width: baseWidth,
      ...style,
    };

    const lineClass = [
      styles.skeletonLine,
      circle ? styles.circle : '',
      className || '',
    ]
      .filter(Boolean)
      .join(' ');

    return <span className={lineClass} style={singleStyle} aria-hidden="true" />;
  }

  const containerClass = className
    ? `${styles.skeletonContainer} ${className}`
    : styles.skeletonContainer;

  return (
    <div className={containerClass} style={style} aria-hidden="true">
      {Array.from({ length: lines }).map((_, index) => {
        // 让最后一行稍短一些（75%），呈现自然的段落骨架
        const isLast = index === lines - 1;
        const lineWidth = isLast && !width ? '75%' : baseWidth;

        return (
          <span
            key={index}
            className={styles.skeletonLine}
            style={{
              height: baseHeight,
              width: lineWidth,
            }}
          />
        );
      })}
    </div>
  );
};

export default Skeleton;
