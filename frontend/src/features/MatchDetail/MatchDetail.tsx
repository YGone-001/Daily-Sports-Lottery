import React from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  useMatch,
  useMatchOddsHistory,
  useMatchSettlements,
  useMatchSnapshots,
} from '@/hooks';
import { ProbBar } from '@/components/ProbBar';
import { StatusTag } from '@/components/StatusTag';
import { KellyCalculator, type KellyItem } from '@/components/KellyCalculator';
import { EmptyState } from '@/components/EmptyState';
import { Skeleton } from '@/components/Skeleton';
import { Icon } from '@/components/Icon';
import { RadarChart } from '@/components/RadarChart';
import { OddsTimeline } from '@/components/OddsTimeline';
import type { Match, MatchSnapshot, SettlementRecord } from '@/types/match';
import styles from './MatchDetail.module.css';

/** 快照概率可能是 0-1 或 0-100 两种口径，统一格式化为百分比数字。 */
function asPercent(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '—';
  const pct = value <= 1 ? value * 100 : value;
  return (Math.round(pct * 10) / 10).toFixed(1);
}

const OUTCOME_LABELS: Record<NonNullable<SettlementRecord['actual_outcome']>, string> = {
  home_win: '主胜',
  draw: '平局',
  away_win: '客胜',
};

