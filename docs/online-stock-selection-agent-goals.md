# 在线选股 Agent 与自动模拟交易目标拆解

本文档记录“在线选股 Agent + 自动化选股 + 自动模拟交易”的目标状态、当前完成度和待完成事项。

截至 2026-07-14，本系统已经具备 AlphaSift 在线选股、本地模拟交易账本、Web 入口、定时自动买入、可交易窗口诊断、跨模块系统健康视图、关键异常 alert 路由通知、vn.py 提交态审计、内置即时撮合 gateway、成交回报同步入口、规则 Agent 买入前二次复核、默认关闭的 LLM 动态计划、默认关闭的 LLM 买入前复核、基础复核质量摘要和手动可选 LLM 复盘，但还没有达到完整 Agent 化、稳定自动化和真实 gateway 长跑验收目标。

## 目标状态

最终目标是形成一个可控的在线选股 Agent：

- 自动选择或接收选股策略，定时拉取在线行情、板块、新闻、资金和基础面上下文。
- 生成候选股票列表，并给出结构化理由、风险、置信度和计划仓位。
- 在通过风控后自动写入模拟交易账户。
- 页面可查看每次任务的候选、决策、成交、跳过原因、持仓变化和运行诊断。
- 后续可选择接入 vn.py 标准事件引擎和网关模型，但实盘交易必须另行设计权限、确认和风控。

## 当前完成度

| 模块 | 当前状态 | 完成度 | 未完成重点 |
| --- | --- | ---: | --- |
| 在线选股能力 | AlphaSift 可用，策略接口返回 8 个策略，候选级数据质量/缺失字段/来源已有基础标注，Web 可查看 source health，自动 Agent 已按历史/当前健康动态调整 snapshot 来源顺序 | 约 85% | 长任务稳定性、资金/新闻独立来源权重仍需增强 |
| 选股 Agent | 已有自动选股运行记录、结构化 Agent 计划、规则派生计划档位/执行路由/预算上限/降级动作、默认关闭的 LLM 动态计划、每轮运行总结、基础复核质量摘要、基础 Agent 工作流状态机、严格样本外跨运行质量状态与可选买入门禁、按日结构化运行总结、候选级仓位计划/规则风控复核、规则 Agent 买入前二次复核、默认关闭的 LLM 买入前复核、手动可选 LLM 复盘、候选决策审计和交易计划审计，但还不是完整 Agent 工作流 | 约 89% | 缺少跨市场动态目标、多模型复核质量评估和人工验收闭环 |
| 自动模拟交易 | 已能从 AlphaSift 候选生成交易计划，支持 dry-run、手动审批、按交易时段写入本地 paper 订单，并支持可交易窗口诊断、首次自动买入调度对齐下一交易窗口、基础止损/止盈/移动止损/持仓天数卖出、超时未走强卖出、按比例分批卖出、active 防守决策信号触发的策略失效卖出、单票/组合/行业仓位暴露上限、跨币种预算、按股票/行业目标缺口缩量补仓、候选评分加权、20 日逆波动率、两两相关性上限或目标缺口/协方差优化分配和基础组合再平衡、股票黑名单、候选级/账户级/市场级基础风控 | 约 99% | 仍缺真实 gateway 长跑验证 |
| Web 操作页 | 已有选股页、模拟交易页和独立自动选股 Agent 控制台，支持暂停确认、一键恢复、立即 dry-run 演练、后台调度状态、持久化可筛选后台任务日志、任务趋势摘要、7/30/90 天后台任务长期指标、任务健康检查、模拟账户历史筛选/恢复/切换/隐藏式批量清理、权益曲线、日度收益表、月度收益表、按 Agent run 创建时间筛选的窗口级绩效矩阵、已记录候选的 1/5/10/20 日样本外前瞻评价、时间点因子采集/覆盖检查/单日重放、逐日期历史全市场分批恢复、现金/整手/等权或显式股票目标权重组合回测、最低佣金/卖出税与成本审计、可交易窗口、风控统计、复核质量摘要、运行时间线、分页历史、策略/市场/状态/时间范围筛选、`/agent-console/<runUid>` 独立详情路由、查询参数兼容深链和单次/最近 run JSON 导出 | 约 99% | 仍缺外部权限下的全市场长任务证据、企业行动和按历史制度自动切换费税 |
| vn.py 集成 | 已有可选 `OrderRequest` / `CancelRequest` 映射、`MainEngine.send_order` / `cancel_order` 桥接入口、订单/成交/账户/持仓回写 API、注入式 EventEngine attach、opt-in runtime bootstrap、内置即时撮合 gateway，以及 Python 3.13 隔离环境中的真实 vn.py 4.4.0 Agent 计划到 Portfolio 成交验收 | 约 90% | 真实 gateway 插件/账户连接、回报、重连和长期订阅稳定性仍未验证 |
| 稳定性与可观测 | 有状态接口、错误提示、跨模块 `system_health` 健康视图、AlphaSift source health 页面视图、候选级数据质量标注、自动交易数据质量诊断、可交易窗口诊断、读取持久化最近事件的后台任务健康检查、持久化可筛选后台任务日志、任务趋势摘要、7/30/90 天终态运行长期指标、7/30/90 天跨 run 数据质量趋势、snapshot 动态来源权重与审计、任务事件保留/低频清理策略、Agent run 时间线、基础失败重试、交易计划恢复矩阵、手动恢复扫描、MainEngine 主动订单对账、暂停买入时独立恢复、多笔成交幂等累计、撤单竞态/迟到成交恢复、对账异常保护、熔断状态/恢复入口、自动交易系统事件告警历史和 alert 路由通知尝试审计 | 约 98% | 缺少真实 gateway 长跑恢复验收、跨来源类型权重和性能优化 |

综合判断：

- 本地 paper 的在线选股、自动计划、风控、审计和恢复链路已达到可用 MVP；生产验收仍取决于真实数据与运行窗口。
- 长期稳定版仍缺真实 gateway 长跑、外部权限下的全市场长任务证据、多模型人工验收闭环，以及历史回测侧的企业行动和按历史制度自动切换费税；历史回测显式股票目标权重、最低佣金/卖出税显式配置与成本审计、自动交易候选评分加权、20 日逆波动率、顺序两两相关性约束、目标缺口/协方差优化、风险字段缺失阻断、显式股票/行业目标缺口补仓、基础跨币种估值和预算风控已完成。
- vn.py 标准化本地模拟链路已可启动并完成真实事件回写，剩余重点约为 10%：真实 gateway、账户连接、真实回报和长跑恢复验收。

