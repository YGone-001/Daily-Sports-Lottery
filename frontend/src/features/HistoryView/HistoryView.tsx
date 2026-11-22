import React from 'react';
import { useSearchParams, Link } from 'react-router-dom';
import { useHistory, useEvaluationSummary } from '@/hooks';
import type { HistoryMatch, HistorySportStats } from '@/types/match';
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

function Verdict({ match }: { match: HistoryMatch }) {
  const comparison = match.comparison;
  const lifecycleWarning =
    match.state_inconsistency_warning && match.lifecycle_presentation ? (
      <div className={`${styles.hrVerdict} ${styles.ambiguous}`}>
        <span>状态不一致：落盘状态与终场证据并存</span>
        <em>
          {match.lifecycle_presentation === 'HISTORICAL_EVIDENCE_CONFLICT'
            ? '结算/评估比分与落盘比分冲突，结果不可信'
            : '落盘状态为进行中，但存在终场比分与结算证据；证据仅供只读查看'}
        </em>
      </div>
    ) : null;

  const verdict = (() => {
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

    // no_evidence：按缺失证据的具体形态区分措辞（Workstream C.4），
    // 不得把「快照存在但链条未建立」误报成「未采集赛前快照」。
    const noEvidenceCopy: Record<string, { title: string; detail: string }> = {
      NO_SNAPSHOT_EVIDENCE: { title: '无赛前验证预测', detail: '未采集赛前快照' },
      SETTLEMENT_WITHOUT_SNAPSHOT: {
        title: '证据不完整',
        detail: '存在结算记录但其引用的赛前快照不在证据库中，链条无法建立',
      },
      SNAPSHOT_PRESENT_SETTLEMENT_MISSING: {
        title: '证据不完整',
        detail: '赛前快照存在，但尚无源确认赛果的结算记录',
      },
      SETTLEMENT_PRESENT_EVALUATION_UNVERIFIED: {
        title: '证据不完整',
        detail: '结算存在，但评估链未通过完整性校验（不重建历史）',
      },
    };
    const copy =
      (comparison.no_evidence_reason && noEvidenceCopy[comparison.no_evidence_reason]) || {
        title: '无赛前验证预测',
        detail: '未采集赛前快照',
      };
    return (
      <div className={`${styles.hrVerdict} ${styles.unverified}`}>
        <span>{copy.title}</span>
        <em>{copy.detail}</em>
      </div>
    );
  })();

  return (
    <>
      {lifecycleWarning}
      {verdict}
    </>
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
          <EvaluationSummaryPanel />
          <div className={styles.historyList}>
            {matches.map((match) => (
              <Link key={match.id} to={`/match/${match.id}`} className={styles.historyRow}>
                <div className={`${styles.hrDate} mono`}>{match.date} {match.time}</div>
                <div className={styles.hrMatch}>
                  {match.home} <span className={styles.hrVs}>vs</span> {match.away}
                  <span className={styles.hrLeague}>{match.league}</span>
                </div>
                <div className={`${styles.hrScore} mono`}>{match.comparison.actual_score}</div>
                <Verdict match={match} />
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

/**
 * 只读模型质量摘要（Workstream D.5）：直接呈现后端 /api/evaluation-summary
 * 按 (sport, model_version) 分组的既有不可变样本指标。
 * 足球三分类与篮球二分类的 Brier 口径不同，绝不并排比较；
 * 小样本（尤其篮球）不构成模型优劣或盈利结论。
 */
const pct = (v: number | undefined | null): string =>
  v == null ? '—' : `${(Math.round(v * 1000) / 10).toFixed(1)}%`

const num = (v: number | undefined | null, digits = 3): string =>
  v == null ? '—' : v.toFixed(digits)

const EvaluationSummaryPanel: React.FC = () => {
  const { data, isLoading, isError } = useEvaluationSummary();
  if (isLoading) return <p className={styles.evalNote}>正在加载评估摘要…</p>;
  if (isError || !data || data.models.length === 0) return null;
  return (
    <section className={styles.evalPanel} aria-label="模型评估摘要">
      <h2 className={styles.evalTitle}>模型评估摘要（不可变样本 · 只读）</h2>
      <table className={styles.evalTable}>
        <thead>
          <tr>
            <th>运动 / 模型版本</th>
            <th>样本</th>
            <th>快照日期范围</th>
            <th>方向命中率</th>
            <th>Brier</th>
            <th>LogLoss</th>
            <th>校准 ECE</th>
            <th>市场基线（同样本）</th>
          </tr>
        </thead>
        <tbody>
          {data.models.map((m) => (
            <tr key={`${m.sport}-${m.model_version}`}>
              <td className="mono">
                {m.sport === 'football' ? '足球' : '篮球'} · {m.model_version}
              </td>
              <td>{m.sample_count}</td>
              <td className="mono">
                {m.snapshot_date_from.slice(0, 10)} ~ {m.snapshot_date_to.slice(0, 10)}
              </td>
              <td>{pct(m.accuracy)}</td>
              <td>{num(m.brier_score)}</td>
              <td>{num(m.multiclass_log_loss)}</td>
              <td>{num(m.macro_expected_calibration_error)}</td>
              <td>
                {m.market_baseline.status === 'VERIFIED' ? (
                  <>
                    模型 {pct(m.market_baseline.model_direction_accuracy)} vs 市场{' '}
                    {pct(m.market_baseline.market_direction_accuracy)}（n=
                    {m.market_baseline.same_fixture_samples}）
                  </>
                ) : (
                  'MARKET_BASELINE_NOT_AVAILABLE'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className={styles.evalNote}>
        指标口径：概率源 model_probabilities；多分类 Brier = mean(Σ(p−y)²)，足球三分类、篮球二分类口径不同，不跨运动比较。
        {data.models.some(
          (m) => m.market_baseline.market_source_identity === 'MARKET_SOURCE_IDENTITY_UNRESOLVED',
        ) &&
          ' 市场基线的历史主客方向尚未经独立证据核实（INDEPENDENT_ORIENTATION_CHECK_PENDING），市场对比仅供参考，不得解读为「模型优于市场」。'}
        {data.models.some((m) => m.market_baseline.status === 'VERIFIED') &&
          ' 市场基线仅在两方概率齐全的同样本上比较，样本数不同属正常。'}
        模型估计的价值不是已验证的盈利能力；小样本不构成模型优劣结论。
      </p>
    </section>
  );
};

export default HistoryView;
