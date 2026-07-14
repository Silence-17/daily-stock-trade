# vn.py 模拟交易说明

本功能在 Web 侧新增“模拟交易”入口，后端提供 `vn.py` 风格的 paper trading API。默认实现写入 DSA 本地持仓账本；当运行进程显式注入 vn.py `MainEngine` 并配置 gateway 名称时，可选择把委托提交到 vn.py `MainEngine.send_order`。该能力仍定位为可选模拟/桥接能力，不默认连接实盘券商、CTP 网关或任何真实交易通道。

## 能力边界

- 模拟账户使用现有 Portfolio 账本，默认券商标识为 `vnpy_paper`，账户名为 `vn.py 模拟交易`。
- 状态接口用 `available` 表示本地模拟账本是否可用，用 `vnpy_available` 表示当前 Python 环境是否可导入 `vnpy`；未安装 `vnpy` 不影响本地模拟账本使用。
- 后端新增可选 vn.py adapter 诊断：`diagnostics.vnpy_adapter` 会说明是否能导入 `vnpy.trader.object.OrderRequest` / `CancelRequest` 与常量枚举，并保留 DSA 委托到 vn.py `OrderRequest` / `CancelRequest` 字段的基础映射。`diagnostics.vnpy_bridge` 会说明当前进程是否已注入 `MainEngine`、是否配置 `vnpy_gateway_name`，以及是否可通过 `MainEngine.send_order` 提交委托、通过 `MainEngine.cancel_order` 发起撤单、通过 `MainEngine.get_order` / `get_all_trades` 执行超时订单对账。
- 手动模拟委托支持买入和卖出；A 股买入会按 100 股一手向下取整，按金额下单不足一手时返回 `cash_below_min_lot`，不会写入成交。
- 未传成交价时，后端会尝试读取 DSA 实时行情；价格不可用、金额不足、数量取整后为 0、重复自动单或超卖时不会写入成交。
- 自动模拟交易默认关闭。开启后，runtime scheduler 会按页面设置的间隔调用 AlphaSift 选股，并对候选票提交本地模拟买入；模拟交易总开关开启时始终注册 `vnpy_paper_auto_retry`，首次注册立即扫描一次并按 1 到 5 分钟间隔恢复已有订单，自动买入开启时再注册 `vnpy_paper_auto_trade`。API 启动期创建或注入的 MainEngine/EventEngine 会传给这两个后台任务使用，不再只对 Web 请求可见。若当前执行模式强制交易时段限制且服务启动时不在交易窗口内，首次自动买入会延迟到下一开盘窗口触发，避免按固定间隔从盘后启动后每天都在盘后跳过。
- 页面提供“暂停自动买入”按钮，确认后会关闭 `auto_trade_enabled` 并触发 runtime scheduler 重新 reconcile，停止后续后台自动买入；已有 `submitted` / `part_filled` / `cancel_requested` 委托仍由恢复任务对账和安全归档，暂停期间不会重提失败计划。暂停后同位置提供“恢复自动买入”按钮，可重新打开后台自动买入并触发 scheduler reconcile。
- 自动模拟交易支持 `paper`、`vnpy_paper`、`dry_run` 和 `manual_approval` 四种执行模式：`paper` 会写入本地模拟成交，`vnpy_paper` 会把通过风控的买入计划提交给注入的 vn.py `MainEngine` 并记录为 `submitted`，随后可通过 vn.py 成交回报同步入口回写为 `filled`，`dry_run` 只生成交易计划和审计记录，`manual_approval` 会生成待审批交易计划，用户在页面确认后才写入 Portfolio 账本。
- 交易计划支持受控恢复：`manual_approval`、`paper`、`vnpy_paper` 计划若变为 `failed` 或可恢复的 `skipped`，页面可触发“重试提交”。后端会重新走对应执行路由（`vnpy_paper` 走 vn.py bridge，其余走本地 paper）、现有持仓检查和 retry 次数/冷却控制，并回写交易计划、候选决策和 run 计数。数据质量、行情灯、组合风控等不可恢复跳过原因仍不能通过该入口绕过。`vnpy_paper` 的 `submitted` / `part_filled` 计划可在页面发起“撤单”，后端调用注入的 `MainEngine.cancel_order` 并把计划临时标记为 `cancel_requested`，最终是否撤销仍以后续 vn.py 订单状态回报为准。
- Web 模拟交易页展示只读“交易计划恢复矩阵”，汇总最近非终态计划的状态、执行模式、活跃提交态、疑似卡住、可撤单、可重试、冷却中和重试超限数量，并列出需要关注的计划。矩阵不自动修改订单，只用于解释哪些计划还在等待 vn.py 回报、哪些可人工重试或撤单。
- Web 模拟交易页可在确认后手动运行一次“交易计划恢复扫描”，该操作复用后台 `vnpy_paper_auto_retry` 的同一条执行路径：活跃 `submitted` / `part_filled` / `cancel_requested` 计划提交满 60 秒后即可查询注入的 `MainEngine.get_order` / `get_all_trades`，补同步漏失的订单和成交回报；查询到网关证据或查询异常时保护原计划。未满 30 分钟且没有网关证据的计划继续等待，只有超过 30 分钟且没有可用证据时才安全归档，再按 retry 次数、冷却和可恢复原因扫描其他到期计划。`vnpy_order_cancelled`、`vnpy_order_rejected`、`vnpy_order_failed` 以及 `vnpy_order_timeout`、`vnpy_partial_fill_timeout`、`vnpy_cancel_timeout` 均不会自动重下单，避免用户撤单、网关拒单或状态不明时重复委托。响应会返回 `reconciled_count`、`protected_count` 和 `reconciliation_failed_count`，Web 成功提示同步展示这些计数。
- 页面提供“重置账户”入口，后端会归档当前 `vnpy_paper` 模拟账户、创建新的干净模拟账户并按初始资金写入现金流入；旧账户和旧流水保留在 Portfolio 账本中用于审计，不做硬删除。
- Web 模拟交易页会展示“模拟账户历史”，显示当前 `vnpy_paper` 账户和已归档账户，并支持按全部、当前、已归档、活跃非当前筛选；非当前账户可在确认后恢复/切换为当前账户，后端只允许切换 `broker=vnpy_paper` 的本地 paper 账户，并会归档原当前 paper 账户、清空 `auto_trailing_peaks`。页面也提供“清理已归档”，该操作只把旧归档账户从 vn.py paper 历史视图隐藏，不删除 Portfolio 账户、成交或资金流水。
- 页面提供“立即 dry-run”演练按钮，会以临时 `dry_run` 覆盖运行一次自动选股，不要求先保存或开启 `auto_trade_enabled`，也不会改变已保存的执行模式和自动交易开关。
- 自动模拟成交默认启用交易时段限制；市场非交易日、盘前、午休、盘后或日历未知时不会提交 paper 订单，并记录 `non_trading_day`、`outside_trading_session` 或 `market_phase_unknown`。`dry_run` 和 `manual_approval` 仍允许在任意时间生成计划。
- 状态接口会返回 `diagnostics.trading_window`；Web 模拟交易页展示“可交易窗口”，可区分当前开市、今日稍后开市、下一交易日、交易日历不可用、时间窗关闭或当前执行模式不强制拦截，并显示对应的下次开盘/收盘时间。
- 状态接口新增 `diagnostics.auto_trade_readiness`，结构化返回自动交易 readiness：总状态、下一步建议、阻断原因、关注项和本地账本、自动交易开关、runtime scheduler、自动任务、AlphaSift 选股依赖、调度窗口、交易窗口、连续失败熔断、vn.py bridge 等组件状态。`diagnostics.auto_trade_readiness.timing_alignment` 会把自动任务下次触发时间与交易窗口开收盘时间对齐展示；`diagnostics.alphasift` 会轻量返回自动交易依赖的 AlphaSift 启用状态、可用性、版本和策略数量；异常只写入诊断，不会拖垮本地 paper 状态接口。
- 状态接口新增 `diagnostics.system_health`，把本地账本、选股来源、自动化调度、调度窗口、交易窗口、持仓估值、行业归属和 vn.py bridge 合并为跨模块健康视图。`required_blockers` 表示会阻断自动执行的必需组件，`warnings` 表示需要关注但不一定阻断的降级，`disabled` 表示因配置或轻量查询暂未启用的组件。
- 完整状态的 `diagnostics.industry_exposure` 返回行业归属解析状态、持仓总数、已解析/缺失数量、覆盖率、缺失代码和按行业汇总的持仓市值。未配置行业风控时使用 `snapshot_only` 模式，只统计快照已有字段且不发起逐股网络请求；配置行业金额/比例上限或目标行业权重后切换到 `risk_guard` 模式，该组件属于必需风控条件，覆盖不完整会 fail closed。轻量状态只返回 `snapshot_not_requested`，不会拉取持仓或行业数据。
- 配置 `auto_max_drawdown_pct` 后，账户回撤按账户 ID 持久化的已观测权益峰值计算，峰值至少为初始资金。`diagnostics.account_drawdown` 和 Agent run 的 `diagnostics.account_risk` 返回 `basis=observed_equity_peak`、当前权益、峰值权益、回撤比例和阈值；恢复旧账户会继续使用该账户的峰值，新建/重置账户使用新账户 ID 独立计算。达到阈值时系统健康的“账户回撤”组件阻断新买入，卖出风险处置不受影响。当前不会自动解除回撤门禁。
- Web 模拟交易页“可用性诊断”摘要会优先使用 `diagnostics.system_health.components`，旧后端无该字段时回退到 `diagnostics.auto_trade_readiness`，再无 readiness 时才基于现有状态字段本地推导，帮助快速判断为何不可用或为何本轮不会自动提交。
- Web 模拟交易页的首屏状态加载失败时会区分常见排障原因：`/api/v1/vnpy-paper/status` 返回 404 时提示后端可能仍是旧进程或未加载 vn.py paper 路由；请求超时时提示优先检查轻量状态接口和行情/估值数据源；本地连接失败时提示检查 Web/API 服务和 `API_BASE_URL`。
- Web 模拟交易页会根据状态诊断、Portfolio 快照 `limitations` 和持仓级 `price_available` / `price_stale` 展示“持仓估值降级”提示；当前持仓表同步显示价格源，缺价时标记为“缺价”，避免市值或浮盈显示为 0 时缺少解释。
- 状态接口的完整持仓快照使用进程内 10 秒短 TTL 缓存，减少页面刷新时重复重放 Portfolio 和拉取行情估值；本地成交、vn.py 成交回调、账户重置或账户恢复后会主动失效缓存。`diagnostics.snapshot_cache_hit` 和 `diagnostics.snapshot_cache_ttl_seconds` 可用于判断本次状态是否来自缓存。
- 自动模拟交易支持最大持仓数风控；当前持仓数量达到上限时，新候选会以 `max_positions_reached` 写入跳过记录，不会提交模拟成交。
- 自动模拟交易支持基础仓位暴露风控；可设置单票最大持仓金额、组合最大持仓金额、组合最大仓位比例、行业最大持仓金额和行业最大仓位比例，触发时分别以 `single_position_value_limit_reached`、`total_position_value_limit_reached`、`total_position_pct_limit_reached`、`industry_position_value_limit_reached`、`industry_position_pct_limit_reached` 写入跳过审计。`dry_run` 和 `manual_approval` 的计划单也会占用同一轮风控预算，避免一次计划超出上限。配置行业上限后，若候选或已有持仓行业无法解析，会以 `industry_exposure_unavailable` 保守跳过。
- “每票金额”、每日预算、最低现金和所有金额型仓位上限统一按模拟账户 `base_currency` 计价。港股、美股、日股、韩股和台股下单前会把基准币种预算换算为 `HKD`、`USD`、`JPY`、`KRW` 或 `TWD`；Agent run 的 `diagnostics.currency_budget` 以及订单 `raw.fx_conversion` 会记录基准金额、交易币种金额、汇率和来源。缺少当前汇率、汇率被标记 stale 或汇率日期超过 7 个自然日时，新增买入以 `fx_rate_unavailable` 保守跳过；卖出减仓不因汇率缺失被阻断。已有外币成交的每日预算和仓位暴露也按账户基准币种汇总。
- 自动模拟交易支持每日买入上限和每日预算风控；达到次数上限时记录 `daily_order_limit_reached`，超过预算时记录 `daily_budget_exceeded`，均不会提交模拟成交。
- 自动模拟交易支持股票黑名单风控；页面可填写逗号分隔代码，命中的候选会以 `symbol_blacklisted` 写入候选决策和交易计划，不会提交模拟成交。
- 自动模拟交易支持候选级基础风控；可过滤 ST/退市风险、停牌、涨跌停和成交额过低的候选，分别以 `st_or_delisting_risk`、`suspended_stock`、`price_limit_reached`、`liquidity_below_threshold` 写入跳过审计。缺少对应字段时不会凭空拦截；AlphaSift/DSA 已补出的 `amount`、`turnover_amount`、`limit_status`、`is_suspended` 等字段会被用于判断。
- 自动模拟交易支持账户级基础风控；可设置最低现金余额和最大回撤百分比，触发时分别以 `cash_low_watermark`、`account_drawdown_limit_reached` 跳过买入候选，并写入告警中心系统事件、复用 alert 路由外发通知。最大回撤暂以模拟账户 `initial_cash` 为基准计算，后续仍需补更完整的权益曲线和连续亏损熔断。
- 自动模拟交易支持可选的大盘红绿灯风控；开启后读取最近一次大盘复盘持久化的 `MarketLightSnapshot`，默认只在红灯时以 `market_light_red` 跳过买入，也可配置红灯和黄灯都跳过（`market_light_yellow`）。没有最近快照或读取失败时暂不阻断自动买入。
- 自动模拟交易支持可选连续失败熔断；开启后按同策略、同市场、同触发源统计最近失败运行，连续达到阈值时以 `failure_fuse_open` 跳过本轮自动买入。熔断只统计 `failed`、数据质量 `stale/unavailable`、非良性 error 以及后续熔断跳过记录，不把自动交易关闭、非交易日、交易时段外等正常跳过计为失败。熔断一旦打开会保持锁存，只有 Web“恢复熔断”或对应 API 显式重置统计基线才重新放行；状态接口通过 `diagnostics.failure_fuse` 返回是否打开、阈值、连续失败数和最近 run 摘要，重置不删除历史 Agent run。
- 自动交易的关键异常会写入告警中心触发历史并复用 alert 路由主动外发通知：连续失败熔断打开记录 `failure_fuse_open`，AlphaSift 筛选异常记录 `alphasift_screen_failed`，数据质量阻断记录 `data_quality_stale` / `data_quality_unavailable`，账户级风控阻断记录 `cash_low_watermark` / `account_drawdown_limit_reached`，后台自动重试失败/未成交记录 `auto_retry_*`，`vnpy_paper` 提交态订单超时记录 `vnpy_order_timeout`，部分成交等待成交回报超时记录 `vnpy_partial_fill_timeout`，撤单请求超时记录 `vnpy_cancel_timeout`；这些事件使用 `rule_id=null`、`target=vnpy_paper`、`data_source=vnpy_paper_auto`，可通过 `/api/v1/alerts/triggers?target=vnpy_paper` 查询。通知结果会写入 `alert_notifications`，未配置 alert 渠道时会记录 `__no_channel__` 方便排障。
- Web 模拟交易页展示最近 `target=vnpy_paper` 的“自动交易告警历史”，可直接看到数据质量阻断、熔断、自动重试失败和 vn.py 订单超时等系统事件；完整诊断仍以告警中心/API 记录为准。
- 自动模拟交易会根据 AlphaSift 返回的 `quality_status`、`warnings`、`source_errors`、`fallback_used` 和 `stale` 生成基础数据质量诊断；仅 `ok` 或可接受的 `partial` 会继续执行，`stale` 或 `unavailable` 会以 `data_quality_stale` / `data_quality_unavailable` 写入跳过审计，不提交模拟成交。
- 自动交易支持基础卖出风控，默认关闭；开启后会按止损百分比、止盈百分比、移动止损百分比、最大持仓天数、超时未走强或可选策略失效信号触发卖出，并写入候选决策和交易计划审计。默认整仓卖出，也可通过 `auto_sell_position_pct` 设置每次卖出的持仓比例，实现按比例分批退出；设置 `auto_no_progress_days` 后，持仓天数达到阈值且浮盈不高于 `auto_no_progress_min_return_pct`（留空按 0%）时会以 `no_progress_timeout` 触发卖出；开启 `auto_signal_exit_enabled` 后，当前持仓命中 active `DecisionSignal` 的 `sell/reduce/avoid` 信号时会以 `strategy_invalidated` 触发卖出。开启 `auto_rebalance_enabled` 后，自动卖出会复用单票、总仓位和行业仓位暴露上限，超限时生成 `rebalance_single_position_value_exceeded`、`rebalance_total_position_value_exceeded`、`rebalance_total_position_pct_exceeded`、`rebalance_industry_position_value_exceeded` 或 `rebalance_industry_position_pct_exceeded` 卖出计划；也可通过 `auto_target_position_weights` 和 `auto_target_industry_weights` 配置股票或行业目标权益占比，超配时生成 `rebalance_target_position_weight_exceeded` 或 `rebalance_target_industry_weight_exceeded` 卖出计划。买入侧会把单笔预算缩减到股票和行业目标的最小剩余缺口，已有持仓可继续补足显式股票目标；换算为交易币种后再按市场整手取可执行金额，不足一手时以 `target_weight_below_min_lot` 跳过。候选原始载荷会记录 `target_weight_sizing`、`rebalance_plan`、目标权重、当前值、剩余缺口和可执行金额。`paper` 模式会直接写入本地模拟卖出；`vnpy_paper` 模式会把卖出委托提交给注入的 vn.py `MainEngine` 并记录为 `submitted`，只有后续成交回报同步后才会写入本地 Portfolio。移动止损会在 `data/vnpy_paper_trading.json` 记录每只持仓的本地峰值价，触发原因是 `trailing_stop_triggered`；未成交的 vn.py 提交态卖出不会提前清理峰值记录。当前不做分批止盈策略；基础跨币种预算与仓位估值已接入 Portfolio 汇率表，任意权重和多约束组合优化仍未完成。
- 开启 `auto_score_weighted_allocation_enabled` 后，系统把 `auto_allocation_budget` 视为每轮组合预算；留空时使用 `auto_cash_per_order`。`auto_allocation_method=score_weighted` 按候选评分分配；`score_inverse_volatility_20d` 使用 AlphaSift 同一决策时点的 `volatility_20d_pct`，按 `score / max(volatility, auto_risk_volatility_floor_pct)` 生成风险调整权重。`score_inverse_volatility_20d_correlation_capped` 在此基础上读取运行日及之前的本地 `stock_daily` trailing returns，按候选顺序保留排名更高者，并排除与已保留候选相关系数高于 `auto_max_pairwise_correlation` 的标的。波动率字段缺失、历史收益或两两重叠样本不足时逐票 fail-closed，不静默退化。每票仍以 `auto_cash_per_order` 为上限，并同时受现金保留、每日预算/订单槽位、最大持仓数、单票/总仓位/行业空间和显式目标权重约束；候选触顶后，剩余预算会重新分配给仍有空间的候选。Agent run 和候选仓位计划会审计风险模型、波动率、相关性样本/阈值/冲突标的、约束、分配金额、排除原因和整手残差。该能力默认关闭，是确定性的顺序相关性约束，不是协方差目标函数优化器。
- 每轮 AlphaSift 结果都会生成 `diagnostics.data_quality.score`（0~100）和等级，固定按筛选运行完整性 45%、候选字段/来源覆盖 35%、snapshot/daily 来源健康 20% 汇总；该分数是可解释规则分，不是成功概率或 LLM 置信度。没有来源健康快照时会标记 `unobserved` 并按 70 分计，不当作健康满分。`auto_min_data_quality_score` 默认留空，仅记录分数；配置为 0~100 后，低于阈值的候选轮次会以 `data_quality_score_below_threshold` 阻断所有新买入、记录候选跳过决策并写入自动交易告警。原有 `stale/unavailable` 状态门禁继续始终生效。