## Goal 1：稳定当前模拟交易页面与 API

目标：用户打开“模拟交易”页面时，能稳定看到账户状态、持仓、成交和可操作按钮，不再误判“不可用”。

已完成：

- 新增 `/api/v1/vnpy-paper/status`、`/orders`、`/settings`、`/auto/run` 等 API。
- 新增 Web `/paper-trading` 页面。
- 状态中拆分 `available` 和 `vnpy_available`，避免把 vn.py 未安装误判为本地模拟不可用。
- 本地 paper 账户可创建，成交可写入 Portfolio 账本。
- `/api/v1/vnpy-paper/status` 支持轻量查询，可跳过持仓快照和近期成交；完整快照路径已增加 10 秒短 TTL 缓存，并在成交、账户重置或恢复后主动失效。
- Web 模拟交易页已改为先加载可操作状态，再后台刷新完整持仓快照。
- `GET /api/v1/vnpy-paper/status` 新增 `diagnostics.auto_trade_readiness`，Web 模拟交易页新增“可用性诊断”摘要，归纳本地账本、自动任务、调度窗口、交易窗口、连续失败熔断和 vn.py bridge 的状态，辅助判断不可用或未自动执行原因；状态接口已用 `scheduler.loop_running` 区分调度循环存活和 `scheduler.running` 当前任务执行状态，避免空闲调度器被误判为不可用。
- `GET /api/v1/vnpy-paper/status` 新增 `diagnostics.system_health`，把本地账本、选股来源、自动化调度、调度窗口、交易窗口、持仓估值和 vn.py bridge 统一成跨模块健康视图；Web “可用性诊断”会优先展示该结构化摘要，旧后端再回退到 readiness 或本地推导。
- Web 模拟交易页首屏状态加载失败时已区分后端旧进程/路由未加载（404）、状态接口超时和本地服务连接失败，并给出对应排障提示。
- Web 模拟交易页会展示持仓估值降级提示，基于状态诊断、Portfolio 快照限制和持仓级 `price_available` / `price_stale` 标记缺价或陈旧价格，并在当前持仓表显示价格源。
- `GET /api/v1/vnpy-paper/status` 已新增 `diagnostics.alphasift`，readiness 会展示 AlphaSift 选股依赖是否启用、可用、版本和策略数量，便于解释自动交易是否具备候选来源。

未完成：

- 状态接口已有轻量查询、完整快照短 TTL 缓存和页面级估值降级提示；仍需补更完整的价格源健康指标和自动恢复策略。
- 页面级“为什么不可用”已有后端 readiness 诊断摘要、跨模块 `system_health` 健康视图、AlphaSift 依赖状态、状态加载失败分类和持仓估值降级提示；后台任务已有 7/30/90 天长期指标，Agent run 已有 7/30/90 天整体数据质量趋势，仍需补后端版本、细粒度来源权重趋势和更完整的自动恢复策略。

需要做：

- 拆分 `GET /vnpy-paper/status` 为轻量状态和完整快照两个层级。
- 给持仓估值增加短 TTL 缓存和价格源失败页面级降级说明已完成；继续补价格源健康指标。
- Web 页面已展示本地账本、选股来源、自动化调度、调度窗口、交易窗口、持仓估值、AlphaSift 依赖、熔断、vn.py bridge 可用性摘要、持仓估值降级提示、7/30/90 天后台任务长期指标和跨 run 数据质量趋势，并对状态路由 404、状态超时和本地连接失败提供明确提示；继续补后端版本、细粒度来源权重趋势和恢复策略。
- 增加 API 超时和错误码测试，已覆盖路由 404、状态超时和价格源缺失页面提示；继续覆盖后端价格源异常矩阵。

验收标准：

- 打开 `/paper-trading` 首屏状态在 2 秒内返回。
- 后端缺少 vn.py 时页面仍显示“本地模拟可用”，并能手动模拟成交。
- 行情源失败时页面不整体失败，只标记价格不可用或使用缓存。

## Goal 2：实现真正的在线选股 Agent 工作流

目标：把“选股服务”升级为“选股 Agent”，让系统能解释为什么选、为什么不买、买多少、何时放弃。

已完成：

- AlphaSift 可用，当前提供 8 个策略：`dual_low`、`quality_value`、`balanced_alpha`、`momentum_quality`、`capital_heat`、`oversold_reversal`、`shrink_pullback`、`volume_breakout`。
- Web 选股页可启动筛选任务。
- DSA 已有 Agent 策略 YAML，用于分析视角和风险判断。
- AlphaSift 调用期可接入 LLM 重排。
- 自动模拟交易会写入 `stock_selection_agent_runs`，记录 run id、触发来源、策略、市场、参数、候选数、成交数、跳过数和诊断。
- 自动模拟交易 run 诊断新增 `agent_plan`，记录策略、市场、候选数量、每票预算、执行模式、规则派生 `plan_profile`、`execution_policy`、`sizing_plan`、`adaptive_controls`、仓位计划模板、风控预算、候选过滤器、gate 和预期产物。
- 自动模拟交易设置新增默认关闭的 `auto_llm_plan_enabled`：开启后会在调用 AlphaSift 前生成本轮 `llm_dynamic_plan`，允许 LLM 在已知策略白名单内选择策略，并只在已保存上限内收紧候选数、每票预算和最低分；失败时回退保存配置并落审计。
- 自动模拟交易会写入 `stock_selection_agent_decisions`，记录候选代码、名称、评分、理由、风险/跳过原因、计划金额、成交数量、成交价和关联 `trade_id`；候选 `order_result` 内含 `position_plan`、`risk_review` 和 `agent_review`。
- 自动模拟交易会写入 `stock_selection_agent_trade_plans`，记录计划金额、执行模式、计划状态、成交回报和跳过原因；交易计划 `order_result` 同步保存候选级仓位计划、规则风控复核、规则 Agent 买入前二次复核和可选 LLM 买入前复核。
- 自动模拟交易设置新增默认关闭的 `auto_llm_review_enabled`：开启后在规则风控通过后调用 LLM 生成 `order_result.llm_review`；`blocked`、模型不可用、JSON 解析失败或调用异常都会 fail-closed 跳过候选，不会继续生成或提交买入计划。
- 自动模拟交易 run 诊断新增 `agent_summary`，聚合本轮结果、候选/计划/成交/跳过计数、数据质量、主要跳过原因、风控复核统计、Agent 复核状态统计和 LLM 复核状态统计。
- `agent_summary.review_quality` 已能基于规则 Agent 复核、可选 LLM 复核和数据质量输出 `audited`、`guarded`、`needs_review` 或 `idle` 状态、质量分、覆盖率、风险标记和是否建议人工确认。
- Agent run 详情会派生 `diagnostics.agent_workflow`，按计划、数据质量、候选复核、交易计划和执行阶段生成状态机摘要，记录当前阶段、整体状态和建议下一步。
- 每轮规则 Agent 计划会持久化 `recent_run_context`，汇总同策略/市场最近 5 次 run 的状态、数据质量、候选/计划/成交/跳过、成交率和连续失败；默认关闭的 LLM 动态计划复用同一上下文，Agent 控制台展示最近运行状态。
- 每轮运行会基于同策略/市场已持久化候选的严格样本外结果生成 `cross_run_quality`，记录成熟样本、覆盖率、胜率、收益、前一状态和状态迁移；默认关闭的前瞻门禁可在成熟胜率不足或评价不可用时阻断新增买入，证据不足不阻断且卖出风险处置继续运行。
- Web 模拟交易页的 Agent run 详情展示“Agent 计划”和“运行总结”摘要，并在运行时间线中包含 `agent_plan` 与 `agent_summary` 阶段。
- Agent 控制台新增“今日 Agent 总结”，通过 `/api/v1/vnpy-paper/agent-runs/daily-summary` 聚合 run 数、候选/计划/成交/跳过计数、状态分布、执行模式、数据质量、Agent 复核状态、LLM 复核状态、复核质量状态/风险标记/平均分、主要跳过原因和热门标的。

