import { createBrowserRouter } from 'react-router-dom';
import { Layout } from '@/components/Layout';
import { DailyView } from '@/features/DailyView';
import { MatchDetail } from '@/features/MatchDetail';
import { StrategyView } from '@/features/StrategyView';
import { HistoryView } from '@/features/HistoryView';

export const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: '/', element: <DailyView /> },
      { path: '/match/:id', element: <MatchDetail /> },
      { path: '/strategy', element: <StrategyView /> },
      { path: '/history', element: <HistoryView /> },
    ],
  },
]);