function SnapshotCard({
  snapshots,
  isLoading,
}: {
  snapshots: MatchSnapshot[];
  isLoading: boolean;
}) {
  return (
    <div className={`${styles.dataCard} ${styles.span2}`}>
      <h3 className={styles.dcTitle}>
        <Icon name="list" />赛前预测快照（不可变历史）
      </h3>
      {isLoading ? (
        <p className={styles.noteFaint}>正在加载快照记录…</p>
      ) : snapshots.length === 0 ? (
        <p className={styles.noteFaint}>
          暂无赛前预测快照：该场未在开赛前捕获预测（可能因开赛时间未知、当时无可用盘口，或赛事开赛后才被收录）。这里只显示真实留存的快照，不会重建历史。
        </p>
      ) : (
        <table className={styles.dataTable}>
          <thead>
            <tr>
              <th>生成时间</th>
              <th>模型版本</th>
              <th>主胜</th>
              <th>平局</th>
              <th>客胜</th>
            </tr>
          </thead>
          <tbody>
            {snapshots.map((s) => (
              <tr key={s.snapshot_id}>
                <td className="mono">{s.generated_at ?? '—'}</td>
                <td className="mono">{s.model_version}</td>
                <td>{asPercent(s.display_probabilities?.home_win)}%</td>
                <td>{asPercent(s.display_probabilities?.draw)}%</td>
                <td>{asPercent(s.display_probabilities?.away_win)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function SettlementsCard({
  settlements,
  isLoading,
}: {
  settlements: SettlementRecord[];
  isLoading: boolean;
}) {
  return (
    <div className={`${styles.dataCard} ${styles.span2}`}>
      <h3 className={styles.dcTitle}>
        <Icon name="coins" />结算记录（源确认赛果）
      </h3>
      {isLoading ? (
        <p className={styles.noteFaint}>正在加载结算记录…</p>
      ) : settlements.length === 0 ? (
        <p className={styles.noteFaint}>
          暂无结算记录：等待官方赛果确认。仅因时间流逝推导的状态不会触发结算。
        </p>
      ) : (
        <table className={styles.dataTable}>
          <thead>
            <tr>
              <th>结算时间</th>
              <th>赛果</th>
              <th>比分</th>
              <th>赛前预测有效性</th>
            </tr>
          </thead>
          <tbody>
            {settlements.map((s) => (
              <tr key={s.settlement_id}>
                <td className="mono">{s.settled_at ?? '—'}</td>
                <td>{s.actual_outcome ? OUTCOME_LABELS[s.actual_outcome] : '—'}</td>
                <td>
                  {s.final_score ? `${s.final_score.home}-${s.final_score.away}` : '—'}
                </td>
                <td>{s.is_valid_prematch === false ? '无效（非赛前快照）' : '有效'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function PredictionUnavailable({ match }: { match: Match }) {
  const reason = match.kickoff_time_known === false
    ? '该场开赛时间未知，无法建立赛前时间边界，因此没有赛前预测。'
    : match.status !== 'upcoming'
      ? '该场已开赛或已完赛，不再生成赛前预测；历史快照与结算记录见下方证据区。'
      : '暂无可用赛前预测（可能缺少可用市场盘口或球队数据不足）。';
  return (
    <EmptyState title="赛前预测不可用" subtitle={reason} icon="◈" />
  );
}

export const MatchDetail: React.FC = () => {
  const { id = '' } = useParams<{ id: string }>();
  const { data: match, isLoading, isError } = useMatch(id);
  const { data: oddsHistory = [] } = useMatchOddsHistory(id);
  const { data: snapshots = [], isLoading: snapshotsLoading } = useMatchSnapshots(id);
  const { data: settlements = [], isLoading: settlementsLoading } = useMatchSettlements(id);

  if (isLoading) {
    return (
      <div>
        <Skeleton height={140} style={{ marginBottom: 20 }} />
        <Skeleton height={100} style={{ marginBottom: 20 }} />
        <Skeleton lines={8} height={50} />
      </div>
    );
  }

  if (isError || !match) {
    return (
      <div>
        <EmptyState
          title="未找到该比赛"
          subtitle="可能已被清理或 ID 有误"
          icon="◈"
        />
        <div className={styles.backNav}>
          <Link to="/" className={styles.backLink}>
            ← 返回赛事列表
          </Link>
        </div>
      </div>
    );
  }

  const p = match.prediction;
  const isBb = match.sport === 'basketball';
  const ft = match.score?.ft;
  const ht = match.score?.ht;

  const hasValue = Boolean(
    p &&
      (p.ev_analysis.home.is_value ||
        p.ev_analysis.draw.is_value ||
        p.ev_analysis.away.is_value),
  );

  // 构造凯利计算器所需项
  const kellyItems: KellyItem[] = [];
  if (p?.ev_analysis.home.is_value && p.market?.odds?.home_win) {
    kellyItems.push({
      label: `主胜 · ${match.home}`,
      kellyPct: p.ev_analysis.home.kelly_pct,
      odds: p.market.odds.home_win,
    });
  }
  if (p && !isBb && p.ev_analysis.draw.is_value && p.market?.odds?.draw) {
    kellyItems.push({
      label: '平局',
      kellyPct: p.ev_analysis.draw.kelly_pct,
      odds: p.market.odds.draw,
    });
  }
  if (p?.ev_analysis.away.is_value && p.market?.odds?.away_win) {
    kellyItems.push({
      label: `客胜 · ${match.away}`,
      kellyPct: p.ev_analysis.away.kelly_pct,
      odds: p.market.odds.away_win,
    });
  }

  const actualScoreStr =
    ft && ft[0] !== undefined && ft[1] !== undefined ? `${ft[0]}-${ft[1]}` : null;
  const kickoffUnknown = match.kickoff_time_known === false;
  const kickoffLabel = kickoffUnknown ? '开赛时间待定' : `${match.date} ${match.time}`;

  return (
    <div>
      {/* 头部元数据与比分对阵 */}
      <section className={styles.detailHeader}>
        <div className={styles.dhMeta}>
          <span className={styles.dhLeague}>{match.league}</span>
          {match.round && <span>{match.round}</span>}
          <span className="mono">{kickoffLabel}</span>
          <StatusTag
            status={match.status}
            kickoffTimeKnown={match.kickoff_time_known}
            matchDate={match.date}
          />
        </div>

        <div className={styles.dhMatchup}>
          {/* 主队 */}
          <div className={styles.dhTeam}>
            <div className={styles.dhTname}>{match.home}</div>
            <div className={styles.dhTmeta}>
              {p ? `Elo ${p.home_team.strength}` : '实力数据不足'}
              {match.home_rank != null && ` · 排名 #${match.home_rank}`}
            </div>
          </div>

          {/* 比分 / VS */}
          <div className={styles.dhCenter}>
            {actualScoreStr ? (
              <>
                <div className={styles.dhScore}>{actualScoreStr}</div>
                <div className={styles.dhStatus}>
                  终场
                  {ht && ht[0] !== undefined && ht[1] !== undefined && (
                    <> · 半场 {ht[0]}-{ht[1]}</>
                  )}
                </div>
              </>
            ) : (
              <div className={styles.dhVs}>VS</div>
            )}
          </div>

          {/* 客队 */}
          <div className={styles.dhTeam}>
            <div className={styles.dhTname}>{match.away}</div>
            <div className={styles.dhTmeta}>
              {p ? `Elo ${p.away_team.strength}` : '实力数据不足'}
              {match.away_rank != null && ` · 排名 #${match.away_rank}`}
            </div>
          </div>
        </div>
      </section>

      {p ? (
        <>
          {/* 概率三卡 (足球3张，篮球2张) */}
          <div className={styles.probCards}>
            {/* 主胜 */}
            <div
              className={`${styles.probCard} ${
                p.ev_analysis.home.is_value ? styles.isValue : ''
              }`}
            >
              {p.ev_analysis.home.is_value && (
                <div className={styles.pcBadge}>
                  <Icon name="flame" />价值 +{p.ev_analysis.home.ev}%
                </div>
              )}
              <div className={`${styles.pcVal} c-home`}>
                {p.probabilities.home_win}%
              </div>
              <div className={styles.pcLbl}>主胜 · {match.home}</div>
              {p.ev_analysis.home.is_value && (
                <div className={styles.pcEv}>
                  建议仓位 {p.ev_analysis.home.kelly_pct}%
                </div>
              )}
            </div>

            {/* 平局 (仅足球) */}
            {!isBb && (
              <div
                className={`${styles.probCard} ${
                  p.ev_analysis.draw.is_value ? styles.isValue : ''
                }`}
              >
                {p.ev_analysis.draw.is_value && (
                  <div className={styles.pcBadge}>
                    <Icon name="flame" />价值 +{p.ev_analysis.draw.ev}%
                  </div>
                )}
                <div className={`${styles.pcVal} c-draw`}>
                  {p.probabilities.draw}%
                </div>
                <div className={styles.pcLbl}>平局</div>
                {p.ev_analysis.draw.is_value && (
                  <div className={styles.pcEv}>
                    建议仓位 {p.ev_analysis.draw.kelly_pct}%
                  </div>
                )}
              </div>
            )}

            {/* 客胜 */}
            <div
              className={`${styles.probCard} ${
                p.ev_analysis.away.is_value ? styles.isValue : ''
              }`}
            >
              {p.ev_analysis.away.is_value && (
                <div className={styles.pcBadge}>
                  <Icon name="flame" />价值 +{p.ev_analysis.away.ev}%
                </div>
              )}
              <div className={`${styles.pcVal} c-away`}>
                {p.probabilities.away_win}%
              </div>
              <div className={styles.pcLbl}>客胜 · {match.away}</div>
              {p.ev_analysis.away.is_value && (
                <div className={styles.pcEv}>
                  建议仓位 {p.ev_analysis.away.kelly_pct}%
                </div>
              )}
            </div>
          </div>

          {/* 大尺寸概率分布条 */}
          <ProbBar
            homeWin={p.probabilities.home_win}
            draw={isBb ? 0 : p.probabilities.draw}
            awayWin={p.probabilities.away_win}
            large
            animate
          />
        </>
      ) : (
        <PredictionUnavailable match={match} />
      )}

      {/* 数据栅格面板 */}
      <div className={styles.dataGrid}>
        {/* 篮球盘口面板 */}
        {p && isBb && p.basketball && (
          <div className={`${styles.dataCard} ${styles.span2}`}>
            <h3 className={styles.dcTitle}>
              <Icon name="basketball" />篮球盘口分析
            </h3>
            <div className={`${styles.bbGrid} ${styles.bbGrid4}`}>
              <div className={styles.bbCell}>
                <span className="m-val m-val-sm">
                  {p.basketball.expected_home_points}
                </span>
                <span className="m-lbl">{match.home} 预期得分</span>
              </div>
              <div className={styles.bbCell}>
                <span className="m-val m-val-sm">
                  {p.basketball.expected_away_points}
                </span>
                <span className="m-lbl">{match.away} 预期得分</span>
              </div>
              <div className={styles.bbCell}>
                <span className="m-val m-val-sm">
                  {p.basketball.expected_total}
                </span>
                <span className="m-lbl">预期总分</span>
              </div>
              <div className={styles.bbCell}>
                <span className="m-val m-val-sm">{p.basketball.spread}</span>
                <span className="m-lbl">让分 (主队视角)</span>
              </div>
            </div>
            <div className={`${styles.bbGrid} ${styles.bbGrid2}`}>
              <div className={styles.bbCell}>
                <span className="m-val m-val-sm c-home">
                  {p.basketball.over_prob}%
                </span>
                <span className="m-lbl">
                  大分 (&gt; {p.basketball.over_line})
                </span>
              </div>
              <div className={styles.bbCell}>
                <span className="m-val m-val-sm c-away">
                  {p.basketball.under_prob}%
                </span>
                <span className="m-lbl">
                  小分 (&lt; {p.basketball.over_line})
                </span>
              </div>
            </div>
          </div>
        )}

        {/* 足球：最可能比分 */}
        {p && !isBb && p.top_scores && p.top_scores.length > 0 && (
          <div className={styles.dataCard}>
            <h3 className={styles.dcTitle}>
              <Icon name="target" />最可能比分
            </h3>
            <div className={styles.scorePills}>
              {p.top_scores.map((s) => {
                const isHit = actualScoreStr !== null && s.score === actualScoreStr;
                return (
                  <div
                    key={s.score}
                    className={`${styles.scorePill} ${isHit ? styles.hit : ''}`}
                  >
                    <span className="m-val m-val-sm">{s.score}</span>
                    <span className="m-lbl">{s.probability}%</span>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* 足球：进球数分布 */}
        {p && !isBb && p.goals_prediction && (
          <div className={styles.dataCard}>
            <h3 className={styles.dcTitle}>
              <Icon name="football" />进球数分布
            </h3>
            <div className={styles.bbGrid}>
              <div className={styles.bbCell}>
                <span className="m-val m-val-sm">
                  {p.goals_prediction.most_likely}
                </span>
                <span className="m-lbl">最可能总进球</span>
              </div>
              <div className={styles.bbCell}>
                <span className="m-val m-val-sm c-home">
                  {(p.goals_prediction.over_2_5 * 100).toFixed(1)}%
                </span>
                <span className="m-lbl">大球 (&gt;2.5)</span>
              </div>
              <div className={styles.bbCell}>
                <span className="m-val m-val-sm c-away">
                  {(p.goals_prediction.under_2_5 * 100).toFixed(1)}%
                </span>
                <span className="m-lbl">小球 (&lt;2.5)</span>
              </div>
            </div>
            {p.expected_goals && (
              <div className={styles.goalsFoot}>
                <span>
                  预期进球 {p.expected_goals.home} : {p.expected_goals.away}
                </span>
                <span>
                  悬念 {p.suspense.stars}★ {p.suspense.label}
                </span>
              </div>
            )}
          </div>
        )}

        {/* 赔率对比：市场赔率 vs 模型概率 */}
        {p?.market?.odds &&
          p.market.implied_probabilities &&
          p.market.implied_probabilities.home_win !== null && (
            <div className={`${styles.dataCard} ${styles.span2}`}>
              <h3 className={styles.dcTitle}>
                <Icon name="scale" />市场赔率 vs 模型概率
              </h3>
              <table className={styles.dataTable}>
                <thead>
                  <tr>
                    <th>选项</th>
                    <th>赔率</th>
                    <th>隐含概率</th>
                    <th>模型概率</th>
                    <th>EV</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    className={
                      p.ev_analysis.home.is_value ? styles.isValueRow : undefined
                    }
                  >
                    <td>主胜</td>
                    <td>{p.market.odds.home_win}</td>
                    <td>{p.market.implied_probabilities.home_win}%</td>
                    <td>{p.model_probabilities.home_win}%</td>
                    <td>{p.ev_analysis.home.ev}%</td>
                  </tr>
                  {!isBb && (
                    <tr
                      className={
                        p.ev_analysis.draw.is_value
                          ? styles.isValueRow
                          : undefined
                      }
                    >
                      <td>平局</td>
                      <td>{p.market.odds.draw}</td>
                      <td>{p.market.implied_probabilities.draw}%</td>
                      <td>{p.model_probabilities.draw}%</td>
                      <td>{p.ev_analysis.draw.ev}%</td>
                    </tr>
                  )}
                  <tr
                    className={
                      p.ev_analysis.away.is_value ? styles.isValueRow : undefined
                    }
                  >
                    <td>客胜</td>
                    <td>{p.market.odds.away_win}</td>
                    <td>{p.market.implied_probabilities.away_win}%</td>
                    <td>{p.model_probabilities.away_win}%</td>
                    <td>{p.ev_analysis.away.ev}%</td>
                  </tr>
                </tbody>
              </table>
              <p className={styles.noteFaint}>
                赔率融合权重 {Math.round(p.market.weight * 100)}% · 数据来源
                500.com
              </p>
            </div>
          )}

        {/* 凯利仓位计算器 (仅在存在正 EV 项时显示) */}
        {p && hasValue && (
          <div className={`${styles.dataCard} ${styles.span2}`}>
            <h3 className={styles.dcTitle}>
              <Icon name="coins" />凯利仓位计算器
            </h3>
            <KellyCalculator
              items={kellyItems}
              defaultBankroll={10000}
              minBankroll={1000}
              maxBankroll={200000}
              step={1000}
            />
            <p className={styles.noteFaint}>
              采用 1/4 分数凯利，仅对正 EV 选项给出建议。模型估计的价值不等于已验证的盈利能力，博彩有风险，请理性对待。
            </p>
          </div>
        )}

        {/* 六维战力雷达图 */}
        {p?.radar && (
          <div className={`${styles.dataCard} ${styles.span2}`}>
            <h3 className={styles.dcTitle}>
              <Icon name="radar" />六维战力对比
            </h3>
            <RadarChart
              data={p.radar}
              homeTeam={match.home}
              awayTeam={match.away}
              height={320}
            />
          </div>
        )}

        {/* 赛前赔率走势与波动 */}
        <div className={`${styles.dataCard} ${styles.span2}`}>
          <h3 className={styles.dcTitle}>
            <Icon name="scale" />赛前赔率走势与市场异动
          </h3>
          <OddsTimeline
            history={oddsHistory}
            sport={match.sport}
            height={260}
          />
          <p className={styles.noteFaint}>
            时序监控赛前盘口变动 · 数据捕获自 500.com · 仅记录赛前不可变波动
          </p>
        </div>

        {/* 赛前预测快照（不可变） */}
        <SnapshotCard snapshots={snapshots} isLoading={snapshotsLoading} />

        {/* 结算记录 */}
        <SettlementsCard settlements={settlements} isLoading={settlementsLoading} />

        {/* 实力情报与模型输入 */}
        {p?.key_info && (
          <div className={`${styles.dataCard} ${styles.span2}`}>
            <h3 className={styles.dcTitle}>
              <Icon name="list" />模型输入
            </h3>
            <div className={styles.intelList}>
              {p.key_info.home_notes && p.key_info.home_notes.length > 0 && (
                <div className={styles.intelItem}>
                  <span className={styles.intelName}>{match.home}</span>
                  <span>{p.key_info.home_notes.join(' · ')}</span>
                </div>
              )}
              {p.key_info.away_notes && p.key_info.away_notes.length > 0 && (
                <div className={styles.intelItem}>
                  <span className={styles.intelName}>{match.away}</span>
                  <span>{p.key_info.away_notes.join(' · ')}</span>
                </div>
              )}
              <div className={styles.intelItem}>
                <span className={styles.intelName}>综合悬念</span>
                <span>
                  {p.suspense.stars} 星 · {p.suspense.label}
                </span>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 返回上一级 */}
      <div className={styles.backNav}>
        <Link to="/" className={styles.backLink}>
          ← 返回赛事列表
        </Link>
      </div>
    </div>
  );
};

export default MatchDetail;