未完成：

- Agent 计划阶段已有基础诊断、规则派生计划档位/降级动作、最近运行上下文、默认关闭的 LLM 动态计划、基础复核质量摘要、基础可解释状态机和跨运行前瞻质量迁移；仍缺跨市场动态目标、更完整的每日目标函数、多模型复核质量评估和人工验收闭环。
- 候选级结构化解释合同已有基础落库，候选级 `position_plan` 和规则 `risk_review` 已写入审计，但还不是独立表级合同。
- LLM 动态计划和买入前二次复核已有默认关闭路径，并记录基础 `prompt_version` / `evaluator_version`；基础复核质量摘要已能标记单轮风险，仍缺多模型/长周期复核质量评估和人工验收闭环。
- 每轮运行总结已有基础结构化 `agent_summary`，跨 run 的每日结构化总结和最近 5 次运行上下文已落地；手动可选 LLM 复盘已能基于结构化审计数据写入 `diagnostics.llm_recap`，但尚未进入自动交易决策闭环。

需要做：

- 扩展 `stock_selection_agent_runs`，Agent 计划阶段、规则派生计划档位/降级动作、默认关闭的 LLM 动态计划、每轮运行总结、每日结构化总结、规则 Agent 复核统计、LLM 买入前复核统计、基础复核质量摘要、默认关闭的 LLM 买入前复核和手动 LLM 复盘已有基础诊断；继续补齐跨市场目标生成、状态机和长周期复核质量评估。
- 扩展候选决策 schema，候选级 `position_plan`、规则 `risk_review`、规则 Agent `agent_review` 与可选 LLM `llm_review` 已写入 `order_result`，LLM 动态计划和买入复核已记录基础提示词/评估版本；继续补策略命中明细、表级独立合同和人工验收闭环。
- 引入 Agent 工作流：拉候选 -> 补上下文 -> LLM/规则复核 -> 风控 -> 生成交易计划。
- Web 选股页增加“Agent 解释”和“本轮自动交易计划”视图。
- 增加任务历史列表与详情页。

验收标准：

- 每次自动选股都有可追溯 run id。
- 自动模拟交易候选能回答“为什么入选、为什么买/不买、计划金额是多少”。
- 自动交易只消费已通过风控的结构化交易计划，不直接消费原始候选列表。

## Goal 3：完善自动模拟交易闭环

目标：让自动选股结果可以稳定、安全地写入模拟交易，而不是只做一次性买入。

已完成：

