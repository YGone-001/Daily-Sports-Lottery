import React, { useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useMatches, useDates, useRefresh, useStatus } from '@/hooks';
import type { Match } from '@/types/match';
import { SportFilter } from '@/components/SportFilter';
import { EmptyState } from '@/components/EmptyState';
import { Skeleton } from '@/components/Skeleton';
import { Icon } from '@/components/Icon';
import { MatchRow } from './MatchRow';
import { availableDatesFor, resolveDisplayDate, shouldClearDate } from './dateSelection';
import { describeOperatingState, describeRefreshError } from './refreshMessages';
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
  const allDates = datesData?.dates || [];
  const datesBySport = datesData?.dates_by_sport;
  // 运动感知：选择「篮球」时不能默认落在只有足球的日期上，否则「当日无篮球」
  // 会被误报成「没有比赛」。判定逻辑见 ./dateSelection（有单元测试）。
  const availableDates = availableDatesFor(currentSport, allDates, datesBySport);
  const displayDate = resolveDisplayDate(rawDate, availableDates);

  const {
    data: matches = [],
    isLoading,
    isError,
  } = useMatches(displayDate, currentSport);

  const refreshMutation = useRefresh();
  const { data: status } = useStatus();
  // 运行时能力由 /api/status 报告：主写开关 + 本地 UI 开关同时开启时界面同步才可用。
  // 状态缺失（请求失败）时不猜测：按钮保持可点，点击后的 403 会由错误横幅明确陈述。
  const refreshAvailable = status?.runtime
    ? status.runtime.refresh_available_via_ui
    : undefined;
  // 后端是运行状态的权威：正在进行/已完成/失败/只读均由其刷新标记推导。
  const operatingNotice = describeOperatingState(status?.runtime);
  const refreshInProgress =
    refreshMutation.isPending || status?.runtime?.operating_state === 'REFRESH_IN_PROGRESS';

  const handleSportChange = (sport: string) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (sport && sport !== 'all') {
        next.set('sport', sport);
      } else {
        next.delete('sport');
      }
      // 换运动后若当前日期在新运动下没有比赛，清掉过期日期，避免误导性空列表。
      if (shouldClearDate(next.get('date'), sport, allDates, datesBySport)) {
        next.delete('date');
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

  // 同步结果必须可见：拒绝 / 未授权 / 忙 / 网络失败各自有明确的中文说明，
  // 不得让按钮在失败后悄悄回到空闲状态（AGENTS.md §4.35、需求 §D.2）。
  const refreshError = refreshMutation.isError
    ? describeRefreshError(refreshMutation.error)
    : null;

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
            className={`${styles.btnRefresh} ${refreshInProgress ? styles.busy : ''}`}
            onClick={handleRefresh}
            disabled={refreshInProgress || refreshAvailable === false}
            aria-live="polite"
            title={
              refreshAvailable === false
                ? '当前部署为只读模式，界面同步未启用'
                : refreshInProgress
                  ? '同步正在进行'
                  : undefined
            }
          >
            {refreshInProgress
              ? '⟳ 同步中...'
              : refreshAvailable === false
                ? '⟳ 同步未启用'
                : '⟳ 同步数据'}
          </button>
        </div>
      </div>

      {/* 运行状态提示：只读 / 需要令牌 / 同步进行中 / 已完成 / 失败都必须可区分，
          且均由后端 /api/status 的权威状态推导 */}
      {operatingNotice && !refreshError && (
        <div
          className={`${styles.refreshNotice} ${
            operatingNotice.tone === 'success'
              ? styles.refreshNoticeSuccess
              : operatingNotice.tone === 'progress'
                ? styles.refreshNoticeProgress
                : styles.refreshNoticeInfo
          }`}
          role="status"
        >
          <span className={styles.refreshNoticeIcon}>
            {operatingNotice.tone === 'success'
              ? '✓'
              : operatingNotice.tone === 'progress'
                ? '⟳'
                : '◈'}
          </span>
          <span className={styles.refreshNoticeText}>{operatingNotice.text}</span>
        </div>
      )}

      {/* 数据新鲜度（Workstream D）：已知未来开赛时间 ≠ 提供商实时核实；
          过期待赛与未核实开赛时间必须可见 */}
      {status?.freshness?.per_sport && (
        <p className={styles.freshnessLine}>
          {Object.entries(status.freshness.per_sport)
            .map(([sport, f]) => {
              const label = sport === 'football' ? '足球' : '篮球';
              const parts = [
                `未来开赛 ${f.known_future_kickoff} 场（提供商核实 ${f.provider_schedule_verified}）`,
              ];
              if (f.future_kickoff_unknown > 0)
                parts.push(`开赛时间未定 ${f.future_kickoff_unknown} 场`);
              if (f.past_kickoff_stored_upcoming + f.unknown_kickoff_past_date > 0)
                parts.push(
                  `已过开赛/过期未确认 ${f.past_kickoff_stored_upcoming + f.unknown_kickoff_past_date} 场`,
                );
              if (f.latest_market_event_date)
                parts.push(`最新盘口日 ${f.latest_market_event_date.slice(5)}`);
              return `${label}：${parts.join(' · ')}`;
            })
            .join(' ｜ ')}
        </p>
      )}

      {/* 同步结果反馈：服务端拒绝/未授权/忙碌/网络失败都必须显式可见 */}
      {refreshError && (
        <div className={styles.refreshNotice} role="alert">
          <span className={styles.refreshNoticeIcon}>⚠</span>
          <span className={styles.refreshNoticeText}>{refreshError}</span>
        </div>
      )}

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
                  <MatchRow
                    key={match.id}
                    match={
                      match.kickoff_time_known === false
                        ? { ...match, time: '待定' }
                        : match
                    }
                  />
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