## 配置与数据

- 页面交易设置持久化到 `data/vnpy_paper_trading.json`；可选 vn.py runtime 托管通过 `.env` 中的 `VNPY_RUNTIME_*` 配置显式开启，默认关闭。
- 成交、资金流水、持仓快照复用 Portfolio 表；因此模拟成交也会出现在现有持仓页和持仓风险计算中。
- 重置账户会更新 `settings.account_id` 指向新模拟账户，并清空 `auto_trailing_peaks`，避免新账户继承旧持仓的移动止损峰值；不会删除旧 Agent run、候选决策或交易计划审计。
- 可执行 `python scripts/check_vnpy_adapter.py` 验证当前 Python 环境的 vn.py adapter 状态；未安装 vn.py 时脚本输出 `local_paper_fallback` 诊断并以 0 退出，安装 vn.py 后会尝试实例化真实 `OrderRequest`。桥接提交可以由宿主进程注入 `MainEngine`，也可以显式设置 `VNPY_RUNTIME_ENABLED=true` 让 DSA 在启动时创建 EventEngine/MainEngine，并按需 add gateway/connect。
- 每次自动模拟交易会写入 `stock_selection_agent_runs`、`stock_selection_agent_decisions` 和 `stock_selection_agent_trade_plans`，保存策略、候选/持仓、买入或卖出动作、评分、跳过原因、计划金额、执行模式、数据质量诊断、结构化 `agent_plan`、结构化 `agent_summary`、候选级 `position_plan`、规则 `risk_review`、规则 Agent 买入前 `agent_review`、可选 LLM 买入前 `llm_review`、成交 `trade_id` 和候选原始载荷，供页面审计。`agent_plan` 会记录规则派生的 `plan_profile`、`execution_policy`、`sizing_plan`、`adaptive_controls` 和可选 `llm_dynamic_plan`，用于说明本轮计划档位、执行路由、预算上限、已启用风控层、LLM 是否调参和降级动作。`agent_summary.review_quality` 会基于规则 Agent 复核、可选 LLM 复核和数据质量生成 `audited`、`guarded`、`needs_review` 或 `idle` 状态、质量分、覆盖率和风险标记，便于判断是否需要人工确认。手动触发 LLM 复盘后，结果会写入 run 诊断中的 `llm_recap`，失败只记录错误原因，不影响交易计划和成交状态。
- vn.py 事件同步已提供外部回写 API：订单状态回报会按 `vt_orderid` 更新提交态/失败态审计；成交回报按 `vt_tradeid` 幂等写入 Portfolio，支持一张委托分多笔累计成交，只有累计数量达到计划数量才标记 `filled`，期间保留 `part_filled`、累计数量、加权均价、剩余数量和成交明细。账户和持仓快照会写入 `diagnostics.vnpy_sync_state` 供状态页和后续风控诊断读取；未匹配到计划、重复回报或本地账本校验失败会以明确 reason code 返回，不会静默写入。
- Agent run 详情会从运行状态、结构化 `agent_plan`、结构化 `agent_summary`、结构化 `agent_workflow`、数据质量、候选决策和交易计划合成 `timeline` 时间线；候选决策和交易计划的 `order_result` 会保存候选级 `position_plan`、`risk_review` 与 `agent_review`；Web 页面直接展示“Agent 计划”“运行总结”“Agent 工作流”“运行时间线”和候选级“Agent 复核”，用于解释本轮任务为何按该策略、市场、预算和执行模式运行，以及最终为何成交、计划、警告或跳过。
- 自动交易写入的同日同策略同股票订单带有 `dedup_hash`，避免定时任务重复买入同一个候选；`vnpy_paper` 模式还会检查同股票同方向是否已有 `submitted` / `part_filled` / `cancel_requested` 活跃计划，命中时以 `active_vnpy_order_exists` 跳过，避免在真实成交回报到达前重复提交买入或卖出委托。
- 定时任务注册在 API/Web/Desktop 进程内的 `RuntimeSchedulerService` 后台任务中；即使每日分析定时任务未开启，只要自动模拟交易开启，后台 scheduler loop 也会运行该任务。
- `python main.py --serve-only` / `--webui-only` 只禁用每日分析 daily job，不会禁用自动模拟交易等 runtime scheduler 后台任务；页面保存“定时自动买入”后会触发 reconcile。
- 状态接口会返回 `scheduler` 状态，包含后台任务名、是否运行、任务级 `next_run_at`、最近错误、最近跳过原因和最近 `task_events`；Web 页面用这些字段展示自动交易后台任务是否已注册、下次触发时间以及“后台任务日志”。
- Web 模拟交易页新增“任务健康检查”，基于 `GET /api/v1/vnpy-paper/task-health` 汇总 `vnpy_paper_auto_trade` 和 `vnpy_paper_auto_retry` 是否注册、是否运行、是否被配置停用、最近事件是否失败/跳过以及下次运行时间；任务健康摘要会优先使用持久化最近事件，便于 API 进程重启后继续判断自动选股和恢复扫描为什么未执行。
- Web 模拟交易页的“后台任务日志”会读取 `GET /api/v1/vnpy-paper/task-events`，支持按任务名和 started/completed/skipped/failed 状态筛选数据库持久化的最近任务事件；API 进程重启后仍可用于定位自动买入、自动恢复扫描或事件监控到底在哪一步被跳过或失败。
- Web 模拟交易页的“任务趋势”会读取 `GET /api/v1/vnpy-paper/task-event-summary`，基于最近持久化任务事件展示 completed/skipped/failed/started 分布、任务级失败率、平均耗时和最近失败/跳过时间，用于判断后台自动化是否持续健康。
- Web 模拟交易页的“长期稳定性”会读取 `GET /api/v1/vnpy-paper/task-metrics`，可切换 7/30/90 天窗口，展示终态运行数、成功/失败/跳过率、平均与 P95 耗时、当前连续失败、任务级明细和逐日趋势。成功率分母只包含 `completed`、`skipped`、`failed` 终态事件，`started` 仅单独计数，避免一次运行被重复计算。
- Agent 控制台会读取 `GET /api/v1/vnpy-paper/agent-runs/data-quality-trends`，按 7/30/90 天窗口展示跨 run 的 `ok`、`partial`、`stale`、`unavailable`、`unknown` 分布、降级率、警告、source error、逐日趋势和 `snapshot/daily + source` 来源健康观测。来源级结果返回观测数、降级次数/比例、最新状态和最新/最大失败计数；旧 run 没有整体质量或来源快照时分别归入 `unknown` 或“无快照”，不会被误算成健康。
- 每轮自动选股会在 `diagnostics.agent_plan.recent_run_context` 保存同触发源、策略和市场最近 5 次 run 的确定性摘要，包括状态/数据质量分布、候选/计划/成交/跳过总数、历史成交率、连续失败和逐 run 摘要。该上下文不依赖 LLM；开启 `auto_llm_plan_enabled` 后，`vnpy_paper_dynamic_agent_plan_v2` 提示词复用同一份上下文，Agent 控制台计划卡展示最近运行数、成交率和最近状态。
- Agent 控制台可调用 `POST /api/v1/vnpy-paper/agent-runs/backtest`，按当前策略、市场和运行时间筛选已持久化买入候选，返回 1/5/10/20 个交易日的覆盖率、胜率、平均/中位收益和平均最大有利/不利波动。评价锚点使用候选价格与决策日期，只读取严格晚于决策日期的 `stock_daily`；默认 `refresh_missing=false`，不会联网补数、重跑历史策略、修改 Agent run 或触发交易。缺价和缺少未来日线会进入 `unable_reason_counts` 并降低覆盖率。该结果不包含手续费、滑点、基准超额收益或 point-in-time 全市场策略重放。
- 每轮自动选股还会把同策略、同市场的持久化买入候选汇总为 `diagnostics.cross_run_quality`。状态使用配置的前瞻交易日、成熟样本阈值、最低胜率和有界扫描数，记录覆盖率、胜率、平均/中位收益、平均最大不利波动、前一状态和状态迁移。`auto_cross_run_quality_gate_enabled` 默认关闭；开启后，成熟样本胜率不足或本地评价异常会以 `cross_run_quality_gate_blocked` 阻断新增买入并告警，`insufficient_evidence` 不阻断，自动止损/止盈等卖出检查仍先执行。该闭环只使用本地持久化决策与严格晚于决策日的 `stock_daily`，不联网补数、不让 LLM决定状态，也不等同完整历史全市场回测。
- 持久化后台任务事件默认保留 30 天，runtime scheduler 写入新事件后会按 `DSA_RUNTIME_SCHEDULER_TASK_EVENT_RETENTION_DAYS` 和 `DSA_RUNTIME_SCHEDULER_TASK_EVENT_CLEANUP_INTERVAL_SECONDS` 做低频清理；该清理只删除 `runtime_scheduler_task_events` 中的任务日志，不删除 Agent run、交易计划、Portfolio 成交或资金流水。保留天数设为 `0` 时不自动清理，适合由外部归档策略接管的部署。
- 绩效接口会基于本地 paper 账本返回 `daily_returns` 和 `monthly_returns`，按每日/每月最后权益聚合盈亏、收益率、累计收益率、回撤和交易数；Web 模拟交易页展示最近日度收益表和月度收益表。该视图用于 paper 运行复盘，不等同接入完整历史行情后的回测级净值曲线。
- 后台恢复任务会先主动对账提交满 60 秒的 `vnpy_paper` 活跃计划；若 `submitted` 计划超过 30 分钟仍无订单终态、成交回报或 MainEngine 证据，会标记为 `failed` / `vnpy_order_timeout` 并保留原 `vt_orderid` 和 timeout 审计。`cancel_requested` 和 `part_filled` 分别归档为 `vnpy_cancel_timeout`、`vnpy_partial_fill_timeout`，且所有状态不明超时结果都禁止自动重提。撤单请求期间发生的成交以及超时归档后的迟到成交仍可按原 `vt_orderid` 回写并把计划恢复为实际成交状态。
- 状态接口会返回 `diagnostics.vnpy_adapter`、`diagnostics.vnpy_bridge`、`diagnostics.vnpy_event_bridge`、`diagnostics.vnpy_runtime` 和 `diagnostics.trading_window`，用于区分当前是 `local_paper_fallback`、已具备 `vnpy_order_request` adapter 能力、已经具备 `MainEngine` 提交通道、已由 DSA 启动可选 vn.py runtime，还是自动交易正在等待下一个交易窗口；未安装 vn.py 或未配置 `MainEngine` 时仍保持本地 paper 可用。
- Web “自动选股 Agent 记录”区块支持导出当前选中 run 的 JSON 明细，包含 Agent 计划、运行总结、候选决策、交易计划、诊断和成交关联信息，便于复盘或排障。
- Web 新增“Agent 控制台”入口 `/agent-console`，独立展示最近自动选股 run 历史、分页、策略/市场/状态/时间范围筛选、运行详情、时间线、候选决策、交易计划和 JSON 导出；该页面支持 `/agent-console/<run_uid>` 独立详情路由，并兼容 `/agent-console?runUid=<run_uid>` 查询参数深链，复用 `/api/v1/vnpy-paper/agent-runs*` 审计接口，不直接提交订单。
- Web Agent 控制台的候选决策表会展示 `order_result.agent_review` 的复核状态和摘要；开启 `auto_llm_review_enabled` 后，也会展示 `order_result.llm_review` 的 LLM 买入复核状态和摘要。今日 Agent 总结会聚合 `agent_review_counts`、`llm_review_counts`、`review_quality_counts`、`review_quality_flag_counts` 和 `review_quality_score_avg`，用于快速识别当天候选是通过、警告、被阻断、LLM 调用失败，还是需要人工复核。
- 自动模拟交易设置新增默认关闭的 `auto_llm_plan_enabled`。开启后，系统会在调用 AlphaSift 前生成本轮 `agent_plan.llm_dynamic_plan`，允许 LLM 在 AlphaSift 策略白名单内选择策略，并只在已保存配置上限内收紧 `max_results`、`cash_per_order` 和 `min_score`；结果会记录 `prompt_version` 和 `evaluator_version`，失败时使用保存配置继续本轮运行并保留失败原因。
- Web Agent 控制台的运行详情支持手动触发 LLM 复盘，并展示 `diagnostics.llm_recap` 的内容或失败原因；该入口只生成审计复盘，不提交订单，也不会改变既有风控结论。
- 自动模拟交易设置新增默认关闭的 `auto_llm_review_enabled`。开启后，候选必须先通过规则风控，再调用 LLM 生成 `order_result.llm_review`，并记录 `prompt_version` 与 `evaluator_version`；`blocked`、模型不可用、JSON 解析失败或调用异常都会 fail-closed 跳过该候选，不会继续生成或提交买入计划。
- Web 页面展示“绩效摘要”、“权益曲线”、日度收益表、月度收益表和只读“绩效矩阵”，后端通过当前 Portfolio paper 快照、成交流水和最近 Agent run 审计聚合累计收益、收益率、成交额、买卖次数、卖出胜率、最大回撤、换手率、当前仓位、运行次数、成交/计划数、主要跳过原因、常见成交股票以及按策略/行业归因；矩阵会展开策略与行业的样本数、计划数、成交数、跳过数、成交金额和填充率，并可在页面按 Agent run 创建时间窗口筛选。卖出胜率、回撤、换手率、权益曲线、日/月收益和绩效矩阵按本地 paper 成交流水与当前 `run_limit` / `created_from` / `created_to` 窗口估算，用于本地模拟复盘，不等同真实券商绩效归因或完整历史行情回测。
- Web “自动选股 Agent 记录”区块支持导出最近运行记录 JSON；批量导出会包含每次 run 的候选决策、交易计划和时间线，便于离线复盘或提交排障证据。

