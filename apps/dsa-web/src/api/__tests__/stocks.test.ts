import { beforeEach, describe, expect, it, vi } from 'vitest';
import { stocksApi } from '../stocks';

const get = vi.hoisted(() => vi.fn());

vi.mock('../index', () => ({
  default: { get, post: vi.fn() },
}));

describe('stocksApi', () => {
  beforeEach(() => {
    get.mockReset();
  });

  it('loads industry boards and camelCases response fields', async () => {
    get.mockResolvedValueOnce({
      data: {
        boards: [
          {
            rank: 1,
            code: 'BK1036',
            name: '半导体',
            change_pct: 2.31,
            up_count: 86,
            down_count: 12,
            leader: '中芯国际',
            leader_change: 8.2,
            source: 'a_stock_data_eastmoney',
            data_quality: 'realtime',
          },
        ],
        total: 1,
        source: 'AStockDataFetcher',
        updated_at: '2026-07-01T09:30:00+00:00',
        data_quality: 'realtime',
        message: '实时涨跌幅、涨跌家数与领涨股来自 EastMoney 行业板块接口。',
      },
    });

    const result = await stocksApi.getIndustryBoards();

    expect(get).toHaveBeenCalledWith('/api/v1/stocks/industry-boards');
    expect(result.boards[0].changePct).toBe(2.31);
    expect(result.boards[0].upCount).toBe(86);
    expect(result.boards[0].leaderChange).toBe(8.2);
    expect(result.boards[0].dataQuality).toBe('realtime');
    expect(result.dataQuality).toBe('realtime');
    expect(result.updatedAt).toBe('2026-07-01T09:30:00+00:00');
  });
});