- 模拟交易设置支持自动交易开关、策略、市场、候选数量、每票金额、间隔分钟、最低评分、跳过已有持仓。
- runtime scheduler 可以在后台按间隔运行自动模拟交易。
- 自动买入在交易时段限制生效且当前不在交易窗口时，会把首次后台任务运行延迟到下一开盘窗口；状态接口通过 `auto_trade_readiness.timing_alignment` 关联调度器下次运行时间和交易窗口。
- 自动订单使用 `dedup_hash` 避免同日同策略同票重复买入；`vnpy_paper` 模式会额外检查同股票同方向活跃提交态计划，避免成交回报到达前重复提交买入或卖出委托。
- 自动交易会保存运行记录和候选决策，页面可查看成交和跳过原因。
- 自动交易支持 `dry_run` 只生成交易计划，不写入 Portfolio 成交。
- 自动交易支持 `manual_approval` 手动审批模式，自动任务只生成待审批交易计划，页面确认后才写入本地 paper 成交。
- 自动交易支持交易日/交易时段 gate，paper 模式在非交易日、盘前、午休、盘后或日历未知时跳过执行，dry-run 与手动审批模式仍可生成计划。
- 自动交易 time gate 已修正为按交易日历 `auto` 推断市场阶段，并使用 `market=` 关键字调用，避免真实运行误判为 `market_phase_unknown` 或把非交易时段强制视为 `intraday`。
- 状态接口新增 `diagnostics.trading_window`，模拟交易页展示“可交易窗口”、下次开盘/收盘、当前阶段和 time gate 是否强制执行。
- 自动交易支持最大持仓数风控，达到上限时以 `max_positions_reached` 跳过候选。
- 自动交易支持基础仓位暴露风控，能以 `single_position_value_limit_reached`、`total_position_value_limit_reached`、`total_position_pct_limit_reached`、`industry_position_value_limit_reached`、`industry_position_pct_limit_reached` 跳过超过单票金额、总持仓金额、总仓位比例、行业金额或行业仓位比例上限的候选；同一轮 dry-run / 手动审批计划单也会计入风控预算。
- 自动交易支持每日买入次数和每日预算风控，分别以 `daily_order_limit_reached`、`daily_budget_exceeded` 跳过候选。
- 自动交易支持股票黑名单风控，命中候选会以 `symbol_blacklisted` 记录跳过并写入交易计划审计。
- 自动交易支持候选级基础风控，能以 `st_or_delisting_risk`、`suspended_stock`、`price_limit_reached`、`liquidity_below_threshold` 跳过 ST/退市风险、停牌、涨跌停和成交额过低候选。
- 自动交易支持账户级基础风控，能以 `cash_low_watermark`、`account_drawdown_limit_reached` 跳过现金低水位和最大回撤超限下的买入候选，并写入告警中心系统事件、复用 alert 路由外发通知。
- 自动交易支持可选大盘红绿灯风控，能以 `market_light_red` / `market_light_yellow` 在最近大盘复盘快照为红灯或黄灯时跳过买入候选。
- 自动交易支持可选连续失败熔断，能以 `failure_fuse_open` 在同策略/同市场连续失败达到阈值后跳过自动买入；状态接口和 Web 页面已展示熔断是否打开、阈值和连续失败数，并提供手动恢复熔断基线入口；熔断打开会写入告警中心 `target=vnpy_paper` 的系统触发历史，并复用 alert 路由外发通知、记录通知尝试。
- 连续失败熔断已修正为锁存语义：触发后的 `failure_fuse_open` 运行继续维持熔断，不会隔轮自行放行，只有显式重置统计基线才恢复。
- 自动交易支持基础卖出风控，默认关闭；开启后在 `paper` 模式下按 `stop_loss_triggered`、`take_profit_triggered`、`trailing_stop_triggered`、`max_holding_days_reached`、`no_progress_timeout` 和 `strategy_invalidated` 写入本地模拟卖出和 Agent 审计，在 `vnpy_paper` 模式下会把卖出提交给 vn.py bridge 并等待成交回报入账；默认整仓退出，也可通过 `auto_sell_position_pct` 按持仓比例分批卖出，设置 `auto_no_progress_days` 后可按超时未走强退出，开启 `auto_signal_exit_enabled` 后会消费 active `sell/reduce/avoid` 决策信号触发策略失效卖出。
- 模拟交易页支持“立即 dry-run”一次性演练，可不保存配置、不开启后台自动交易，临时生成自动选股交易计划和审计记录。
- 交易计划支持基础失败恢复，`manual_approval`、`paper`、`vnpy_paper` 计划若变为 `failed` 或可恢复的 `skipped`，可在页面重试提交并回写交易计划、候选决策和 run 计数。
- vn.py 订单状态回写支持部分成交审计和多笔成交累计：`parttraded` / `partial_filled` 会标记 `part_filled`，成交回报按 `vt_tradeid` 幂等写入 Portfolio 并累计数量、加权均价和剩余数量，达到计划数量后才标记 `filled`。超时恢复会先查询 `MainEngine.get_order` / `get_all_trades` 补同步漏失回报；查询异常会保护活跃计划，所有订单/部分成交/撤单超时结果均禁止自动重下单。
- `serve-only` / `webui-only` 启动现在只禁用每日分析 daily job，不再压制自动模拟交易后台任务；自动买入开启后可在 Web/API 长运行进程中注册 `vnpy_paper_auto_trade` 和 `vnpy_paper_auto_retry`。

未完成：

- 自动交易已有基础止损、止盈、移动止损、持仓天数卖出、超时未走强卖出、按比例分批卖出、active 防守决策信号触发的策略失效卖出，以及基于候选评分、同一决策时点 20 日逆波动率、本地 trailing returns 顺序两两相关性上限或目标缺口/协方差目标函数的约束分配、缺口补仓与卖出侧组合再平衡；真实 gateway 长跑验收仍未完成。
- 交易计划表已有基础状态、手动审批成交、受控计划重试、retry 次数/冷却审计、vn.py 多笔成交累计、MainEngine 超时对账、对账异常保护、提交态/部分成交/撤单请求安全归档、主动撤单、活跃提交态防重复、只读恢复矩阵和手动恢复扫描，但还缺完整订单生命周期的真实 gateway 长跑验证。
- 自动失败恢复已有页面/API 受控重试、后台到期重试调度、页面手动恢复扫描、MainEngine 漏回报对账、状态不明 fail-closed、数据质量阻断告警历史、熔断状态诊断、交易计划恢复矩阵、告警中心系统事件历史和 alert 路由通知尝试审计；剩余重点是真实 gateway 重连、缓存保留和迟到回报长跑验收。

需要做：

- 扩展交易计划层：已具备 `planned`、`submitted`、`part_filled`、`cancel_requested`、`filled`、`skipped`、`failed`、受控重试、后台到期重试、页面手动恢复扫描、提交态/部分成交/撤单请求超时归档、主动撤单、活跃提交态防重复、只读恢复矩阵、关键异常告警历史和 alert 路由通知基础语义；继续补真实 gateway 长跑验证和完整自动恢复策略。
- 已支持 dry-run、手动审批、受控计划重试和自动成交；继续补完整订单生命周期。
- 扩展交易规则：基础止损、止盈、移动止损、持仓天数、超时未走强、按比例分批卖出、策略失效卖出、评分加权/20 日逆波动率/相关性上限/目标缺口协方差优化、暴露上限驱动和目标权重驱动的缺口补仓/基础组合再平衡，以及跨币种估值和预算风控已落地；继续补真实 gateway 长跑验证。
- 已把下次可交易窗口和调度器下次运行时间通过 readiness 关联起来；继续补单次 run 的跳过原因到该诊断链路。
- Web 继续增强每次自动交易的成交、跳过原因、持仓变化和导出能力。

验收标准：

- 自动交易有基础计划、执行记录和提交态超时归档；仍需补完整订单生命周期。
- 自动买入、跳过、失败都能在页面解释。
- 没有通过风控的股票不会写入模拟成交。
- 同一只股票不会因重复任务失控加仓。

## Goal 4：补齐基础风控

目标：自动交易必须先可控，再谈收益表现。

已完成：