## API

所有接口前缀为 `/api/v1/vnpy-paper`：

- `GET /status`：读取模拟交易状态、设置、账户快照、近期成交和最近一次自动运行摘要。
- `GET /status` 响应包含 `scheduler`、`diagnostics.system_health`、`diagnostics.auto_trade_readiness`、`diagnostics.vnpy_adapter`、`diagnostics.vnpy_bridge`、`diagnostics.vnpy_event_bridge`、`diagnostics.vnpy_runtime`、`diagnostics.trading_window`、`diagnostics.snapshot_cache_hit` 和 `diagnostics.failure_fuse`，用于展示 runtime scheduler 是否启动、`vnpy_paper_auto_trade` / `vnpy_paper_auto_retry` 是否注册、顶层/任务级 `next_run_at`、任务级 `initial_delay_seconds`、`last_error`、`last_skip_reason`、最近 `scheduler.task_events`、跨模块健康视图、自动交易 readiness、持仓快照是否命中短缓存、连续失败熔断状态，以及 vn.py `OrderRequest` adapter、`MainEngine` 桥接、EventEngine 回调、可选 runtime bootstrap 和自动交易可交易窗口是否可用。`scheduler.loop_running` 表示调度循环是否存活，`scheduler.running` 仍表示当前是否正在执行分析任务；readiness 使用 `loop_running` 判断自动交易定时任务是否可继续调度，并用 `timing_alignment` 判断下一次自动买入是否落在交易窗口内。
- `GET /task-health`：读取后台任务健康摘要，返回整体健康状态、调度器启用/运行状态、自动交易开关、健康/关注/异常/停用计数，以及每个后台任务的注册状态、运行状态、间隔、下次运行时间、持久化最近事件和 reason code。
- `GET /task-events?limit=50&name=vnpy_paper_auto_retry&status=failed`：读取数据库持久化的最近后台任务事件，`limit` 范围 1~100，可选 `name` 和 `status` 过滤，用于 Web “后台任务日志”筛选和跨进程重启排障。
- `GET /task-event-summary?limit=100`：聚合最近后台任务事件，返回全局状态计数和每个任务的总数、失败率、平均耗时、最近事件、最近失败和最近跳过时间。
- `GET /task-metrics?days=30`：按 1 至 90 天窗口读取最多 5000 条持久化任务事件，返回终态运行成功/失败/跳过率、平均与 P95 耗时、当前连续失败、任务级指标和逐日趋势；响应中的 `truncated=true` 表示当前窗口超过读取上限，页面会提示统计结果已截断。
- `GET /status?include_snapshot=false&include_recent_trades=false`：读取轻量状态，只返回设置、账户、可用性和诊断信息，不拉取持仓估值或近期成交；Web 页面首屏使用该路径避免被行情估值拖慢。
- `POST /account/ensure`：创建或复用 `vnpy_paper` 模拟账户，并按初始资金写入一笔现金流入。
- `POST /account/reset`：归档当前 `vnpy_paper` 模拟账户，创建新的干净模拟账户并返回最新状态；响应 `diagnostics.account_reset` 包含旧账户和新账户 ID。
- `GET /accounts?include_inactive=true`：读取本地 `vnpy_paper` 模拟账户历史，返回当前账户、已归档账户、`is_current`、`archived`、`cleanup_hidden` 和基础账本元数据；默认不返回已清理隐藏的归档账户，传 `include_hidden=true` 可用于审计查看。
- `POST /accounts/{account_id}/restore`：将指定本地 `vnpy_paper` 账户恢复/切换为当前模拟交易账户，并归档其他活跃 `vnpy_paper` 账户；响应 `diagnostics.account_restore` 包含恢复账户、原当前账户和被归档账户 ID。该接口拒绝非 `vnpy_paper` 账户，且不会新增现金流水或删除历史成交。
- `POST /accounts/archived/cleanup`：批量清理已归档 `vnpy_paper` 账户；该接口只把符合条件的非当前、非活跃归档账户 ID 写入 `hidden_archived_account_ids`，让默认账户历史不再显示它们，不会删除 Portfolio 账本。恢复某个隐藏账户时会自动取消该账户的隐藏标记。
- `PUT /settings`：保存页面设置，并触发 runtime scheduler 重新 reconcile 后台任务。可设置 `auto_execution_mode="vnpy_paper"` 和 `vnpy_gateway_name`，用于让自动买入委托走 vn.py `MainEngine` 桥接；也可设置默认关闭的 `auto_llm_plan_enabled` 和 `auto_llm_review_enabled`，分别让自动买入在选股前追加 LLM 动态计划、在规则风控通过后追加 LLM 买入复核。
- `POST /failure-fuse/reset`：重置连续失败熔断统计基线，返回轻量状态并更新 `diagnostics.failure_fuse.reset_at`；历史 Agent run、候选决策和交易计划仍保留。
- `POST /orders`：提交一笔模拟委托。默认 `execution_route="local_paper"` 时写入 Portfolio 交易流水；传 `execution_route="vnpy_bridge"` 时会尝试通过注入的 vn.py `MainEngine.send_order` 提交委托，成功后返回 `status="submitted"`、`source="vnpy_main_engine"` 和 `raw.vt_orderid`，不会立即写入本地成交。
- `POST /vnpy-events/trades`：同步一笔 vn.py 成交回报。请求至少包含 `vt_orderid`、`quantity` 和 `price`；后端按 `vt_orderid` 找到提交态交易计划后写入本地 Portfolio 交易流水，并把计划/决策状态回写为 `filled`。
- `POST /vnpy-events/orders`：同步一笔 vn.py 订单状态回报。`rejected`、`cancelled`、`inactive` 等状态会把对应交易计划回写为 `failed`；`parttraded` / `partial_filled` 会回写为 `part_filled` 并记录已成交数量/价格；`submitted`、`alltraded` 等状态只更新提交态审计，不会写入本地 Portfolio 成交。只有 `/vnpy-events/trades` 成交回报会写入本地 Portfolio。
- `POST /vnpy-events/account`：同步 vn.py 账户资金快照到 `diagnostics.vnpy_sync_state.account`，用于区分本地 paper 账本与外部 vn.py 账户状态。
- `POST /vnpy-events/positions`：同步 vn.py 持仓快照到 `diagnostics.vnpy_sync_state.positions`，当前作为诊断快照保存，不直接改写本地 Portfolio 持仓。
- `POST /vnpy-events/attach`：当应用进程已注入 `app.state.vnpy_event_engine` 或 `app.state.vnpy_main_engine.event_engine` 时，注册订单、成交、账户和持仓事件 handler，把 vn.py EventEngine 事件自动转入上述同步入口；重复 attach 会先注销旧 bridge。
- `POST /auto/run`：立即运行一次 AlphaSift 选股驱动的自动模拟买入，响应中包含 `agent_run_uid`。
- `POST /auto/run` 可传可选 JSON 请求体，例如 `{"execution_mode":"dry_run","ignore_auto_trade_enabled":true}`，用于一次性演练自动选股和交易计划生成；也可传 `{"execution_mode":"vnpy_paper"}` 临时把本轮买入委托交给 vn.py bridge。该请求只影响本次运行，审计 diagnostics 会记录临时覆盖参数。
- `GET /performance?run_limit=50&created_from=2026-07-01T09:00:00&created_to=2026-07-02T15:00:00`：读取本地 paper 绩效摘要，聚合当前模拟账户权益、相对初始资金收益、成交额、买卖次数、FIFO 卖出胜率、权益路径、日度收益、月度收益、最大回撤、换手率、当前仓位、最近 Agent run 状态、成交计划状态、跳过原因、成交股票分布和按策略/行业归因；`created_from` / `created_to` 可选，用于按 Agent run 创建时间筛选窗口级归因。
- `POST /trade-plans/{plan_uid}/approve`：审批一笔 `manual_approval` 模式生成的待执行交易计划，成功后写入本地 paper 成交并回写计划、候选决策和运行统计。
- `POST /trade-plans/{plan_uid}/retry`：重试一笔 `failed` 或可恢复 `skipped` 的 `manual_approval` / `paper` / `vnpy_paper` 交易计划；响应和计划审计中的 `raw.retry` / `order_result.retry` 会记录尝试次数、最大次数、上次原因和下一次可重试时间。
- `POST /trade-plans/{plan_uid}/cancel`：对 `vnpy_paper` 的 `submitted` / `part_filled` 交易计划发起撤单请求；成功后返回 `status="cancel_requested"` 并保留 `raw.cancel.cancel_request_payload`，随后仍需通过 `/vnpy-events/orders` 同步 `cancelled` / `rejected` / `filled` 等终态。
- `GET /trade-plans/recovery-summary?limit=100&include_terminal=false`：读取只读交易计划恢复矩阵；默认只扫描非终态计划，返回状态/执行模式/恢复状态计数、疑似卡住计划数、可撤单数、可重试数和需要关注的计划列表。
- `POST /trade-plans/recovery/run?max_plans=5&scan_limit=200`：手动运行一次受限交易计划恢复扫描；复用后台到期重试逻辑，返回超时归档数、扫描数、尝试数、提交数、跳过数、失败数和消息列表。
- `GET /agent-runs`：读取最近的自动选股 Agent 运行摘要；支持 `limit`、`offset`、`trigger_source`、`strategy`、`market`、`status`、`created_from` 和 `created_to` 过滤；响应包含过滤后的 `total`。
- `GET /agent-runs/daily-summary?date=2026-07-06`：聚合某一天的自动选股 Agent 运行摘要，返回 run 数、候选/计划/成交/跳过计数、状态分布、执行模式分布、数据质量分布、Agent 复核状态、LLM 复核状态、复核质量状态/风险标记/平均分、工作流状态/阶段分布、交易计划状态、主要跳过原因和热门标的；当复核质量出现 `guarded` 或 `needs_review` 时，摘要 `health` 会进入 `warning`；支持 `trigger_source`、`strategy`、`market`、`status` 过滤。
- `GET /agent-runs/data-quality-trends?days=30`：按 1 至 90 天窗口汇总跨 run 的整体筛选数据质量、降级率、警告/source error、逐日趋势和来源级 `source_health_items`；支持 `trigger_source`、`strategy`、`market`、`status` 过滤，最多扫描 5000 条，`truncated=true` 表示结果已截断。
- `GET /agent-runs/cross-run-quality`：按当前保存的策略、市场和前瞻门禁参数，只读计算当前跨运行前瞻状态、成熟样本、覆盖率、胜率、收益、状态迁移与是否会阻断下一轮新增买入；不创建 Agent run、不联网补数、不提交订单。
- `GET /agent-runs/export?limit=50&include_details=true`：导出最近 Agent run；`include_details=true` 时内联候选决策、交易计划和时间线；同样支持 `trigger_source`、`strategy`、`market`、`status`、`created_from` 和 `created_to` 过滤。
- `GET /agent-runs/{run_uid}`：读取单次运行的候选决策、交易计划、风控/跳过原因和模拟成交关联。
- `POST /agent-runs/{run_uid}/llm-recap`：基于该 run 的结构化审计数据生成一次可选 LLM 复盘，并写入 `diagnostics.llm_recap`；请求体支持 `max_output_tokens`（200~2000，默认 800）。LLM 不可用、空响应或调用失败时返回 `accepted=false` 和失败原因，不改变订单、交易计划或自动交易状态。

