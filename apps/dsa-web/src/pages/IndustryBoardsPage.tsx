import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw, Search, TrendingDown, TrendingUp } from 'lucide-react';
import { stocksApi, type IndustryBoard } from '../api/stocks';
import { Button, InlineAlert } from '../components/common';
import { AppPage } from '../components/common/AppPage';

const formatPercent = (value: unknown) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  const numeric = Number(value);
  return `${numeric > 0 ? '+' : ''}${numeric.toFixed(2)}%`;
};

const formatDateTime = (value: string) => {
  if (!value) {
    return '-';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
};

const changeClassName = (value: unknown) => {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) {
    return 'text-secondary-text';
  }
  return numeric > 0 ? 'text-danger' : 'text-success';
};

const trendIcon = (value: unknown) => {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) {
    return null;
  }
  return numeric > 0 ? <TrendingUp className="h-4 w-4" /> : <TrendingDown className="h-4 w-4" />;
};

const countText = (label: string, value: unknown) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return `${label} -`;
  }
  return `${label} ${Number(value).toFixed(0)}`;
};

const IndustryBoardsPage: React.FC = () => {
  const [boards, setBoards] = useState<IndustryBoard[]>([]);
  const [source, setSource] = useState('');
  const [updatedAt, setUpdatedAt] = useState('');
  const [dataQuality, setDataQuality] = useState('unavailable');
  const [qualityMessage, setQualityMessage] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');

  const loadBoards = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const result = await stocksApi.getIndustryBoards();
      setBoards(result.boards || []);
      setSource(result.source || '');
      setUpdatedAt(result.updatedAt || '');
      setDataQuality(result.dataQuality || 'unknown');
      setQualityMessage(result.message || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : '行业板块列表加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadBoards();
  }, [loadBoards]);

  const filteredBoards = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) {
      return boards;
    }
    return boards.filter((board) => {
      const name = String(board.name || '').toLowerCase();
      const code = String(board.code || '').toLowerCase();
      const leader = String(board.leader || '').toLowerCase();
      return name.includes(keyword) || code.includes(keyword) || leader.includes(keyword);
    });
  }, [boards, query]);

  const rankedBoards = useMemo(
    () => boards.filter((board) => Number.isFinite(Number(board.changePct))),
    [boards],
  );
  const leaders = useMemo(() => rankedBoards.slice(0, 3), [rankedBoards]);
  const laggards = useMemo(() => rankedBoards.slice(-3).reverse(), [rankedBoards]);
  const isRealtime = dataQuality === 'realtime';

  return (
    <AppPage className="space-y-4">
      <section className="flex flex-col gap-4 border-b border-border/70 pb-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">行业板块</h1>
          <p className="mt-2 text-sm text-secondary-text">
            {boards.length > 0 ? `共 ${boards.length} 个行业板块 · 更新 ${formatDateTime(updatedAt)}` : '正在读取行业板块数据'}
          </p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-secondary-text" />
            <input
              className="h-10 w-full rounded-xl border border-border bg-surface pl-9 pr-3 text-sm text-foreground outline-none transition-colors focus:border-cyan sm:w-64"
              value={query}
              aria-label="搜索行业板块"
              placeholder="名称、代码、领涨股"
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          <Button variant="secondary" isLoading={loading} loadingText="刷新中..." onClick={() => void loadBoards()}>
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
        </div>
      </section>

      {error ? (
        <InlineAlert
          variant="danger"
          title="行业板块加载失败"
          message={error}
          action={(
            <Button variant="outline" size="sm" onClick={() => void loadBoards()}>
              重试
            </Button>
          )}
        />
      ) : null}

      {!error && !isRealtime && qualityMessage ? (
        <InlineAlert
          variant="warning"
          title="当前为备用目录"
          message={qualityMessage}
        />
      ) : null}

      <section className="grid gap-3 md:grid-cols-3">
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">数据源</p>
          <p className="mt-1 text-sm font-semibold text-foreground">{source || '-'}</p>
          <p className="mt-1 text-xs text-secondary-text">{isRealtime ? '实时行情' : '备用目录'}</p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">领涨</p>
          <p className="mt-1 text-sm font-semibold text-foreground">
            {leaders.map((item) => item.name).filter(Boolean).join('、') || '-'}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">领跌</p>
          <p className="mt-1 text-sm font-semibold text-foreground">
            {laggards.map((item) => item.name).filter(Boolean).join('、') || '-'}
          </p>
        </div>
      </section>

      <section className="overflow-hidden rounded-xl border border-border bg-card/95">
        <div className="flex items-center justify-between gap-3 border-b border-border bg-surface/80 px-4 py-3">
          <h2 className="text-sm font-semibold text-foreground">板块列表</h2>
          <span className="text-xs text-secondary-text">{filteredBoards.length} / {boards.length}</span>
        </div>

        {loading && boards.length === 0 ? (
          <div className="px-5 py-10 text-center text-sm text-secondary-text">正在加载行业板块...</div>
        ) : filteredBoards.length === 0 ? (
          <div className="px-5 py-10 text-center text-sm text-secondary-text">暂无匹配板块</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[860px] border-collapse text-sm">
              <thead className="bg-surface text-left text-xs text-secondary-text">
                <tr>
                  <th className="w-16 px-4 py-3 font-semibold">#</th>
                  <th className="px-4 py-3 font-semibold">板块</th>
                  <th className="px-4 py-3 font-semibold">涨跌幅</th>
                  <th className="px-4 py-3 font-semibold">涨跌家数</th>
                  <th className="px-4 py-3 font-semibold">领涨股</th>
                  <th className="px-4 py-3 font-semibold">领涨股涨幅</th>
                  <th className="px-4 py-3 font-semibold">来源</th>
                </tr>
              </thead>
              <tbody>
                {filteredBoards.map((board) => (
                  <tr key={`${board.code || board.name}-${board.rank}`} className="border-t border-border align-middle transition-colors hover:bg-hover/50">
                    <td className="px-4 py-3 text-secondary-text">{board.rank}</td>
                    <td className="px-4 py-3">
                      <div className="font-semibold text-foreground">{board.name}</div>
                      <div className="mt-1 font-mono text-xs text-secondary-text">{board.code || '-'}</div>
                    </td>
                    <td className={`px-4 py-3 font-semibold ${changeClassName(board.changePct)}`}>
                      <span className="inline-flex items-center gap-1">
                        {trendIcon(board.changePct)}
                        {formatPercent(board.changePct)}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-secondary-text">
                      <span className="mr-3 text-danger">{countText('上涨', board.upCount)}</span>
                      <span className="text-success">{countText('下跌', board.downCount)}</span>
                    </td>
                    <td className="px-4 py-3 font-medium text-foreground">{board.leader || '-'}</td>
                    <td className={`px-4 py-3 font-semibold ${changeClassName(board.leaderChange)}`}>
                      {formatPercent(board.leaderChange)}
                    </td>
                    <td className="px-4 py-3 text-secondary-text">{board.source || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </AppPage>
  );
};

export default IndustryBoardsPage;