- 有基础现金检查。
- A 股买入按 100 股一手向下取整。
- 可选择跳过已有持仓。
- 可设置每票买入金额和最低评分。
- 最大持仓数、每日最大买入次数、每日最大买入金额和股票黑名单已有基础配置。
- 单票最大金额、总持仓金额、总仓位比例、行业最大金额和行业仓位比例已有基础配置，拒单原因分别为 `single_position_value_limit_reached`、`total_position_value_limit_reached`、`total_position_pct_limit_reached`、`industry_position_value_limit_reached`、`industry_position_pct_limit_reached`。
- 候选级基础风控已支持 ST/退市风险、停牌、涨跌停和最低成交额过滤，拒单原因分别为 `st_or_delisting_risk`、`suspended_stock`、`price_limit_reached`、`liquidity_below_threshold`。
- 账户级基础风控已支持最低现金余额和最大回撤限制，拒单原因分别为 `cash_low_watermark`、`account_drawdown_limit_reached`。
- 最大回撤已升级为按账户 ID 持久化的已观测权益峰值口径；Agent run 和系统健康诊断会返回峰值权益、当前权益、回撤比例和阈值，账户重建后仍保留同一账户的峰值。
- 市场级基础风控已支持可选大盘红绿灯 gate，拒单原因分别为 `market_light_red`、`market_light_yellow`。
- 连续失败熔断已有基础实现，拒单原因为 `failure_fuse_open`。
- 完整模拟交易状态会返回行业归属覆盖率、已解析/缺失持仓数、缺失代码和行业市值分布；Web“可用性诊断”展示该组件，配置行业上限或目标权重时覆盖不完整会明确标为阻断。

未完成：

- 每日最大买入次数和每日最大买入金额已按账户基准币种统计，外币汇率缺失时新增买入 fail-closed。
- 候选级基础风控依赖 AlphaSift/DSA 返回的结构化字段；仍需补更稳定的股票状态源和涨跌停状态源。
- 市场环境过滤已有基础大盘红绿灯 gate，仍缺实时市场宽度、热点退潮和多市场联动的独立规则。
- 最大回撤已有基于已观测权益峰值的持久化限制，连续失败熔断已有基础实现、页面诊断、手动恢复入口、告警中心历史和 alert 路由通知，仍缺回撤触发后的自动恢复策略。

需要做：

- 扩展风控配置：单票上限、总仓位金额上限、行业上限、跨币种预算、最大持仓数、每日次数和每日预算已有基础实现。
- 强化股票状态过滤：股票黑名单、ST/退市风险、停牌、涨跌停、成交额过低已有基础实现；继续补独立状态数据源、价格不可用与跨市场规则。
- 强化账户级风控：最低现金余额、权益峰值最大回撤、连续失败熔断和现金低水位告警已有基础实现；继续补连续亏损和熔断自动恢复。
- 强化市场级风控：大盘红绿灯 gate 已有基础实现；继续补指数趋势、市场宽度、热点退潮时降低买入。
- 所有风控结果写入候选决策和交易计划。

验收标准：

- 最大持仓数、单票金额、总持仓金额、总仓位比例、行业金额、行业仓位比例、每日次数、每日预算、股票黑名单、ST/退市、停牌、涨跌停、流动性、账户级现金/回撤、市场红绿灯和连续失败熔断拒单已有明确 reason code；市场宽度/热点退潮和恢复策略仍需补齐。
- 极端行情、数据缺失、现金不足、重复持仓都不会静默成交。
- 页面可以看到本轮风控通过和拒绝数量。

## Goal 5：提升在线数据稳定性

目标：在线选股和模拟交易不能被单一数据源拖垮。

已完成：

- AlphaSift 状态接口可返回 `source_health`。
- Web 选股页已展示 AlphaSift `source_health`，可查看 snapshot/daily 源状态、失败次数、冷却时间和最近错误摘要。
- DSA 已有多数据源 fallback 基础设施。
- 行业板块与热点数据已有部分缓存和 fallback。
- AlphaSift screen 成功返回候选时会写入同策略/同市场 last-good 候选缓存；后续 adapter 运行失败或返回空候选且带 `source_errors` 时，可回退 24 小时内 `quality_status=stale`、`cache_used=true` 的只读候选结果，Web 选股页会展示缓存提示、缓存时间和 stale 数据质量。
- AlphaSift screen 候选已新增 `data_quality`、`missing_fields`、`data_sources` 和 `quality_notes` 基础合同；缓存回退候选还会标记 `cache_used`、`stale`、`cached_at` 和 `stale_age_hours`；Web 选股页会展示候选级数据质量、缺失字段、来源和缓存时间。
- Agent 跨 run 趋势已按 `snapshot/daily + source` 汇总来源健康观测数、降级次数/比例、最新状态和最大失败计数；Agent 控制台按降级率展示具体来源，旧 run 缺少快照时不推断为健康。
- 自动模拟交易已根据 AlphaSift `quality_status`、`warnings`、`source_errors`、`fallback_used`、`stale` 生成基础数据质量诊断；`ok/partial` 可继续执行，`stale/unavailable` 会以 `data_quality_stale` / `data_quality_unavailable` 跳过成交并写入审计，候选级质量快照会同步保存到 `risk_review.candidate_data_quality`。

未完成：

- 当前 AlphaSift snapshot/daily 源已有 Web 可见失败计数、错误摘要和具体来源跨 run 降级率；自动 Agent 已按最近 30 天同策略/市场历史与当前连续失败生成有边界的 snapshot 来源权重和动态顺序，并保留显式优先级，资金/新闻等独立来源仍待接入同一闭环。
- 自动选股运行已按筛选完整性、候选字段/来源覆盖和 AlphaSift snapshot/daily 来源健康生成确定性 0~100 统一质量评分，并支持可选最低分门禁、候选质量分和告警审计；资金/新闻来源还缺少与行情源同粒度的独立健康遥测。
- 数据质量 `stale/unavailable` 和 AlphaSift 筛选异常已写入告警中心系统事件历史并复用 alert 路由外发通知，Web 模拟交易页已展示最近自动交易告警历史；仍缺自动恢复策略。

需要做：

- 基础数据质量合同已下沉到 AlphaSift screen 候选和 Web 选股页；Web 已展示 AlphaSift source health，Agent 控制台已提供整体质量、具体来源降级率趋势和本轮动态来源权重。
- 候选结果已标注数据来源、缺失字段和是否使用 screen cache；后续继续补资金/新闻独立状态源和缺失字段处置策略。
- 自动交易已有基础 gate，只允许消费 `ok` 或可接受的 `partial` 数据；后续需扩展更细的来源权重、候选级缺失字段策略和自动恢复策略。
- 页面已展示 AlphaSift 源健康、本轮降级原因、模拟交易跨模块健康摘要、跨 run 整体质量、具体来源降级趋势和实际生效的动态路由；继续补跨来源类型的长期自动恢复策略。

验收标准：

- 单一行情源失败不导致选股页整体不可用。
- 数据质量不足时自动交易不下单，只生成观察记录。
- 每轮选股都能追溯关键数据来源。