## 可选 vn.py runtime 配置

默认不创建 vn.py runtime。vn.py 4.4.0 发布元数据覆盖 Python 3.10 至 3.13；建议为该能力单独使用 Python 3.13，避免让系统默认 Python 3.14 环境承担未经上游声明支持的 GUI/数值依赖。Windows PowerShell 可执行：

```powershell
.\scripts\setup_vnpy_runtime.ps1 -PythonExecutable "python"
.\.venv-vnpy\Scripts\python.exe scripts\check_vnpy_adapter.py --require-vnpy
```

安装脚本会校验 Python 版本，安装项目依赖与 `requirements-vnpy.txt`，优先选择 LiteLLM wheel，并把 pip 构建缓存放在 `.venv-vnpy/.pip-cache`。若 AlphaSift 的远程 Git 安装受限，可先准备固定提交的本地源码，再传 `-AlphaSiftSource <repo-relative-path>`；仅诊断 adapter 时可传 `-SkipProjectDependencies`，但该模式不能作为完整 DSA API 运行环境。验收脚本会构造真实 `OrderRequest`，调用测试 MainEngine bridge，启动内置 `DsaSimulatedGateway` 完成一笔标准订单/成交事件，并关闭 vn.py EventEngine/MainEngine。

runtime 启动前会创建部署工作目录下已忽略的 `.vntrader/`，供 vn.py 保存本地运行状态，避免受限服务账户回退写入用户主目录。需要由 DSA 托管 vn.py EventEngine/MainEngine 时，显式设置：

