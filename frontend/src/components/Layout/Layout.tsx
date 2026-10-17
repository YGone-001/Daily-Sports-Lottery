import React from 'react';
import { Outlet } from 'react-router-dom';
import { Navbar, type NavbarMeta } from '../Navbar';
import { Footer } from '../Footer';
import { useStatus } from '@/hooks';
import styles from './Layout.module.css';

export interface LayoutProps {
  children?: React.ReactNode;
  meta?: NavbarMeta;
  lastRefresh?: string;
  className?: string;
  contentClassName?: string;
}

export const Layout: React.FC<LayoutProps> = ({
  children,
  meta,
  lastRefresh,
  className,
  contentClassName,
}) => {
  const { data: status } = useStatus();
  const wrapperClass = className
    ? `${styles.layoutWrapper} ${className}`
    : styles.layoutWrapper;

  const contentClass = contentClassName
    ? `${styles.mainContent} ${contentClassName}`
    : styles.mainContent;

  return (
    <div className={wrapperClass}>
      <Navbar meta={meta ?? status?.meta} lastRefresh={lastRefresh} />
      <main className={contentClass}>
        {children ?? <Outlet />}
      </main>
      <Footer />
    </div>
  );
};

export default Layout;