## Goal 6：vn.py 标准化模拟交易集成

目标：在需要时接入 vn.py 标准交易抽象，而不是只使用 DSA 本地账本。

已完成：

- 状态接口可检测 Python 环境是否可导入 `vnpy`。
- 页面能显示 `vn.py 环境未安装`。
- 本地账本已使用 `vnpy_paper` broker 标识，便于后续迁移。
- 新增可选 vn.py adapter 基础映射层，可将 DSA 委托映射为 `OrderRequest` 风格 payload，并在假 vn.py 模块下完成对象构造 smoke test。
- `vnpy-paper` 状态诊断新增 `diagnostics.vnpy_adapter`，可区分 `local_paper_fallback` 与 `vnpy_order_request` adapter 能力。
- 新增 `python scripts/check_vnpy_adapter.py` 可选安装验证脚本；未安装 vn.py 时输出 fallback 诊断，安装后可 smoke 真实 `OrderRequest` 构造。
- 新增可选 `MainEngine.send_order` bridge：应用进程注入 `app.state.vnpy_main_engine` 并配置 `vnpy_gateway_name` 后，手动委托可传 `execution_route="vnpy_bridge"`，自动交易可用 `auto_execution_mode="vnpy_paper"` 把买入计划提交给 vn.py。
- `vnpy-paper` 状态诊断新增 `diagnostics.vnpy_bridge`，Web 模拟交易页显示 MainEngine/gateway/OrderRequest 桥接状态。
- Agent 交易计划已支持 `submitted` 和 `failed` 状态；vn.py bridge 提交成功记录 `submitted`，提交异常记录 `failed`，不会写入本地 Portfolio 成交。
- 新增 `POST /api/v1/vnpy-paper/vnpy-events/trades` 成交回报同步入口，可按 `vt_orderid` 匹配提交态交易计划，幂等写入 Portfolio 交易流水，并回写 Agent 计划、候选决策和 run 计数。
- 新增 `POST /api/v1/vnpy-paper/vnpy-events/orders` 订单状态同步入口，可把 vn.py 拒单、撤单、失效等状态回写为 Agent 交易计划 `failed`，普通提交/部分成交/全部成交状态只更新提交态审计，不写本地成交。
- 新增 `POST /api/v1/vnpy-paper/vnpy-events/account` 和 `POST /api/v1/vnpy-paper/vnpy-events/positions` 外部快照同步入口，状态接口通过 `diagnostics.vnpy_sync_state` 暴露最近账户、持仓和订单状态诊断。
- 新增 `POST /api/v1/vnpy-paper/vnpy-events/attach` 注入式 EventEngine bridge：应用进程提供 `app.state.vnpy_event_engine` 或 `app.state.vnpy_main_engine.event_engine` 后，可注册订单、成交、账户和持仓事件 handler，把 vn.py 事件自动转入 DSA 同步入口。
- 新增 opt-in vn.py runtime bootstrap：`VNPY_RUNTIME_ENABLED=true` 时，API 进程启动会尝试创建 EventEngine/MainEngine，可按 `VNPY_GATEWAY_CLASS` add gateway、按 `VNPY_CONNECT_SETTINGS_PATH` 和 `VNPY_CONNECT_ON_START=true` 显式连接，并自动 attach 事件回调。
- 新增 `requirements-vnpy.txt` 与 `scripts/setup_vnpy_runtime.ps1`，可在 Python 3.10 至 3.13 隔离环境安装项目、AlphaSift 和 vn.py 4.4.0，并在仓库内使用受控 pip 缓存。
- Python 3.13.14 隔离环境已完成真实 vn.py 验收：`OrderRequest` 构造、EventEngine/MainEngine 启停、四类事件 attach 和 API lifespan runtime 注入均通过；后台恢复任务注册由 scheduler/API 回归测试覆盖。
- runtime 会准备部署工作目录下的 `.vntrader/`，避免受限服务账户尝试写用户主目录导致 MainEngine bootstrap 失败。
- 新增默认关闭的内置 `DsaSimulatedGateway`：无需账户参数即可通过真实 MainEngine/EventEngine 延迟即时成交，并已验证 Agent 自动交易计划经订单/成交事件回写为 Portfolio 成交。
- 新增 `POST /api/v1/vnpy-paper/trade-plans/{plan_uid}/cancel` 和 Web “撤单”入口：`vnpy_paper` 的 `submitted` / `part_filled` 计划可映射为 vn.py `CancelRequest` 并调用 `MainEngine.cancel_order`，计划进入 `cancel_requested`，终态仍以 vn.py 订单回报为准。

未完成：

- 系统默认 Python 3.14.6 未安装 vn.py，继续保持本地 paper fallback；完整 vn.py 能力使用已验证的 Python 3.13.14 隔离环境。
- 已有 opt-in Gateway add/connect bootstrap，但尚未安装和配置具体 gateway 插件/账户，真实 gateway 运行态、连接参数、回报、重连和长期事件订阅稳定性未验证。
- 已能调用注入或启动期创建的 `MainEngine.send_order`，并支持订单状态、成交、账户和持仓回报通过 API 手动/外部同步；注入或启动期创建的 EventEngine 可自动 attach 回调，但真实 gateway 连接仍未验收。
- `vnpy_paper` 当前覆盖买入委托提交、自动按比例卖出提交、主动撤单请求、订单/成交状态回写、多笔成交累计、MainEngine 漏回报对账、对账异常保护、提交态/部分成交/撤单请求超时安全归档和活跃委托防重复；真实 gateway 长运行、重连和迟到回报验收仍未完成。
- 安装脚本已处理 Python 版本、GUI/数值依赖、LiteLLM wheel 和受限 pip 缓存；Docker、Desktop 安装体积与打包影响仍未验收。

需要做：

- 明确是否必须接 vn.py；如果只做模拟交易，本地账本已能满足 MVP。
- 若必须接入真实通道，基于已验证的 Python 3.13 runtime 安装对应 gateway 插件，并使用非仓库连接参数文件完成模拟账户验收。
- 继续扩展 vn.py adapter 层：已完成 DSA 委托 -> vn.py `OrderRequest` / `CancelRequest` 基础映射、可选 `MainEngine.send_order` / `cancel_order` 调用、订单/成交/账户/持仓回写 API、注入式 EventEngine attach 和 opt-in runtime bootstrap，下一步需要真实 gateway 连接、长时间运行和异常恢复验收。
- 内置 vn.py 模拟撮合方案已完成；继续增加真实 gateway 长跑验收。

