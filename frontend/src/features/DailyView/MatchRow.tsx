import React from 'react';
import { Link } from 'react-router-dom';
import type { Match } from '@/types/match';
import { ProbBar } from '@/components/ProbBar';
import { StatusTag } from '@/components/StatusTag';
import { SuspenseStars } from '@/components/SuspenseStars';
import { Icon } from '@/components/Icon';
import styles from './MatchRow.module.css';

export interface MatchRowProps {
  match: Match;
}

export const MatchRow: React.FC<MatchRowProps> = ({ match }) => {
  const ev = match.prediction?.ev_analysis;
  const hasValue = Boolean(
    ev && (ev.home?.is_value || ev.draw?.is_value || ev.away?.is_value)
  );
  const isLive = match.status === 'live';
  const isFinished = match.status === 'finished';

  const rowClasses = [
    styles.matchRow,
    isLive ? styles.isLive : '',
    isFinished ? styles.isFinished : '',
    hasValue ? styles.hasValue : '',
  ]
    .filter(Boolean)
    .join(' ');

  const ft = match.score?.ft;
  const ht = match.score?.ht;

  const renderProbs = () => {
    if (match.prediction && !isFinished) {
      const { probabilities } = match.prediction;
      const isFootball = match.sport === 'football';

      return (
        <span className={styles.mrProbs}>
          <ProbBar
            homeWin={probabilities.home_win}
            draw={isFootball ? probabilities.draw : 0}
            awayWin={probabilities.away_win}
            animate={false}
          />
          <span className={`${styles.mrNums} mono`}>
            <span className={ev?.home?.is_value ? styles.isValue : undefined}>
              {probabilities.home_win}
            </span>
            {isFootball && (
              <span className={ev?.draw?.is_value ? styles.isValue : undefined}>
                {probabilities.draw}
              </span>
            )}
            <span className={ev?.away?.is_value ? styles.isValue : undefined}>
              {probabilities.away_win}
            </span>
          </span>
        </span>
      );
    }

    if (isFinished && ft && ft[0] !== undefined && ft[1] !== undefined) {
      const outcomeText =
        ft[0] > ft[1] ? '主胜' : ft[0] < ft[1] ? '客胜' : '平局';
      return (
        <span className={styles.mrProbs}>
          <span className={styles.mrOutcome}>{outcomeText}</span>
        </span>
      );
    }

    return (
      <span className={styles.mrProbs}>
        <span className={styles.mrNone}>暂无赔率</span>
      </span>
    );
  };

  const renderOdds = () => {
    if (!match.odds) return null;
    if (match.sport === 'football') {
      return `${match.odds.home_win}/${match.odds.draw}/${match.odds.away_win}`;
    }
    return `${match.odds.home_win}/${match.odds.away_win}`;
  };

  return (
    <Link to={`/match/${match.id}`} className={rowClasses}>
      {/* 比赛时间 */}
      <span className={styles.mrTime}>
        {hasValue && <Icon name="flame" className={styles.mrMark} />}
        <span className="mono">{match.time}</span>
      </span>

      {/* 主队信息 */}
      <span className={`${styles.mrSide} ${styles.mrSideHome}`}>
        <span className={styles.mrName}>{match.home}</span>
        {match.home_rank != null && (
          <span className={`${styles.mrRank} mono`}>#{match.home_rank}</span>
        )}
      </span>

      {/* 中间比分 / VS */}
      <span className={styles.mrCenter}>
        {ft && ft[0] !== undefined && ft[1] !== undefined ? (
          <>
            <span className={`${styles.mrScore} mono`}>
              {ft[0]}-{ft[1]}
            </span>
            {ht && ht[0] !== undefined && ht[1] !== undefined && (
              <span className={`${styles.mrHt} mono`}>
                {ht[0]}-{ht[1]}
              </span>
            )}
          </>
        ) : (
          <span className={`${styles.mrVs} mono`}>VS</span>
        )}
      </span>

      {/* 客队信息 */}
      <span className={`${styles.mrSide} ${styles.mrSideAway}`}>
        {match.away_rank != null && (
          <span className={`${styles.mrRank} mono`}>#{match.away_rank}</span>
        )}
        <span className={styles.mrName}>{match.away}</span>
      </span>

      {/* 预测概率 / 终场赛果 */}
      {renderProbs()}

      {/* 赔率 */}
      <span className={`${styles.mrOdds} mono`}>{renderOdds()}</span>

      {/* 状态与悬念 */}
      <span className={styles.mrStatus}>
        <StatusTag status={match.status} />
        {match.prediction?.suspense && !isFinished && (
          <SuspenseStars
            stars={match.prediction.suspense.stars}
            className={styles.mrSuspense}
          />
        )}
      </span>
    </Link>
  );
};

export default MatchRow;