- `VNPY_RUNTIME_ENABLED=true`：启动时尝试创建 `vnpy.event.EventEngine` 与 `vnpy.trader.engine.MainEngine`。
- `VNPY_GATEWAY_CLASS`：可选 gateway 类导入路径，支持 `module:Class` 或 `module.Class`。
- `VNPY_GATEWAY_NAME`：可选 gateway 名称；若启用 `VNPY_CONNECT_ON_START` 则必填。
- `VNPY_CONNECT_SETTINGS_PATH`：可选连接参数 JSON 文件路径；账号、密码、token 等敏感字段只放在该外部文件中，不写入仓库。
- `VNPY_CONNECT_ON_START=false`：显式改为 `true` 后才会调用 `MainEngine.connect(settings, gateway_name)`。
- `VNPY_AUTO_ATTACH_EVENTS=true`：runtime 创建成功后自动注册订单、成交、账户和持仓事件 handler。
- `DSA_RUNTIME_SCHEDULER_TASK_EVENT_RETENTION_DAYS=30`：后台任务事件持久化保留天数；设为 `0` 时不自动清理。
- `DSA_RUNTIME_SCHEDULER_TASK_EVENT_CLEANUP_INTERVAL_SECONDS=3600`：写入后台任务事件后最多每隔多少秒触发一次旧事件清理；设为 `0` 时每次写入后都检查。