验收标准：

- 未安装 vn.py 时系统仍可使用本地 paper 模式。
- 安装 vn.py 后状态显示 `vnpy_available=true`，`diagnostics.vnpy_adapter.order_request_supported=true`，并能通过真实 vn.py 环境 adapter smoke test。
- vn.py 模式下订单、成交、账户和持仓能同步回 DSA 页面；当前完成提交态审计、成交入账、订单状态回写、账户/持仓诊断快照、注入式 EventEngine attach 和启动期 runtime bootstrap，尚未满足真实 gateway 运行态验收标准。

## Goal 7：Web 产品化与审计

目标：让用户可以在页面上理解自动化系统正在做什么，并能随时暂停和回滚。

已完成：

- 已有选股页、模拟交易页、持仓页。
- 已有基本设置项和自动交易开关。
- 模拟交易页已展示最近自动选股 Agent 运行、候选决策、交易计划、成交和跳过原因。
- 模拟交易页已提供“暂停自动买入”按钮，可直接关闭自动交易配置并触发后台 scheduler 重新 reconcile。
- 模拟交易页已提供“立即 dry-run”按钮，可不修改已保存设置就演练一轮自动选股和交易计划生成。
- 模拟交易页已展示 runtime scheduler 状态，包括后台调度是否启动、自动交易任务是否注册、任务级下次运行时间、调度窗口对齐状态、最近跳过原因、最近错误、`scheduler.task_events` 后台任务日志和 `/task-events` 持久化任务名/状态筛选。
- 模拟交易页暂停自动买入前会要求确认，暂停后提供“恢复自动买入”入口，可重新打开 `auto_trade_enabled` 并触发后台 scheduler reconcile。
- 模拟交易页已支持导出当前选中 Agent run 的 JSON 明细，包含候选决策、交易计划、诊断和成交关联信息。
- 模拟交易页已在 Agent run 详情中按 reason code 聚合展示风控统计，便于快速查看本轮跳过/风险原因。
- 模拟交易页已在 Agent run 详情中展示运行时间线，按开始、数据质量、候选决策、交易计划和完成状态解释本轮执行过程。
- 模拟交易页已提供“重置账户”入口，可归档当前 `vnpy_paper` 模拟账户、创建干净账户并保留旧流水审计。
- 模拟交易页已展示“模拟账户历史”，可按全部、当前、已归档、活跃非当前筛选，并核对当前账户和已归档 `vnpy_paper` 账户；非当前账户可确认后恢复/切换为当前账户，后端只允许切换 `vnpy_paper` 账户并保留历史流水。
- 模拟交易页已展示基础绩效摘要、权益曲线、日度收益表、月度收益表和窗口级绩效矩阵，可按 Agent run 创建时间窗口筛选，聚合当前 paper 账户收益、收益率、成交额、买卖次数、FIFO 卖出胜率、最大回撤、换手率、当前仓位、最近 Agent run 成交/计划/跳过统计、主要跳过原因和按策略/行业归因。
- 模拟交易页已提供最近 Agent run 批量导出入口，可按策略、市场和状态筛选后导出运行详情、候选决策、交易计划和时间线 JSON。
- Web 已新增独立“Agent 控制台”入口 `/agent-console`，可分页浏览并按策略、市场、状态和时间范围筛选自动选股 run，查看今日结构化总结、复核质量摘要、运行详情、Agent 计划、运行总结、时间线、候选决策、交易计划，支持 `/agent-console/<run_uid>` 独立详情路由并兼容 `/agent-console?runUid=<run_uid>` 查询参数深链，并导出当前 run 或最近 run JSON；模拟交易页也已提供可用性诊断摘要。

未完成：

- 独立任务历史页已有 MVP 控制台、分页、筛选表格、`runUid` 查询参数兼容深链和 `/agent-console/<runUid>` 独立详情路由；后台任务日志已在模拟交易页基于 `scheduler.task_events` 和持久化 `/task-events` 展示，并支持按任务名/状态筛选。
- 一键重置测试账户已有基础能力，并已有旧归档账户历史列表、状态筛选、恢复/切换和隐藏式批量清理入口；隐藏式清理不删除 Portfolio 流水。
- 运行中状态、任务级下次运行时间、最近错误、后台任务日志和 Agent run 时间线已有基础状态字段。
- 绩效摘要已有 MVP，并已包含本地 paper 权益路径页面曲线、日度收益、月度收益、最大回撤、换手率、按策略/行业归因、窗口级绩效矩阵、Agent run 创建时间窗口筛选、已记录候选的 1/5/10/20 日样本外前瞻评价、带字段覆盖门禁的单日时间点策略重放，以及按现金、A 股整手和等权或显式股票目标权重执行的跨日组合回测；Tushare 生命周期元数据已按每个快照日期独立还原全 A 股成员并接入持久化、分批、租约保护的可恢复因子采集，仍缺成功在线长任务证据、企业行动处理和市场费税细则。

需要做：

- Agent 控制台和运行历史表格已有 MVP，独立详情路由已落地；后台任务日志已在模拟交易页落地，支持按任务名和状态筛选持久化最近事件。
- 增加详情页：候选、解释、风控、订单、日志；当前模拟交易页和 Agent 控制台已有内嵌候选、交易计划和运行时间线基础详情。
- 暂停确认、恢复、立即执行、立即 dry-run、深链详情、持久化可筛选后台任务日志、任务趋势摘要、任务健康检查、账户历史筛选/恢复/切换/隐藏式清理、日度收益表、月度收益表和导出已有基础入口；继续补完整历史行情驱动的长期绩效审计。
- 测试账户重置、旧归档账户历史列表、状态筛选、恢复/切换、隐藏式清理、单次 Agent run JSON 导出和最近运行批量导出已有基础入口，并支持策略/市场/状态/时间范围筛选。
- 运行中状态、任务级下次执行时间、最近错误、读取持久化最近事件的后台任务健康检查、持久化可筛选后台任务日志、任务趋势摘要、7/30/90 天终态运行长期指标、跨 run 数据质量趋势、任务事件保留/低频清理策略和 Agent run 时间线已有基础展示；继续补细粒度来源权重和长期可用率。

验收标准：

- 用户不看日志也能知道自动交易为何执行或未执行。
- 页面可以暂停自动交易，并确认后台 scheduler 已停。
- 任一模拟成交都能追溯到选股 run 和决策原因。

## Goal 8：测试、回测与验收

目标：上线前能证明系统不会乱买、不会沉默失败、不会因数据源波动误导用户。

