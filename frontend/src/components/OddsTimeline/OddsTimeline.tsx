import React from 'react';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  CartesianGrid,
} from 'recharts';
import type { OddsHistoryItem } from '@/types';
import styles from './OddsTimeline.module.css';

export interface OddsTimelineProps {
  history: OddsHistoryItem[];
  sport?: 'football' | 'basketball';
  height?: number;
}

interface ChartDataPoint {
  time: string;
  fullTime: string;
  homeWin: number;
  draw?: number;
  awayWin: number;
}

export const OddsTimeline: React.FC<OddsTimelineProps> = ({
  history,
  sport = 'football',
  height = 260,
}) => {
  if (!history || history.length === 0) {
    return (
      <div className={styles.emptyWrap}>
        <span className={styles.emptyText}>暂无历史盘口变动记录（仅记录赛前有效波动）</span>
      </div>
    );
  }

  // 格式化时间与数值
  const chartData: ChartDataPoint[] = history.map((item) => {
    const d = new Date(item.captured_at);
    const timeLabel = isNaN(d.getTime())
      ? item.captured_at.slice(11, 16) || item.captured_at
      : `${d.getMonth() + 1}-${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(
          d.getMinutes()
        ).padStart(2, '0')}`;

    const hw = typeof item.odds?.home_win === 'number' ? item.odds.home_win : parseFloat(String(item.odds?.home_win || 0));
    const dw = typeof item.odds?.draw === 'number' ? item.odds.draw : parseFloat(String(item.odds?.draw || 0));
    const aw = typeof item.odds?.away_win === 'number' ? item.odds.away_win : parseFloat(String(item.odds?.away_win || 0));

    const pt: ChartDataPoint = {
      time: timeLabel,
      fullTime: item.captured_at,
      homeWin: Number(hw.toFixed(2)),
      awayWin: Number(aw.toFixed(2)),
    };

    if (sport === 'football' && dw > 0) {
      pt.draw = Number(dw.toFixed(2));
    }

    return pt;
  });

  return (
    <div className={styles.container} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={chartData} margin={{ top: 10, right: 20, left: 0, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
          <XAxis
            dataKey="time"
            stroke="var(--border-strong)"
            tick={{ fill: 'var(--text-secondary)', fontSize: 11 }}
          />
          <YAxis
            domain={['auto', 'auto']}
            stroke="var(--border-strong)"
            tick={{ fill: 'var(--text-secondary)', fontSize: 11, fontFamily: 'var(--mono)' }}
            width={45}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: 'var(--bg-elev)',
              borderColor: 'var(--border-strong)',
              borderRadius: 'var(--radius)',
              color: 'var(--text-primary)',
              boxShadow: 'var(--shadow)',
              fontSize: 12,
              fontFamily: 'var(--mono)',
            }}
            labelStyle={{ color: 'var(--text-muted)', marginBottom: 4 }}
          />
          <Legend
            wrapperStyle={{
              fontSize: 12,
              fontWeight: 700,
              paddingTop: 8,
              color: 'var(--text-primary)',
            }}
          />
          <Line
            type="monotone"
            name="主胜赔率"
            dataKey="homeWin"
            stroke="var(--side-home)"
            strokeWidth={2.5}
            dot={{ r: 3, fill: 'var(--side-home)' }}
            activeDot={{ r: 5 }}
          />
          {sport === 'football' && (
            <Line
              type="monotone"
              name="平局赔率"
              dataKey="draw"
              stroke="var(--side-draw)"
              strokeWidth={2}
              dot={{ r: 3, fill: 'var(--side-draw)' }}
              activeDot={{ r: 5 }}
            />
          )}
          <Line
            type="monotone"
            name="客胜赔率"
            dataKey="awayWin"
            stroke="var(--side-away)"
            strokeWidth={2.5}
            dot={{ r: 3, fill: 'var(--side-away)' }}
            activeDot={{ r: 5 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
};

export default OddsTimeline;