安装 vn.py 本身不会让 bridge 变为可提交状态。只做本地模拟时，可使用仓库内置且默认关闭的即时撮合网关：

```dotenv
VNPY_RUNTIME_ENABLED=true
VNPY_GATEWAY_CLASS=src.services.vnpy_simulated_gateway:DsaSimulatedGateway
VNPY_GATEWAY_NAME=DSA_SIM
VNPY_CONNECT_ON_START=true
VNPY_AUTO_ATTACH_EVENTS=true
```

内置网关无需 `VNPY_CONNECT_SETTINGS_PATH`。API 重启后，模拟交易设置会在未保存 gateway 名称时继承 `VNPY_GATEWAY_NAME`；把执行模式设为 `vnpy_paper` 后即可开启定时自动买入或执行手动委托。合法限价单默认延迟 500 毫秒全量成交，订单、成交、账户和持仓均通过真实 vn.py EventEngine 回到 DSA；DSA Portfolio 仍是跨进程持久化账本。

内置网关只用于功能验收和本地模拟，不提供真实行情撮合、手续费、滑点、部分成交概率或网关状态持久化；进程重启会重置网关内存账户，但不会删除 DSA Portfolio 流水。接真实通道时仍需安装具体 gateway 插件，把敏感连接参数放入外部 JSON，并显式配置 `VNPY_CONNECT_SETTINGS_PATH`。未配置 gateway 时，runtime 和事件引擎可以为可用状态，但 `diagnostics.vnpy_bridge.available=false` 且 reason 为 `gateway_name_not_configured`，这是预期的安全状态。

