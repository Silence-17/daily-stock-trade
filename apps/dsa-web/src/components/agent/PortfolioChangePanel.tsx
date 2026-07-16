import { ArrowRightLeft } from 'lucide-react';
import type { VnpyPaperAgentPortfolioChange } from '../../api/vnpyPaperTrading';

type PortfolioChangePanelProps = {
  change?: VnpyPaperAgentPortfolioChange | null;
  currency?: string;
};

const STATUS_LABELS: Record<VnpyPaperAgentPortfolioChange['status'], string> = {
  changed_pending: '已变化，仍有待回报',
  changed: '已入账',
  pending: '等待成交回报',
  planned: '仅生成计划',
  unchanged: '无变化',
};

function formatQuantity(value: number): string {
  if (!Number.isFinite(value)) return '-';
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(value);
}

function formatSignedQuantity(value: number): string {
  if (!Number.isFinite(value)) return '-';
  return `${value > 0 ? '+' : ''}${formatQuantity(value)}`;
}

function formatMoney(value: number, currency: string): string {
  if (!Number.isFinite(value)) return '-';
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency,
    maximumFractionDigits: 2,
  }).format(value);
}

function statusTone(status: VnpyPaperAgentPortfolioChange['status']): string {
  if (status === 'changed') return 'border-success/30 bg-success/10 text-success';
  if (status === 'changed_pending' || status === 'pending') {
    return 'border-warning/30 bg-warning/10 text-warning';
  }
  return 'border-border bg-card text-secondary-text';
}

export function PortfolioChangePanel({ change, currency = 'CNY' }: PortfolioChangePanelProps) {
  if (!change) return null;
  const statusLabel = STATUS_LABELS[change.status] || change.status;
  const items = Array.isArray(change.items) ? change.items : [];

  return (
    <section data-testid="portfolio-change-panel" className="rounded-lg border border-border bg-surface px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
          <ArrowRightLeft className="h-4 w-4 text-cyan" />
          本轮持仓变化
        </div>
        <span className={`rounded-full border px-2 py-1 text-xs ${statusTone(change.status)}`}>
          {statusLabel}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary-text">
        <span>已入账计划 {change.bookedPlanCount || 0}</span>
        <span>待回报 {change.pendingPlanCount || 0}</span>
        <span>仅计划 {change.plannedPlanCount || 0}</span>
      </div>

      {items.length > 0 ? (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[680px] border-collapse text-xs">
            <thead className="text-left text-secondary-text">
              <tr className="border-b border-border">
                <th className="px-2 py-2 font-semibold">标的</th>
                <th className="px-2 py-2 text-right font-semibold">买入</th>
                <th className="px-2 py-2 text-right font-semibold">卖出</th>
                <th className="px-2 py-2 text-right font-semibold">净变化</th>
                <th className="px-2 py-2 text-right font-semibold">买入额</th>
                <th className="px-2 py-2 text-right font-semibold">卖出额</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={`${item.market}-${item.symbol}`} className="border-b border-border/70 last:border-b-0">
                  <td className="px-2 py-2">
                    <div className="font-mono font-semibold text-foreground">{item.symbol}</div>
                    <div className="mt-0.5 text-secondary-text">{item.name || item.market}</div>
                  </td>
                  <td className="px-2 py-2 text-right text-success">{formatQuantity(item.buyQuantity)}</td>
                  <td className="px-2 py-2 text-right text-warning">{formatQuantity(item.sellQuantity)}</td>
                  <td className={`px-2 py-2 text-right font-semibold ${item.netQuantity >= 0 ? 'text-success' : 'text-warning'}`}>
                    {formatSignedQuantity(item.netQuantity)}
                  </td>
                  <td className="px-2 py-2 text-right text-secondary-text">{formatMoney(item.buyNotional, currency)}</td>
                  <td className="px-2 py-2 text-right text-secondary-text">{formatMoney(item.sellNotional, currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="mt-3 border-t border-border pt-3 text-sm text-secondary-text">
          本轮尚未产生已入账的持仓变化
        </div>
      )}
    </section>
  );
}
