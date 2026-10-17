import React from 'react';
import {
  ResponsiveContainer,
  RadarChart as RechartsRadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  Radar,
  Legend,
  Tooltip,
} from 'recharts';
import type { RadarData } from '@/types';
import styles from './RadarChart.module.css';

export interface RadarChartProps {
  data: RadarData;
  homeTeam: string;
  awayTeam: string;
  height?: number;
}

interface RadarDataPoint {
  metric: string;
  home: number;
  away: number;
}

export const RadarChart: React.FC<RadarChartProps> = ({
  data,
  homeTeam,
  awayTeam,
  height = 320,
}) => {
  if (!data?.labels || data.labels.length === 0) {
    return (
      <div className={styles.emptyWrap}>
        <span className={styles.emptyText}>暂无战力雷达数据</span>
      </div>
    );
  }

  const chartData: RadarDataPoint[] = data.labels.map((label, index) => ({
    metric: label,
    home: data.home[index] ?? 0,
    away: data.away[index] ?? 0,
  }));

  return (
    <div className={styles.container} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <RechartsRadarChart cx="50%" cy="50%" outerRadius="75%" data={chartData}>
          <PolarGrid stroke="var(--border)" strokeDasharray="3 3" />
          <PolarAngleAxis
            dataKey="metric"
            tick={{ fill: 'var(--text-secondary)', fontSize: 11, fontWeight: 600 }}
          />
          <PolarRadiusAxis
            angle={30}
            domain={[0, 100]}
            stroke="var(--border-strong)"
            tick={{ fill: 'var(--text-faint)', fontSize: 10 }}
            axisLine={false}
          />
          <Radar
            name={homeTeam}
            dataKey="home"
            stroke="var(--side-home)"
            fill="var(--side-home)"
            fillOpacity={0.22}
            strokeWidth={2}
          />
          <Radar
            name={awayTeam}
            dataKey="away"
            stroke="var(--side-away)"
            fill="var(--side-away)"
            fillOpacity={0.22}
            strokeWidth={2}
          />
          <Legend
            wrapperStyle={{
              fontSize: 12,
              fontWeight: 700,
              paddingTop: 10,
              color: 'var(--text-primary)',
            }}
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
            itemStyle={{ color: 'var(--text-primary)', fontWeight: 600 }}
          />
        </RechartsRadarChart>
      </ResponsiveContainer>
    </div>
  );
};

export default RadarChart;
