import React, { useState } from 'react';
import styles from './KellyCalculator.module.css';

export interface KellyItem {
  label: string;
  kellyPct: number;
  odds: number;
}

export interface KellyCalculatorProps {
  items: KellyItem[];
  defaultBankroll?: number;
  showSlider?: boolean;
  minBankroll?: number;
  maxBankroll?: number;
  step?: number;
  className?: string;
}

export const KellyCalculator: React.FC<KellyCalculatorProps> = ({
  items,
  defaultBankroll = 1000,
  showSlider = true,
  minBankroll = 100,
  maxBankroll = 10000,
  step = 100,
  className,
}) => {
  const [bankroll, setBankroll] = useState<number>(defaultBankroll);

  const containerClass = className
    ? `${styles.kellyBox} ${className}`
    : styles.kellyBox;

  const formatCurrency = (amount: number, decimals: number = 0) => {
    return `¥${amount.toLocaleString('zh-CN', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })}`;
  };

  return (
    <div className={containerClass}>
      {showSlider && (
        <div className={styles.kellySliderRow}>
          <label htmlFor="kelly-bankroll-slider" className={styles.sliderLabel}>
            模拟本金
          </label>
          <div className={styles.kellySliderMain}>
            <input
              id="kelly-bankroll-slider"
              type="range"
              min={minBankroll}
              max={maxBankroll}
              step={step}
              value={bankroll}
              onChange={(e) => setBankroll(Number(e.target.value))}
              aria-label="模拟本金滑块"
            />
            <div className={styles.sliderScale}>
              <span>{formatCurrency(minBankroll)}</span>
              <span>{formatCurrency(maxBankroll)}</span>
            </div>
          </div>
          <div className={styles.kellyAmount}>
            {formatCurrency(bankroll)}
          </div>
        </div>
      )}

      {items.length === 0 ? (
        <div className={styles.emptyHint}>暂无凯利计算推荐项</div>
      ) : (
        <div className={styles.kellyList}>
          {items.map((item, index) => {
            // bet = 本金 * 凯利比例(%)
            const betAmount = Math.max(0, Math.round((bankroll * item.kellyPct) / 100));
            // return = 投注额 * 赔率
            const expectedReturn = betAmount * item.odds;

            return (
              <div key={`${item.label}-${index}`} className={styles.kellyRow}>
                <div className={styles.krLeft}>
                  <span className={styles.krLabel}>{item.label}</span>
                  <span className={styles.krPct}>
                    建议仓位 {item.kellyPct.toFixed(1)}% · 赔率 {item.odds.toFixed(2)}
                  </span>
                </div>

                <div className={styles.krDetails}>
                  <div className={styles.krMetric}>
                    <span className={styles.krMetricLabel}>建议投注</span>
                    <span className={styles.krBet}>{formatCurrency(betAmount)}</span>
                  </div>
                  <div className={styles.krMetric}>
                    <span className={styles.krMetricLabel}>预计回报</span>
                    <span className={styles.krRet}>{formatCurrency(expectedReturn, 1)}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default KellyCalculator;