## vn.py bridge 边界

- `vnpy_paper` 模式负责把 DSA 通过风控后的买入计划和自动卖出计划转换为 vn.py `OrderRequest` 并调用 `MainEngine.send_order`，也可把提交态计划转换为 vn.py `CancelRequest` 并调用 `MainEngine.cancel_order` 发起撤单；订单状态、成交、账户和持仓回报可通过 `/vnpy-events/*` 同步回 DSA，或在宿主进程注入 EventEngine 后通过 `/vnpy-events/attach` 注册自动回调。DSA 也可以在 `VNPY_RUNTIME_ENABLED=true` 时创建 EventEngine/MainEngine 并按配置 add gateway/connect；内置 `DsaSimulatedGateway` 已完成自动交易事件回写验收，真实券商 gateway 的连接参数、重连和长跑仍需部署方显式验证。
- bridge 提交成功只代表 vn.py 接收了委托请求，交易计划状态会记录为 `submitted`；订单状态中的部分成交会记录为 `part_filled` 供审计；只有收到并同步成交回报后，本地 Portfolio 才会写入成交并更新现金和持仓。
- bridge 不可用、gateway 未配置、`OrderRequest` 构造失败或 `send_order` 抛错时，自动交易会把该计划记录为 `failed` 或 `skipped`，不会让整轮 Agent 运行变成 500。

## 回滚

关闭页面中的“定时自动买入”即可停止新增后台自动订单；关闭“启用模拟交易”会阻止手动和自动模拟成交。误点“重置账户”时，旧账户只是被归档，必要时可在 Portfolio 账本中重新启用或按账本纠错流程处理。若需要彻底回滚代码能力，移除 `vnpy-paper` API、Web `/paper-trading` 与 `/agent-console` 页面和 `RuntimeSchedulerService` 中的后台任务注册即可。已有 Portfolio 模拟流水不会自动删除，可在持仓页按账本纠错流程删除。
