import React from 'react';
import { Link, NavLink } from 'react-router-dom';
import { useClock } from '@/hooks/useClock';
import { useTheme } from '@/hooks/useTheme';
import styles from './Navbar.module.css';

export interface NavbarMeta {
  last_refresh?: string;
  [key: string]: unknown;
}

export interface NavbarProps {
  meta?: NavbarMeta;
  lastRefresh?: string;
  className?: string;
}

function formatSyncTime(raw?: string): { short: string; full: string } | null {
  if (!raw) return null;
  const trimmed = raw.trim();
  // Typical format: "2026-10-13 23:42:08" or "2026-10-13T23:42:08"
  if (trimmed.length >= 16 && (trimmed.includes(' ') || trimmed.includes('T'))) {
    const timePart = trimmed.slice(11);
    const short = timePart.slice(0, 5);
    const full = timePart.length >= 8 ? timePart.slice(0, 8) : timePart;
    return { short, full };
  }
  return { short: trimmed, full: trimmed };
}

export const Navbar: React.FC<NavbarProps> = ({
  meta,
  lastRefresh,
  className,
}) => {
  const clock = useClock();
  const { theme, toggleTheme } = useTheme();

  const syncTime = lastRefresh || meta?.last_refresh;
  const formattedSync = formatSyncTime(syncTime);

  const containerClass = className ? `${styles.topNav} ${className}` : styles.topNav;

  return (
    <nav className={containerClass} aria-label="全局导航">
      <div className={styles.navInner}>
        {/* 品牌标识 */}
        <Link to="/" className={styles.navBrand} aria-label="每日体彩预测终端 首页">
          <span className={styles.brandMark} aria-hidden="true">◈</span>
          <span className={styles.brandText}>体彩预测终端</span>
          <span className={styles.brandBadge}>LIVE</span>
        </Link>

        {/* 导航链接 */}
        <div className={styles.navLinks} role="navigation">
          <NavLink
            to="/"
            end
            className={({ isActive }) =>
              `${styles.navLink} ${isActive ? styles.active : ''}`
            }
          >
            今日赛事
          </NavLink>
          <NavLink
            to="/strategy"
            className={({ isActive }) =>
              `${styles.navLink} ${isActive ? styles.active : ''}`
            }
          >
            智能策略
          </NavLink>
          <NavLink
            to="/history"
            className={({ isActive }) =>
              `${styles.navLink} ${isActive ? styles.active : ''}`
            }
          >
            历史复盘
          </NavLink>
        </div>

        {/* 右侧信息与操作 */}
        <div className={styles.navRight}>
          {formattedSync && (
            <span
              className={styles.syncBadge}
              title={`数据最近同步时间：${formattedSync.full}`}
            >
              <span className={styles.syncDot} aria-hidden="true" />
              同步 {formattedSync.short}
            </span>
          )}

          <span className={styles.clockBadge} title="当前北京时间（UTC+8）">
            <span className={styles.clockIcon} aria-hidden="true">◷</span>
            <span className={styles.mono}>{clock || '--:--:--'}</span>
          </span>

          <button
            type="button"
            className={styles.themeToggle}
            onClick={toggleTheme}
            title={theme === 'light' ? '切换为暗色主题' : '切换为亮色主题'}
            aria-label="切换深浅色主题"
          >
            <span className={styles.iconSun} aria-hidden="true">☀</span>
            <span className={styles.iconMoon} aria-hidden="true">☾</span>
          </button>
        </div>
      </div>
    </nav>
  );
};

export default Navbar;
