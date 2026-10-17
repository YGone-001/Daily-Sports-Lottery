import React from 'react';
import styles from './EmptyState.module.css';

export interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  subtitle?: React.ReactNode;
  compact?: boolean;
  action?: React.ReactNode;
  className?: string;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon,
  title,
  subtitle,
  compact = false,
  action,
  className,
}) => {
  if (compact) {
    const compactClass = className
      ? `${styles.emptyInline} ${className}`
      : styles.emptyInline;

    return (
      <div className={compactClass}>
        {icon && <div className={styles.eiIcon}>{icon}</div>}
        <div className={styles.eiTitle}>{title}</div>
        {subtitle && <div className={styles.eiSub}>{subtitle}</div>}
        {action && <div className={styles.action}>{action}</div>}
      </div>
    );
  }

  const standardClass = className
    ? `${styles.emptyState} ${className}`
    : styles.emptyState;

  return (
    <div className={standardClass}>
      {icon && <div className={styles.esIcon}>{icon}</div>}
      <div className={styles.esTitle}>{title}</div>
      {subtitle && <div className={styles.esSub}>{subtitle}</div>}
      {action && <div className={styles.action}>{action}</div>}
    </div>
  );
};

export default EmptyState;
