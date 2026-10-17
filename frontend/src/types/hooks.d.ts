// Ambient type declarations for shared hooks
declare module '@/hooks/useClock' {
  export function useClock(): string;
  export default useClock;
}

declare module '@/hooks/useTheme' {
  export type Theme = 'light' | 'dark';
  export interface UseThemeReturn {
    theme: Theme;
    toggleTheme: () => void;
  }
  export function useTheme(): UseThemeReturn;
  export default useTheme;
}