已完成：

- 已有 AlphaSift API 测试、vn.py paper 服务/API 测试、Web 页面测试。
- 已有部分 runtime scheduler 测试。
- 已新增 API 端到端回归，覆盖保存自动交易设置、固定 AlphaSift 候选、运行自动选股、写入模拟成交、回查 Agent run 决策/交易计划/时间线。

未完成：

- 端到端主路径已有基础覆盖，仍缺多候选、拒单、数据源失败和 scheduler 长运行场景矩阵。
- 自动交易 dry-run 已有基础回归测试。
- 缺少数据源失败、行情缺价、LLM 超时、重复订单等更完整回归；非交易时段已有基础 gate 测试。
- 纸面交易绩效摘要已有基础能力，已包含权益曲线、日度收益、月度收益、FIFO 卖出胜率、最大回撤、换手率、策略/行业贡献评估、窗口级绩效矩阵、Agent run 创建时间窗口筛选、已记录候选的严格后向多周期评价、单日时间点策略重放和现金/整手/等权或显式股票目标权重组合回测；组合回测可显式配置最低佣金和卖出税并审计逐笔/逐期/汇总成本。历史全 A 股逐日期成员解析和分批可恢复因子采集已完成，仍缺具备外部权限后的成功在线长任务证据、企业行动和按历史制度自动切换费税。

需要做：

- 端到端集成测试已有固定候选和固定成交价的 API 主路径；继续扩展失败矩阵和 scheduler 长运行路径。
- 增加风控拒单测试矩阵。
- 增加 scheduler 自动任务测试，覆盖开启、关闭、重启 reconcile。
- 增加 Web 测试，覆盖任务历史和详情。
- 基础模拟交易绩效摘要已覆盖收益、收益率、权益曲线、日度收益、月度收益、成交额、买卖次数、FIFO 卖出胜率、最大回撤、换手率、当前仓位、成交/计划、跳过原因、策略/行业归因、窗口级绩效矩阵、Agent run 创建时间窗口筛选、已记录候选的多周期前瞻评价、时间点策略重放，以及带成本/基准、显式股票目标权重、重叠持仓保留、卖出受阻跨期延续和基础停牌/涨跌停约束的跨日组合回测；继续补历史全市场在线证据、企业行动和市场费税细则。

验收标准：

- CI 可覆盖自动交易主路径。
- 自动交易错误不会被吞掉。
- 每次策略改动可以看到回测或模拟绩效影响。

## 推荐交付顺序

### Phase 1：可用 MVP

目标：先让“自动选股 -> 自动模拟买入”稳定、可解释、可暂停。

任务：

- 优化模拟交易状态接口性能。
- 已新增选股 Agent run 持久化。
- 已新增候选决策基础 schema。
- 已新增基础交易计划层和 dry-run 执行模式。
- 已在 Web 模拟交易页展示最近运行、候选、交易计划、成交、跳过原因。

未完成程度：当前约 35% 未完成。

### Phase 2：安全自动化

目标：自动交易可长期运行，不会失控加仓或在坏数据下成交。

任务：

- 行业仓位、单票/总仓位、黑名单、候选级基础风控、账户级基础风控、市场红绿灯 gate、数据质量风控和候选级缺失字段展示已落地，继续补来源权重、独立状态源和缺失字段处置策略。
- 卖出、止损、止盈、暴露上限、跨币种预算、评分加权/20 日逆波动率/相关性上限/目标缺口协方差优化和目标权重驱动的基础调仓规则已落地；继续补真实 gateway 长跑验证。
- dry-run、手动审批、受控计划重试、交易计划恢复矩阵、手动恢复扫描、连续失败熔断、关键异常告警历史和 alert 路由通知已有基础能力；继续补完整订单生命周期真实 gateway 验证和完整自动恢复策略。
- 完善自动失败重试、熔断和订单超时后的自动恢复策略。

未完成程度：当前约 64% 未完成。

### Phase 3：Agent 产品化

目标：让系统像一个可审计的选股 Agent，而不是后台脚本。

任务：

- Agent 控制台已有独立 Web 入口、分页、深链详情、独立详情路由和复核质量摘要；模拟交易页已有持久化可筛选后台任务日志、任务趋势摘要、任务健康检查、旧归档账户历史列表、状态筛选、恢复/切换、隐藏式批量清理入口、日度收益表和月度收益表，继续补完整历史行情驱动的长期绩效指标。
- 增加每轮总结、候选解释、风险复核和更细运行日志；Agent run 时间线已有基础实现。
- 基础策略绩效摘要已有页面展示，并返回本地 paper 权益路径曲线、日度收益、月度收益、最大回撤、换手率、当前仓位、分策略/分行业归因、窗口级绩效矩阵和 Agent run 创建时间窗口筛选；继续增加回测级绩效矩阵和完整回测。
- 一键暂停确认、一键恢复、单次导出、最近运行筛选批量导出、后台任务日志、账户重置、旧归档账户历史筛选、恢复/切换和隐藏式清理已有基础能力。

未完成程度：当前约 60% 未完成。

### Phase 4：vn.py 标准集成

目标：可选接入 vn.py 标准交易事件模型，保留本地 paper 作为 fallback。

任务：

- 定义可选依赖、Python 3.10 至 3.13 版本边界和安装验证；`requirements-vnpy.txt`、PowerShell 安装脚本及真实 runtime smoke 已完成。
- 接 vn.py EventEngine / Gateway 或 paper adapter；当前已完成 `OrderRequest` / `CancelRequest` payload 映射、adapter 诊断、可选注入式 `MainEngine.send_order` / `cancel_order` 调用、订单/成交/账户/持仓回写 API、注入式 EventEngine attach 和 opt-in runtime bootstrap，并在 Python 3.13 隔离环境完成 EventEngine/MainEngine 启停验收。
- 映射订单、成交、账户、持仓；当前订单请求、提交态、订单状态回写、成交入账、账户/持仓诊断快照、注入式 EventEngine 回调和启动期 add/connect 入口已有基础桥接，真实 Gateway 运行态未完成。
- 增加 vn.py smoke test 和文档。

未完成程度：当前约 35% 未完成，主要集中在真实 gateway 与部署打包验收。

## 明确不属于当前目标的事项

- 不直接接入实盘券商交易。
- 不默认开启自动买入。
- 不在数据质量不足时强行成交。
- 不把 LLM 结论作为唯一买入依据。
- 不绕过用户配置和风控去执行订单。
