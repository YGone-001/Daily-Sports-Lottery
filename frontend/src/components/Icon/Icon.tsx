import React from 'react';
import styles from './Icon.module.css';

export type IconName =
  | 'football'
  | 'basketball'
  | 'flame'
  | 'target'
  | 'scale'
  | 'coins'
  | 'radar'
  | 'list';

export interface IconProps extends React.SVGAttributes<SVGSVGElement> {
  name: IconName;
  className?: string;
  size?: number | string;
}

export const Icon: React.FC<IconProps> = ({
  name,
  className,
  size,
  style,
  ...restProps
}) => {
  const sizeStyle: React.CSSProperties = size
    ? {
        width: typeof size === 'number' ? `${size}px` : size,
        height: typeof size === 'number' ? `${size}px` : size,
        ...style,
      }
    : style || {};

  const combinedClassName = className ? `${styles.icon} ${className}` : styles.icon;

  const renderIconPaths = () => {
    switch (name) {
      case 'football':
        return (
          <>
            <circle cx="12" cy="12" r="8.5" />
            <path d="M12 7.6l3.4 2.5-1.3 4h-4.2l-1.3-4z" />
          </>
        );
      case 'basketball':
        return (
          <>
            <circle cx="12" cy="12" r="8.5" />
            <path d="M12 3.5v17M3.5 12h17" />
            <path d="M6.2 6.2c3.4 3.4 3.4 8.2 0 11.6M17.8 6.2c-3.4 3.4-3.4 8.2 0 11.6" />
          </>
        );
      case 'flame':
        return (
          <path d="M12 21a5.5 5.5 0 0 0 5.5-5.5c0-3.6-3.4-5.2-5.5-12.5-2.1 7.3-5.5 8.9-5.5 12.5A5.5 5.5 0 0 0 12 21z" />
        );
      case 'target':
        return (
          <>
            <circle cx="12" cy="12" r="8.5" />
            <circle cx="12" cy="12" r="4" />
            <circle cx="12" cy="12" r="1" style={{ fill: 'currentColor', stroke: 'none' }} />
          </>
        );
      case 'scale':
        return (
          <>
            <path d="M12 4v16M7 20h10M5 8h14" />
            <path d="M5 8l-2.5 5h5zM19 8l-2.5 5h5z" />
          </>
        );
      case 'coins':
        return (
          <>
            <ellipse cx="12" cy="7.5" rx="7" ry="3" />
            <path d="M5 7.5v5c0 1.7 3.1 3 7 3s7-1.3 7-3v-5" />
            <path d="M5 12.5v4c0 1.7 3.1 3 7 3s7-1.3 7-3v-4" />
          </>
        );
      case 'radar':
        return (
          <>
            <path d="M12 2.8l7.9 4.6v9.2L12 21.2l-7.9-4.6V7.4z" />
            <path d="M12 12V2.8M12 12l7.9 4.6M12 12l-7.9 4.6" />
          </>
        );
      case 'list':
        return (
          <>
            <path d="M9 6h11M9 12h11M9 18h11" />
            <path d="M4 6h1.5M4 12h1.5M4 18h1.5" />
          </>
        );
      default:
        return null;
    }
  };

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      className={combinedClassName}
      style={sizeStyle}
      {...restProps}
    >
      {renderIconPaths()}
    </svg>
  );
};

export default Icon;
