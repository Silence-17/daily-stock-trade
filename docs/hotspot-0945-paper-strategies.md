# 09:45 热点策略双账户 30 日模拟

该实验同时运行两个隔离的本地模拟账户：

| 策略 | AlphaSift 候选池 | 初始资金 | 单笔目标 | 最多持仓 |
| --- | --- | ---: | ---: | ---: |
| `hotspot_oversold_reversal_0945_v1` | `oversold_reversal` | 100,000 元 | 当前权益 25% | 2 |
| `hotspot_trend_reacceleration_0945_v1` | `momentum_quality` | 100,000 元 | 当前权益 30% | 2 |

两个账户使用 `broker=paper_0945`，不会切换或污染当前 `vnpy_paper` 账户。账户、成交、费用和每日净值进入 Portfolio 账本；活动进度及逐日决策证据写入 `data/hotspot_0945_paper/`，该目录属于本地运行数据，不入库。

## 前向执行契约

- 工作日 09:38 冻结两个 AlphaSift 候选池，09:45 获取带供应商时间戳的实时开盘价、最高价、最低价、最新价和累计量价。
- 行情必须不超过 120 秒；缺时间戳、字段缺失或错过 09:46:30 信号窗口时失败关闭。
- 超跌策略要求 09:45 相对开盘至少上涨 1.5%、自低点反弹至少 1%；趋势策略要求相对开盘至少上涨 2%、自低点反弹至少 0.5%且重新站上累计 VWAP。
- 两者都只允许综合排名前二、位于早盘区间上部的候选进入下单判断。
- 信号使用 09:45 快照，成交使用 09:46 的第二份实时快照并加入 5 bps 滑点，避免用信号价伪造成交。
- A 股数量向下取整为 100 股整手，执行双边万 0.8、最低 5 元佣金，双边万 0.1 过户费和卖出万 5 印花税。
- 每日 14:55 运行退出风控并持久化收盘净值；T+1、止损、止盈、移动止盈和最长持仓期均生效。
- 只有当天开盘决策和收盘估值证据都完整时，才推进一个真实交易日。每个账户累计 30 个合格交易日后停止新买入，并在后续可卖时清仓。

## 安装与检查

首次创建账户：

```powershell
python scripts/run_hotspot_0945_paper_30d.py --setup
```

管理员 PowerShell 安装 Windows 计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_hotspot_0945_paper_30d_task.ps1
```

任务 `DailyStockAnalysis-Hotspot0945Paper30D` 在工作日 09:38 和 14:55 唤醒；非交易日由交易日历失败关闭。查看当前进度：

```powershell
python scripts/run_hotspot_0945_paper_30d.py --phase status
```

此功能只做模拟交易，不连接真实券商，也不构成投资建议。

