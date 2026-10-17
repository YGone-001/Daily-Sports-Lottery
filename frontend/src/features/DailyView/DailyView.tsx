import React, { useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useMatches, useDates, useRefresh } from '@/hooks';
import type { Match } from '@/types/match';
import { SportFilter } from '@/components/SportFilter';
import { EmptyState } from '@/components/EmptyState';
import { Skeleton } from '@/components/Skeleton';
import { Icon } from '@/components/Icon';
import { MatchRow } from './MatchRow';
import styles from './DailyView.module.css';

interface LeagueGroup {
  league: string;
  sport: 'football' | 'basketball';
  matches: Match[];
}

export const DailyView: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();

  const rawDate = searchParams.get('date') || undefined;
  const currentSport = searchParams.get('sport') || 'all';

  const { data: datesData } = useDates();
  const availableDates = datesData?.dates || [];
  const displayDate = rawDate || availableDates[0];

  const {
    data: matches = [],
    isLoading,
    isError,
  } = useMatches(displayDate, currentSport);

  const refreshMutation = useRefresh();

  const handleSportChange = (sport: string) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (sport && sport !== 'all') {
        next.set('sport', sport);
      } else {
        next.delete('sport');
      }
      return next;
    });
  };

  const handleDateChange = (date: string) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('date', date);
      return next;
    });
  };

  const handleRefresh = () => {
    refreshMutation.mutate();
  };

  // 统计指标计算
  const stats = useMemo(() => {
    const total = matches.length;
    const live = matches.filter((m) => m.status === 'live').length;
    const upcoming = matches.filter((m) => m.status === 'upcoming').length;
    const withOdds = matches.filter((m) => Boolean(m.odds)).length;

    return { total, live, upcoming, withOdds };
  }, [matches]);

  // 按联赛客户端聚合分组
  const groups = useMemo<LeagueGroup[]>(() => {
    const map = new Map<string, LeagueGroup>();
    for (const m of matches) {
      const key = `${m.sport}_${m.league}`;
      const existing = map.get(key);
      if (existing) {
        existing.matches.push(m);
      } else {
        map.set(key, {
          league: m.league,
          sport: m.sport,
          matches: [m],
        });
      }
    }
    return Array.from(map.values());
  }, [matches]);

  const pageTitle =
    currentSport === 'basketball'
      ? '篮球赛事预测'
      : currentSport === 'football'
        ? '足球赛事预测'
        : '全量赛事预测';

  return (
    <div>
      {/* 顶部标题与指标看板 */}
      <section className={styles.terminalHeader}>
        <div className={styles.thLeft}>
          <h1 className={styles.thTitle}>{pageTitle}</h1>
        </div>

        <div className={styles.thRight}>
          <div className={styles.statCluster}>
            <div className={styles.statPod}>
              <span className={`m-val ${stats.total === 0 ? 'is-zero' : ''}`}>
                {stats.total}
              </span>
              <span className="m-lbl">赛事</span>
            </div>
            <div className={styles.statPod}>
              <span className={`m-val ${stats.live > 0 ? 'c-live' : 'is-zero'}`}>
                {stats.live}
              </span>
              <span className="m-lbl">进行中</span>
            </div>
            <div className={styles.statPod}>
              <span className={`m-val ${stats.upcoming > 0 ? 'c-up' : 'is-zero'}`}>
                {stats.upcoming}
              </span>
              <span className="m-lbl">待开赛</span>
            </div>
            <div className={styles.statPod}>
              <span className={`m-val ${stats.withOdds > 0 ? 'c-odds' : 'is-zero'}`}>
                {stats.withOdds}
              </span>
              <span className="m-lbl">有赔率</span>
            </div>
          </div>
        </div>
      </section>

      {/* 筛选栏：类别 + 日期 + 同步按钮 */}
      <div className={styles.filterBar}>
        <div className={styles.filterGroup}>
          <span className={styles.fgLabel}>类别</span>
          <SportFilter current={currentSport} onChange={handleSportChange} />
        </div>

        {availableDates.length > 0 && (
          <div className={styles.filterGroup}>
            <span className={styles.fgLabel}>日期</span>
            <div className={styles.chipRow}>
              {availableDates.slice(0, 8).map((d) => {
                const isSelected = d === displayDate;
                return (
                  <button
                    key={d}
                    type="button"
                    className={`${styles.chip} ${isSelected ? styles.chipOn : ''}`}
                    onClick={() => handleDateChange(d)}
                    aria-pressed={isSelected}
                  >
                    {d.slice(5)}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        <div className={`${styles.filterGroup} ${styles.filterActions}`}>
          <button
            type="button"
            className={`${styles.btnRefresh} ${refreshMutation.isPending ? styles.busy : ''}`}
            onClick={handleRefresh}
            disabled={refreshMutation.isPending}
            aria-live="polite"
          >
            {refreshMutation.isPending ? '⟳ 同步中...' : '⟳ 同步数据'}
          </button>
        </div>
      </div>

      {/* 状态反馈：加载骨架屏 / 错误 / 列表 / 空状态 */}
      {isLoading ? (
        <div className={styles.skeletonGroup}>
          <Skeleton height={42} />
          <Skeleton lines={6} height={46} />
          <Skeleton height={42} />
          <Skeleton lines={4} height={46} />
        </div>
      ) : isError ? (
        <EmptyState
          title="赛事数据获取失败"
          subtitle="网络连接异常或服务响应超时，请尝试点击上方同步按钮重新获取"
          icon="⚠"
        />
      ) : groups.length > 0 ? (
        <div className={styles.leagueStream}>
          {groups.map((group) => (
            <section
              key={`${group.sport}-${group.league}`}
              className={styles.leagueBlock}
            >
              <div className={styles.lbHeader}>
                <div className={styles.lbLeft}>
                  <span className={styles.lbSport}>
                    <Icon name={group.sport === 'football' ? 'football' : 'basketball'} />
                  </span>
                  <h2 className={styles.lbName}>{group.league}</h2>
                  <span className={styles.lbCount}>{group.matches.length} 场</span>
                </div>
              </div>

              <div className={styles.matchList}>
                {group.matches.map((match) => (
                  <MatchRow key={match.id} match={match} />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <EmptyState
          title="当前日期暂无赛事数据"
          subtitle="点击上方「同步数据」抓取最新赛程，或切换其他日期"
          icon="◈"
        />
      )}
    </div>
  );
};

export default DailyView;
