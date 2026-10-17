import React from 'react';
import { useSearchParams, Link } from 'react-router-dom';
import { useHistory } from '@/hooks';
import type { HistoryComparison, HistorySportStats } from '@/types/match';
import { SportFilter } from '@/components/SportFilter';
import { EmptyState } from '@/components/EmptyState';
import { Skeleton } from '@/components/Skeleton';
import styles from './HistoryView.module.css';

type Sport = 'all' | 'football' | 'basketball';

interface MetricProps {
  value: React.ReactNode;
  label: React.ReactNode;
  tone?: 'ok' | 'up' | 'muted';
}

const Metric: React.FC<MetricProps> = ({ value, label, tone }) => (
  <div className={styles.calibPod}>
    <span className={`m-val ${tone === 'ok' ? 'c-ok' : tone === 'up' ? 'c-up' : tone === 'muted' ? 'is-zero' : ''}`}>
      {value}
    </span>
    <span className="m-lbl">{label}</span>
  </div>
);

function renderScopeMetrics(sport: Sport, football: HistorySportStats, basketball: HistorySportStats) {
  if (sport === 'football') {
    return (
      <>
        <Metric value={football.total_finished} label="完赛场次" />
        <Metric value={`${football.verified_evaluations}/${football.total_finished}`} label={`赛前验证覆盖 (${football.unverified_matches}场未验证)`} />
        <Metric value={football.accuracy_str} label={`胜负命中率 (${football.ratio_str})`} tone="ok" />
        <Metric value={football.score_coverage_str} label={`赛前 Top-3 比分覆盖率 (${football.score_ratio_str})`} tone="up" />
      </>
    );
  }

  if (sport === 'basketball') {
    return (
      <>
        <Metric value={basketball.total_finished} label="完赛场次" />
        <Metric value={`${basketball.verified_evaluations}/${basketball.total_finished}`} label={`赛前验证覆盖 (${basketball.unverified_matches}场未验证)`} />
        <Metric value={basketball.accuracy_str} label={`胜负命中率 (${basketball.ratio_str})`} tone="ok" />
        <Metric value="不适用" label="比分覆盖率" tone="muted" />
      </>
    );
  }

  const totalFinished = football.total_finished + basketball.total_finished;
  const verified = football.verified_evaluations + basketball.verified_evaluations;
  const unverified = football.unverified_matches + basketball.unverified_matches;
  return (
    <>
      <Metric value={totalFinished} label="完赛总场次" />
      <Metric value={`${verified}/${totalFinished}`} label={`赛前验证覆盖 (${unverified}场未验证)`} />
      <Metric value={football.accuracy_str} label={`足球胜负命中 (${football.ratio_str})`} tone="ok" />
      <Metric value={football.score_coverage_str} label={`足球 Top-3 比分覆盖 (${football.score_ratio_str})`} tone="up" />
      <Metric value={basketball.accuracy_str} label={`篮球胜负命中 (${basketball.ratio_str})`} tone="ok" />
    </>
  );
}

function Verdict({ comparison }: { comparison: HistoryComparison }) {
  if (comparison.evidence_status === 'verified') {
    return (
      <div className={`${styles.hrVerdict} ${comparison.correct ? styles.ok : styles.bad}`}>
        <span>{comparison.correct ? '✓ 命中' : '✗ 偏差'}</span>
        <em>
          预测{comparison.predicted_label} · 置信{comparison.confidence}%
          {comparison.score_eligible && ` · 比分${comparison.score_hit ? '覆盖' : '未覆盖'}`}
        </em>
      </div>
    );
  }

  if (comparison.evidence_status === 'historical_version') {
    return (
      <div className={`${styles.hrVerdict} ${styles.historical}`}>
        <span>历史版本 ({comparison.model_version})</span>
        <em>预测{comparison.predicted_label} · 置信{comparison.confidence}%</em>
      </div>
    );
  }

  if (comparison.evidence_status === 'ambiguous' || comparison.evidence_status === 'invalid') {
    const isInvalid = comparison.evidence_status === 'invalid';
    return (
      <div className={`${styles.hrVerdict} ${styles.ambiguous}`}>
        <span>{isInvalid ? '证据无效 / 排除' : '证据冲突 / 排除'}</span>
        <em>{isInvalid ? '证据校验未通过' : '多条不一致记录'}</em>
      </div>
    );
  }

  return (
    <div className={`${styles.hrVerdict} ${styles.unverified}`}>
      <span>无赛前验证预测</span>
      <em>未采集赛前快照</em>
    </div>
  );
}

export const HistoryView: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const rawSport = searchParams.get('sport');
  const currentSport: Sport = rawSport === 'football' || rawSport === 'basketball' ? rawSport : 'all';
  const { data, isLoading, isError } = useHistory(currentSport);

  const handleSportChange = (sport: string) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (sport === 'football' || sport === 'basketball') next.set('sport', sport);
      else next.delete('sport');
      return next;
    });
  };

  if (isLoading) return <Skeleton height={52} lines={6} />;
  if (isError || !data) return <EmptyState title="历史赛果获取失败" subtitle="网络连接异常或服务未响应，请稍后重试" icon="⚠" />;

  const { matches, stats, total_finished: totalFinished } = data;
  return (
    <div>
      <section className={styles.terminalHeader}>
        <div>
          <h1 className={styles.title}>历史复盘</h1>
          <p className={styles.subtitle}>
            不可变赛前预测 vs 真实赛果 · 共 {totalFinished} 场完赛 · {stats.verified_evaluations} 场赛前验证
          </p>
        </div>
        <SportFilter current={currentSport} onChange={handleSportChange} />
      </section>

      {matches.length > 0 ? (
        <>
          <div className={styles.calibBar}>
            {renderScopeMetrics(currentSport, stats.football, stats.basketball)}
          </div>
          <div className={styles.historyList}>
            {matches.map((match) => (
              <Link key={match.id} to={`/match/${match.id}`} className={styles.historyRow}>
                <div className={`${styles.hrDate} mono`}>{match.date} {match.time}</div>
                <div className={styles.hrMatch}>
                  {match.home} <span className={styles.hrVs}>vs</span> {match.away}
                  <span className={styles.hrLeague}>{match.league}</span>
                </div>
                <div className={`${styles.hrScore} mono`}>{match.comparison.actual_score}</div>
                <Verdict comparison={match.comparison} />
              </Link>
            ))}
          </div>
        </>
      ) : (
        <EmptyState title="暂无历史赛果" subtitle="等待赛事完赛后自动归档" icon="◈" />
      )}
    </div>
  );
};

export default HistoryView;
