import React from 'react';
import styles from './Footer.module.css';

export interface FooterProps {
  className?: string;
}

export const Footer: React.FC<FooterProps> = ({ className }) => {
  const containerClass = className
    ? `${styles.siteFooter} ${className}`
    : styles.siteFooter;

  return (
    <footer className={containerClass}>
      <p className={styles.footerMain}>
        概率 ≠ 结果。系统输出为模型视角，不构成投注建议。
      </p>
      <p className={styles.footerSub}>
        数据来源 500.com · 模型基于 Elo 强度 + 泊松分布 + 赔率对撞 · 仅供研究参考
      </p>
    </footer>
  );
};

export default Footer;
