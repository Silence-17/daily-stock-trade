import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import IndustryBoardsPage from '../IndustryBoardsPage';

const getIndustryBoards = vi.hoisted(() => vi.fn());

vi.mock('../../api/stocks', () => ({
  stocksApi: {
    getIndustryBoards,
  },
}));

const response = {
  boards: [
    {
      rank: 1,
      code: 'BK1036',
      name: '半导体',
      changePct: 2.31,
      upCount: 86,
      downCount: 12,
      leader: '中芯国际',
      leaderChange: 8.2,
      source: 'a_stock_data_eastmoney',
      dataQuality: 'realtime',
    },
  ],
  total: 1,
  source: 'AStockDataFetcher',
  updatedAt: '2026-07-01T09:30:00+00:00',
  dataQuality: 'realtime',
  message: '实时涨跌幅、涨跌家数与领涨股来自 EastMoney 行业板块接口。',
};

describe('IndustryBoardsPage', () => {
  beforeEach(() => {
    getIndustryBoards.mockReset();
    getIndustryBoards.mockResolvedValue(response);
  });

  it('renders industry board rows from the stocks API', async () => {
    render(
      <UiLanguageProvider>
        <IndustryBoardsPage />
      </UiLanguageProvider>,
    );

    expect(await screen.findByRole('heading', { name: '行业板块' })).toBeInTheDocument();
    expect(screen.getAllByText('半导体').length).toBeGreaterThan(0);
    expect(screen.getByText('+2.31%')).toBeInTheDocument();
    expect(screen.getByText('上涨 86')).toBeInTheDocument();
    expect(screen.getByText('下跌 12')).toBeInTheDocument();
    expect(screen.getByText('中芯国际')).toBeInTheDocument();
    expect(screen.getByText('AStockDataFetcher')).toBeInTheDocument();
    expect(screen.getByText('实时行情')).toBeInTheDocument();
  });

  it('refreshes the board list on demand', async () => {
    render(
      <UiLanguageProvider>
        <IndustryBoardsPage />
      </UiLanguageProvider>,
    );

    await screen.findByRole('heading', { name: '行业板块' });
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => expect(getIndustryBoards).toHaveBeenCalledTimes(2));
  });

  it('labels directory fallback data instead of presenting it as realtime ranking', async () => {
    getIndustryBoards.mockResolvedValue({
      boards: [
        {
          rank: 1,
          code: '101024',
          name: '房地产开发',
          source: 'a_stock_data_eastmoney_reportapi_fallback',
          dataQuality: 'directory_fallback',
        },
      ],
      total: 1,
      source: 'AStockDataFetcher',
      updatedAt: '2026-07-01T09:30:00+00:00',
      dataQuality: 'directory_fallback',
      message: 'EastMoney 实时行业板块暂不可用，当前显示 reportapi 行业目录；涨跌幅、涨跌家数与领涨股不可用于实时排序。',
    });

    render(
      <UiLanguageProvider>
        <IndustryBoardsPage />
      </UiLanguageProvider>,
    );

    expect(await screen.findByText('当前为备用目录')).toBeInTheDocument();
    expect(screen.getByText(/reportapi 行业目录/)).toBeInTheDocument();
    expect(screen.getAllByText('备用目录').length).toBeGreaterThan(0);
    expect(screen.getByText('房地产开发')).toBeInTheDocument();
  });
});
