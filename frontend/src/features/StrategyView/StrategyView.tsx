import React, { useState } from 'react';
import { useStrategy } from '@/hooks';
import type { ComboResult } from '@/types/strategy';
import { Icon } from '@/components/Icon';
import styles from './StrategyView.module.css';

export const StrategyView: React.FC = () => {
  const [sport, setSport] = useState<string>('football');
  const [bankroll, setBankroll] = useState<number>(10000);

  const { data, isLoading, isError } = useStrategy(sport);

  const formatNumber = (num: number) => num.toLocaleString('zh-CN');

  const renderLegs = (legs: ComboResult['legs']) => {
    return legs.map((leg, idx) => (
      <div key={`${leg.match_id}-${idx}`} className={styles.ccLeg}>
        <span className={styles.ccLegName}>{leg.match_name}</span>
        <span className={styles.ccLegRight}>
          <span className={styles.ccLegOutcome}>{leg.outcome}</span>
          <span className={styles.ccLegOdds}>@{leg.odds.toFixed(2)}</span>
        </span>
      </div>
    ));
  };

  const renderComboCard = (
    combo: ComboResult | undefined,
    colorClass: 'double' | 'treble' | 'four',
    title: string,
    note: string
  ) => {
    if (!combo) return null;

    const betAmount = Math.max(
      0,
      Math.round((bankroll * combo.kelly_pct) / 100)
    );
    const returnAmount = Math.round(betAmount * combo.combined_odds);

    return (
      <div className={styles.comboCard}>
        <div className={styles.ccHead}>
          <div className={styles.ccTitle}>
            <span className={`${styles.ccDot} ${styles[colorClass]}`} />
            {title}
            <span className={styles.ccNote}>{note}</span>
          </div>
          <div className={styles.ccMetrics}>
            <div className={styles.ccMetric}>
              联合胜率 <strong className="c-ok">{combo.combined_prob}%</strong>
            </div>
            <div className={styles.ccMetric}>
              复合赔率 <strong>@{combo.combined_odds}</strong>
            </div>
            <div className={styles.ccMetric}>
              期望值 <strong className="c-odds">+{combo.ev}%</strong>
            </div>
          </div>
        </div>

        <div className={styles.ccLegs}>{renderLegs(combo.legs)}</div>

        <div className={styles.kellyRow}>
          <span className={styles.krLabel}>建议仓位 {combo.kelly_pct}%</span>
          <span className={styles.krBet}>¥{formatNumber(betAmount)}</span>
          <span className={styles.krRet}>回报 ¥{formatNumber(returnAmount)}</span>
        </div>
      </div>
    );
  };

  const bets = data?.single_bets || [];
  const hasCombos = Boolean(
    data?.best_double || data?.best_treble || data?.best_four
  );

  return (
    <div>
      {/* 头部标题与描述 */}
      <div className={styles.strategyHead}>
        <h1 className={styles.shTitle}>智能策略舱</h1>
        <p className={styles.shSub}>
          扫描当日全部盘口 · 定位正 EV 价值 · 计算最优串关与仓位
        </p>
      </div>

      {/* 模拟本金滑块与项目筛选卡 */}
      <div className={styles.bankrollCard}>
        <div className={styles.bcTitle}>资金管理</div>

        <div className={styles.kellySliderRow}>
          <label className={styles.sliderLabel} htmlFor="bankroll-input">
            模拟本金
          </label>
          <div className={styles.kellySliderMain}>
            <input
              id="bankroll-input"
              type="range"
              min={1000}
              max={200000}
              step={1000}
              value={bankroll}
              onChange={(e) => setBankroll(Number(e.target.value))}
              aria-label="模拟本金滑块"
            />
            <div className={styles.sliderScale}>
              <span>¥1,000</span>
              <span>¥200,000</span>
            </div>
          </div>
          <span className={styles.kellyAmount}>¥{formatNumber(bankroll)}</span>
        </div>

        <div className={styles.chipRowTight}>
          <button
            type="button"
            className={`${styles.chip} ${sport === 'football' ? styles.chipOn : ''}`}
            onClick={() => setSport('football')}
            aria-pressed={sport === 'football'}
          >
            <Icon name="football" className={styles.chipIcon} />
            足球
          </button>
          <button
            type="button"
            className={`${styles.chip} ${sport === 'basketball' ? styles.chipOn : ''}`}
            onClick={() => setSport('basketball')}
            aria-pressed={sport === 'basketball'}
          >
            <Icon name="basketball" className={styles.chipIcon} />
            篮球
          </button>
          <button
            type="button"
            className={`${styles.chip} ${sport === 'all' ? styles.chipOn : ''}`}
            onClick={() => setSport('all')}
            aria-pressed={sport === 'all'}
          >
            全部
          </button>
        </div>
      </div>

      {/* 加载与内容 */}
      {isLoading ? (
        <div className={styles.loadingState}>
          <div className={styles.spinner} />
          <p>正在扫描盘口并计算价值...</p>
        </div>
      ) : isError ? (
        <div className={styles.loadingState}>
          <p className="c-live">加载策略数据失败，请稍后重试</p>
        </div>
      ) : (
        <div>
          {/* 单个正 EV 价值选项 */}
          <div className={styles.valueTable}>
            <div className={styles.dataCard}>
              <h3 className={styles.dcTitle}>
                <Icon name="flame" />
                正 EV 价值选项{' '}
                <span className="c-odds">({bets.length} 项)</span>
              </h3>

              {bets.length > 0 ? (
                <div>
                  {bets.map((b, index) => {
                    const betAmount = Math.max(
                      0,
                      Math.round((bankroll * b.kelly_pct) / 100)
                    );
                    const returnAmount = Math.round(betAmount * b.odds);

                    return (
                      <div
                        key={`${b.match_id}-${index}`}
                        className={styles.ccLeg}
                      >
                        <span className={styles.ccLegName}>
                          {b.league} · {b.match_name}
                        </span>
                        <span className={styles.ccLegRight}>
                          <span className={styles.ccLegOutcome}>
                            {b.outcome}
                          </span>
                          <span className={styles.ccLegOdds}>
                            @{b.odds.toFixed(2)}
                          </span>
                          <span className={styles.legEv}>+{b.ev}%</span>
                          <span className={styles.legKelly}>
                            仓位 {b.kelly_pct}% (¥{formatNumber(betAmount)} → 回报 ¥{formatNumber(returnAmount)})
                          </span>
                        </span>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className={styles.emptyInline}>
                  <p className={styles.eiTitle}>当前盘口未发现正 EV 机会</p>
                  <p className={styles.eiSub}>
                    这通常意味着市场定价有效，模型概率与赔率隐含概率基本一致。
                  </p>
                </div>
              )}
            </div>
          </div>

          {/* 最优串关组合 */}
          <div className={styles.comboList}>
            {hasCombos ? (
              <>
                {renderComboCard(
                  data?.best_double,
                  'double',
                  '二串一',
                  '最稳组合'
                )}
                {renderComboCard(
                  data?.best_treble,
                  'treble',
                  '三串一',
                  '平衡组合'
                )}
                {renderComboCard(
                  data?.best_four,
                  'four',
                  '四串一',
                  '高赔组合'
                )}
              </>
            ) : (
              <div className={styles.comboCard}>
                <div className={styles.emptyInline}>
                  <p className={styles.eiTitle}>
                    今日没有符合条件的串关组合
                  </p>
                  <p className={styles.eiSub}>
                    串关至少需要 2 场正 EV 选项，当前全天只找到 {bets.length}{' '}
                    项，不足以组成串关。
                    <br />
                    可以关注上方的单项价值选项，或等待盘口更新。
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default StrategyView;
