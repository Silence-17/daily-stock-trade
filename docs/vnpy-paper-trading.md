# vn.py 模拟交易说明

本功能在 Web 侧新增“模拟交易”入口，后端提供 `vn.py` 风格的 paper trading API。默认实现写入 DSA 本地持仓账本；当运行进程显式注入 vn.py `MainEngine` 并配置 gateway 名称时，可选择把委托提交到 vn.py `MainEngine.send_order`。该能力仍定位为可选模拟/桥接能力，不默认连接实盘券商、CTP 网关或任何真实交易通道。

## 能力边界

- 模拟账户使用现有 Portfolio 账本，默认券商标识为 `vnpy_paper`，账户名为 `vn.py 模拟交易`。
- 状态接口用 `available` 表示本地模拟账本是否可用，用 `vnpy_available` 表示当前 Python 环境是否可导入 `vnpy`；未安装 `vnpy` 不影响本地模拟账本使用。
- 后端新增可选 vn.py adapter 诊断：`diagnostics.vnpy_adapter` 会说明是否能导入 `vnpy.trader.object.OrderRequest` / `CancelRequest` 与常量枚举，并保留 DSA 委托到 vn.py `OrderRequest` / `CancelRequest` 字段的基础映射。`diagnostics.vnpy_bridge` 会说明当前进程是否已注入 `MainEngine`、是否配置 `vnpy_gateway_name`，以及是否可通过 `MainEngine.send_order` 提交委托、通过 `MainEngine.cancel_order` 发起撤单、通过 `MainEngine.get_order` / `get_all_trades` 执行超时订单对账。
- 手动模拟委托支持买入和卖出；A 股买入会按 100 股一手向下取整，按金额下单不足一手时返回 `cash_below_min_lot`，不会写入成交。
- 未传成交价时，后端会尝试读取 DSA 实时行情；价格不可用、金额不足、数量取整后为 0、重复自动单或超卖时不会写入成交。
- 自动模拟交易默认关闭。开启后，runtime scheduler 会按页面设置的间隔调用 AlphaSift 选股，并对候选票提交本地模拟买入；模拟交易总开关开启时始终注册 `vnpy_paper_auto_retry`，首次注册立即扫描一次并按 1 到 5 分钟间隔恢复已有订单，同时检查最近校准状态告警是否需要有限重试；自动买入开启时再注册 `vnpy_paper_auto_trade`。API 启动期创建或注入的 MainEngine/EventEngine 会传给这些后台任务使用，不再只对 Web 请求可见。若当前执行模式强制交易时段限制且服务启动时不在交易窗口内，首次自动买入会延迟到下一开盘窗口触发，避免按固定间隔从盘后启动后每天都在盘后跳过。
- 开启 `DSA_AGENT_CALIBRATION_SHADOW_ENABLED` 后，scheduler 还会注册 `agent_calibration_evidence` 只读任务：首次延迟 600 秒，随后与 shadow 使用相同周期。它复用 90 天证据门禁，持久化 pending/ready、逐市场计数和有界失败原因；不会创建 Agent run、交易计划或订单。证据不足是正常完成状态，只有读取/评估异常才记为任务失败。
- 校准证据监控会把首次状态及后续 ready/pending 变化写入 `target=vnpy_paper` 的告警历史，并复用 alert 通知路由。pending 使用 `degraded`，ready 使用 `resolved`；状态未变化时不重复写告警或发送通知。状态基线来自持久化告警记录，因此服务重启不会重置去重。告警存储或通知异常只记录 warning，不会让只读监控失败，也不会创建 Agent run、交易计划或订单。
- `GET /agent-runs/calibration-evidence` 同时返回最近校准状态告警的投递摘要，包括告警状态/时间、投递总数、成功/失败/可重试数量、不含敏感诊断的逐渠道结果和 `retry_policy`。Agent 控制台直接展示投递状态，以及自动重试的 `waiting`、`due`、`succeeded`、`not_retryable`、`exhausted` 或 `not_applicable`。可重试失败由已有 `vnpy_paper_auto_retry` 约每 5 分钟检查，逐渠道持久化尝试号，最多 3 轮投递；到期判定吸收 5 秒调度抖动，避免固定节拍在冷却截止前擦边而永久错过。服务重启不会清零，也不会因同一轮多渠道投递而重复计算。成功、未配置渠道、通知抑制、不可重试失败和次数耗尽均停止重发。该路径只重发最近校准状态通知，不运行 Agent、不创建交易计划或订单；告警历史读取失败会单独降级，不隐藏校准证据主体，也不会阻断同轮订单恢复。
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
- 状态接口新增 `diagnostics.auto_trade_readiness`，结构化返回自动交易 readiness：总状态、下一步建议、阻断原因、关注项和本地账本、自动交易开关、runtime scheduler、自动任务、AlphaSift 选股依赖、调度窗口、交易窗口、连续失败熔断、vn.py bridge 等组件状态。`diagnostics.auto_trade_readiness.timing_alignment` 会按自动任务下次触发日期投影对应交易窗口并展示开收盘时间，避免跨日期误报；`window_session_date` 标识本次比较使用的交易日。`diagnostics.alphasift` 会轻量返回自动交易依赖的 AlphaSift 启用状态、可用性、版本和策略数量；异常只写入诊断，不会拖垮本地 paper 状态接口。
- 启用交易时段门禁时，日级自动交易任务会把首次运行锚定到可交易窗口开始后 5 分钟，为 runtime 启动、行情初始化和在线选股预留时间。服务在窗口中重启且当天尚未运行时，会选择至少 5 分钟后仍可交易的当前或下一段窗口；当天已有正式自动运行时直接对齐下一交易日，避免重启重复下单。小于 24 小时的自定义轮询仍沿用原间隔语义。
- readiness 还会把配置文件中持久化的 `last_auto_run` 映射为“最近运行结果”，返回运行时间、Agent run id、是否接受/跳过、原始 reason code 和候选/提交计数；统一 `system_health` 与 Web“可用性诊断”复用该组件。成功运行显示 ready，最近跳过或未完成显示非必需 warning，不会仅凭历史结果制造当前硬阻断；因此即使 scheduler task event 已清理或缺失，页面仍能解释上一轮为什么没有交易。
- 状态接口新增 `diagnostics.system_health`，把本地账本、选股来源、自动化调度、调度窗口、交易窗口、持仓估值、行业归属和 vn.py bridge 合并为跨模块健康视图。`required_blockers` 表示会阻断自动执行的必需组件，`warnings` 表示需要关注但不一定阻断的降级，`disabled` 表示因配置或轻量查询暂未启用的组件。
- `diagnostics.vnpy_runtime.connect` 会区分连接请求已受理与网关已确认连接：`request_accepted=true` 只表示 `MainEngine.connect()` 已返回，只有网关的 `get_connection_status()`、`get_state_snapshot()` 或公开 `connected` 状态确认后才返回 `connected=true`。连接调用异常记录为 `connect_failed` 且不拖垮 API 启动；无法确认的异步第三方网关显示 warning，明确断开时系统健康 blocked，自动新增委托以 `vnpy_gateway_disconnected` 跳过。状态读取会动态刷新支持状态钩子的网关。
- 可通过 `VNPY_AUTO_RECONNECT_ENABLED=true` 显式开启无人值守重连监控，并用 `VNPY_AUTO_RECONNECT_INTERVAL_SECONDS` 设置 5 至 3600 秒基础检查间隔（默认 60）。连续重连失败后，下一次等待时间按基础间隔指数增长，并由 `VNPY_AUTO_RECONNECT_MAX_INTERVAL_SECONDS` 封顶（5 至 3600 秒，默认 300，实际值不低于基础间隔）；连接确认或重连成功后自动复位。监控只在启动连接已配置且当前状态明确为 `failed` / `disconnected` 时重读 `VNPY_CONNECT_SETTINGS_PATH` 并重连；`connect_requested` / `connection_unconfirmed` 不会触发。已受理但尚未确认的异步连接还会等待 `VNPY_AUTO_RECONNECT_CONFIRMATION_GRACE_SECONDS`（5 至 600 秒，默认 30），避免登录初始化期间重复连接。`diagnostics.vnpy_runtime.auto_reconnect` 返回线程状态、基础/当前/最大间隔、连续失败数、检查/尝试时间、尝试/成功/失败次数、最近结果和下次检查时间；配置默认关闭且保存后需重启生效。
- `POST /api/v1/vnpy-paper/gateway/reconnect` 和 Web 顶部“重连网关”可由操作员立即执行一次安全重连。该入口不要求开启后台自动重连，但要求当前进程已经创建 runtime、MainEngine 并配置 gateway；已连接、连接确认中或仍在异步确认宽限期时返回无操作，明确失败/断开或尚未按启动配置连接时才重读外部参数并调用一次 `MainEngine.connect`。响应返回 `attempted`、`connected`、`status`、`reason` 及连接/重连审计，不运行选股、不创建交易计划、不提交订单。
- 自动和手动重连共用异步事件队列：首次连续失败记录 `vnpy_gateway_reconnect_failed`，退避首次达到最大间隔记录 `vnpy_gateway_reconnect_backoff_capped`，连接恢复记录 `vnpy_gateway_reconnected`（`resolved`）。事件进入现有 `/api/v1/alerts/triggers?target=vnpy_paper` 历史并复用 alert 通知路由、去重和冷却；重复失败不反复创建开启事件，runtime 关闭时会先停止重连再排空事件队列。持久化诊断仅包含 gateway 名称、自动/手动触发方式、连接状态、确认来源、连续失败数和当前退避间隔，确保现有告警字段内仍是完整 JSON，且不包含连接参数路径或凭据。Web 手动重连完成后会刷新该告警历史。
- 完整状态的 `diagnostics.system_health.components[key=valuation]` 会基于已经加载的 Portfolio 快照汇总持仓估值健康，不新增行情请求。字段包括可用/新鲜/缺失/陈旧/状态未知数量、价格覆盖率、新鲜覆盖率、`price_source` / `price_provider` 分布、受影响代码以及最早/最新价格日期；Web“可用性诊断”直接展示覆盖率和来源摘要，轻量状态仍以 `snapshot_not_requested` 返回。
- 完整状态的 `diagnostics.industry_exposure` 返回行业归属解析状态、持仓总数、已解析/缺失数量、覆盖率、缺失代码和按行业汇总的持仓市值。未配置行业风控时使用 `snapshot_only` 模式，只统计快照已有字段且不发起逐股网络请求；配置行业金额/比例上限或目标行业权重后切换到 `risk_guard` 模式，该组件属于必需风控条件，覆盖不完整会 fail closed。轻量状态只返回 `snapshot_not_requested`，不会拉取持仓或行业数据。
- 配置 `auto_max_drawdown_pct` 后，账户回撤按账户 ID 持久化的已观测权益峰值计算，峰值至少为初始资金。`diagnostics.account_drawdown` 和 Agent run 的 `diagnostics.account_risk` 返回 `basis=observed_equity_peak`、当前权益、峰值权益、回撤比例和阈值；恢复旧账户会继续使用该账户的峰值，新建/重置账户使用新账户 ID 独立计算。达到阈值时系统健康的“账户回撤”组件阻断新买入，卖出风险处置不受影响；配置的恢复缓冲决定自动解锁线。
- Web 模拟交易页“可用性诊断”摘要会优先使用 `diagnostics.system_health.components`，旧后端无该字段时回退到 `diagnostics.auto_trade_readiness`，再无 readiness 时才基于现有状态字段本地推导，帮助快速判断为何不可用或为何本轮不会自动提交。
- 状态接口的 `diagnostics.backend` 返回 API 版本、vn.py paper contract 版本、可选 build id、Python 版本和进程启动时间；统一系统健康包含“后端版本”组件。当前 Web 以 contract `3` 为兼容基线：字段缺失或低于基线时显示“需更新”，可直接识别页面已更新但 API 仍是旧进程。部署可选设置 `DSA_BUILD_ID`；未设置时兼容 `GITHUB_SHA`、`RENDER_GIT_COMMIT` 和 `RAILWAY_GIT_COMMIT_SHA`，本地运行无需配置。
- Web 模拟交易页的首屏状态加载失败时会区分常见排障原因：`/api/v1/vnpy-paper/status` 返回 404 时提示后端可能仍是旧进程或未加载 vn.py paper 路由；请求超时时提示优先检查轻量状态接口和行情/估值数据源；本地连接失败时提示检查 Web/API 服务和 `API_BASE_URL`。
- Web 模拟交易页会根据状态诊断、Portfolio 快照 `limitations` 和持仓级 `price_available` / `price_stale` 展示“持仓估值降级”提示；当前持仓表同步显示价格源，缺价时标记为“缺价”，避免市值或浮盈显示为 0 时缺少解释。
- 状态接口的完整持仓快照使用进程内 10 秒短 TTL 缓存，减少页面刷新时重复重放 Portfolio 和拉取行情估值；本地成交、vn.py 成交回调、账户重置或账户恢复后会主动失效缓存。`diagnostics.snapshot_cache_hit` 和 `diagnostics.snapshot_cache_ttl_seconds` 可用于判断本次状态是否来自缓存。
- 自动模拟交易支持最大持仓数风控；当前持仓数量达到上限时，新候选会以 `max_positions_reached` 写入跳过记录，不会提交模拟成交。
- 自动模拟交易支持基础仓位暴露风控；可设置单票最大持仓金额、组合最大持仓金额、组合最大仓位比例、行业最大持仓金额和行业最大仓位比例，触发时分别以 `single_position_value_limit_reached`、`total_position_value_limit_reached`、`total_position_pct_limit_reached`、`industry_position_value_limit_reached`、`industry_position_pct_limit_reached` 写入跳过审计。`dry_run` 和 `manual_approval` 的计划单也会占用同一轮风控预算，避免一次计划超出上限。配置行业上限后，若候选或已有持仓行业无法解析，会以 `industry_exposure_unavailable` 保守跳过。
- “每票金额”、每日预算、最低现金和所有金额型仓位上限统一按模拟账户 `base_currency` 计价。港股、美股、日股、韩股和台股下单前会把基准币种预算换算为 `HKD`、`USD`、`JPY`、`KRW` 或 `TWD`；Agent run 的 `diagnostics.currency_budget` 以及订单 `raw.fx_conversion` 会记录基准金额、交易币种金额、汇率和来源。缺少当前汇率、汇率被标记 stale 或汇率日期超过 7 个自然日时，新增买入以 `fx_rate_unavailable` 保守跳过；卖出减仓不因汇率缺失被阻断。已有外币成交的每日预算和仓位暴露也按账户基准币种汇总。
- 自动模拟交易支持每日买入上限和每日预算风控；达到次数上限时记录 `daily_order_limit_reached`，超过预算时记录 `daily_budget_exceeded`，均不会提交模拟成交。
- 自动模拟交易支持股票黑名单风控；页面可填写逗号分隔代码，命中的候选会以 `symbol_blacklisted` 写入候选决策和交易计划，不会提交模拟成交。
- 自动模拟交易支持候选级基础风控；可过滤 ST/退市风险、停牌、涨跌停和成交额过低的候选，分别以 `st_or_delisting_risk`、`suspended_stock`、`price_limit_reached`、`liquidity_below_threshold` 写入跳过审计。启用 ST/停牌/涨跌停过滤但候选明确缺少交易状态时，以 `candidate_trading_status_unavailable` 保守跳过；配置最低成交额但成交额不可用时，以 `liquidity_data_unavailable` 跳过。AlphaSift/DSA 已补出的 `amount`、`turnover_amount`、`limit_status`、`is_suspended` 等字段会被用于判断。
- 自动模拟交易支持账户级基础风控；可设置最低现金余额、最大回撤百分比和回撤恢复缓冲。最大回撤按账户 ID 持久化已观测权益峰值；触发后锁存自动买入，只有回撤降至“最大回撤 - 恢复缓冲”才解锁，并写入 `account_drawdown_recovered` / `resolved` 恢复事件。现金低水位和回撤锁存仅阻断新增买入，不阻断卖出。
- 可选 `auto_consecutive_loss_limit` 使用本地 paper 成交流水按 FIFO 成本计算连续已平仓亏损笔数，与任务执行失败熔断分离。达到上限后以 `consecutive_loss_limit_reached` 暂停新增买入，`auto_consecutive_loss_cooldown_minutes` 到期后自动恢复；后续盈利或持平平仓会立即清零连续亏损。门禁开启和恢复分别写入 `triggered` / `resolved` 系统事件，卖出风险处置始终不受该门禁影响。
- 自动模拟交易支持可选的大盘红绿灯风控；开启后读取最近一次大盘复盘持久化的 `MarketLightSnapshot`，默认只在红灯时以 `market_light_red` 跳过买入，也可配置红灯和黄灯都跳过（`market_light_yellow`）。任一市场上下文门禁启用后，最近快照缺失或读取失败会以 `market_context_unavailable`（或单一宽度/退潮门禁对应的 unavailable reason）拒绝新增买入。
- 自动模拟交易还支持默认关闭的“市场宽度风控”和“热点退潮风控”。市场宽度使用最近快照的 `dimensions.breadth.score`，低于配置值时以 `market_breadth_below_threshold` 跳过新增买入；热点退潮比较最近两次快照的 `dimensions.limit.score`，回落达到配置值时以 `hotspot_retreat_detected` 跳过新增买入。启用对应规则后，所需维度或前序快照缺失会分别以 `market_breadth_unavailable` / `hotspot_retreat_unavailable` fail-closed。页面可设置最近快照最长年龄，默认 7 个自然日、范围 1 至 30 天；非法日期、未来日期或超期快照分别按不可用或 `market_context_stale` fail-closed。快照、当前/前值、阈值、年龄和代理口径会写入 `diagnostics.market_context_risk` 及 Agent 时间线；卖出风控先执行，不受这些买入门禁影响。该规则消费持久化盘后快照，不等同于盘中实时市场宽度。
- 默认关闭的“盘中指数与实时宽度风控”直接复用 `DataFetcherManager.get_main_indices()`；A 股还调用 `get_market_stats()`，以 `上涨家数 / (上涨 + 下跌 + 平盘家数) * 100` 计算实时宽度。主指数等权平均涨跌幅或 A 股宽度低于页面阈值时，分别以 `intraday_market_index_below_threshold` / `intraday_market_breadth_below_threshold` 阻断新增买入；取数异常、空指数或无效涨跌家数分别 fail-closed 为对应 `*_unavailable`。其他市场当前只检查指数，不伪造宽度。启用该门禁后，“要求可验证行情时间”默认开启：指数和 A 股宽度必须有完整、合法且不超过 15 分钟的 provider 时间戳；关闭此项是显式降级，只接受 provider 标记为实时但无法验证 quote as-of 的结果，相关开关、覆盖率和拒绝原因仍写入 Agent 诊断。
- 默认关闭的“跨市场联动风控”按固定映射 v1 读取关联市场主指数：`cn -> hk,us`、`hk -> cn,us`、`us -> hk`、`jp/kr/tw -> us`。每个市场使用方向性指数的等权平均涨跌幅，美国 `VIX` 会被排除；任一关联市场低于阈值时使用 `cross_market_index_below_threshold`，任一关联市场缺少有效指数时使用 `cross_market_context_unavailable`。`diagnostics.market_context_risk` 记录映射版本、逐指数涨跌幅、聚合口径、阈值和阻断市场。行情是 provider 返回的最新可用 quote，当前没有统一跨 provider 的 quote as-of 字段，因此这是一层确定性基础门禁，不应被解释为交易所级同步行情。
- 每次实时指数或宽度取证有 15 秒调用预算，进程内最多保留 4 个尚未返回的取证线程。超时或槽位耗尽不会等待无上限，也不会绕过门禁；当前候选分别以 `intraday_market_index_unavailable`、`intraday_market_breadth_unavailable` 或 `cross_market_context_unavailable` 停止新增买入，底层诊断用 `*_fetch_timeout` / `*_worker_pool_exhausted` 区分原因并记录耗时。超时线程只能等待 provider 自身返回，无法被 Python 安全强制终止，因此工作池上限是防止持续堆积的第二道边界。
- `DataFetcherManager` 会在成功的指数/宽度 fallback 结果追加 `provider` 和 `fetched_at`；各 provider 能确认时还会给出 `provider_timestamp`、`provider_timestamp_coverage_pct`、`data_date` 和 `data_granularity`。TickFlow 指数使用 quote 自带时间并标记 `realtime`，市场宽度仅在所有有效样本都有时间戳时给出最早 provider 时间；efinance/AkShare 明确来自实时端点但当前没有统一 provider 时间，YFinance 两日 history 标记 `session_bar`，Tushare `index_daily` 标记 `end_of_day`。盘中本市场门禁拒绝 `end_of_day`、非当前市场日期的 session bar、未来/非法 provider 时间，以及超过 15 分钟的 provider 时间；严格模式还拒绝时间戳缺失或宽度时间戳覆盖不完整的证据，拒绝明细写入 `rejected_indices`。跨市场联动允许已收盘市场的最新 `session_bar`，因为其用途是隔夜/关联市场背景而非声称同步盘中报价。
- 自动模拟交易支持可选连续失败熔断；开启后按同策略、同市场、同触发源统计最近失败运行，连续达到阈值时以 `failure_fuse_open` 跳过本轮自动买入。熔断只统计 `failed`、数据质量 `stale/unavailable`、非良性 error 以及后续熔断跳过记录，不把自动交易关闭、非交易日、交易时段外等正常跳过计为失败。熔断打开后保持锁存；可继续使用 Web“恢复熔断”或对应 API 手动重置，也可显式开启默认关闭的 `auto_failure_fuse_auto_recovery_enabled` 并设置 `auto_failure_fuse_cooldown_minutes`。自动恢复会持久化首次打开时间，冷却到期后只放行当前一轮作为恢复探测；若探测再次失败，后续任务会重新熔断并开始新的冷却周期。状态接口通过 `diagnostics.failure_fuse` 返回打开时间、预计恢复时间、剩余冷却秒数和最近自动恢复时间，自动恢复写入 `failure_fuse_auto_recovered` 系统事件；任何复位都不删除历史 Agent run。
- 自动交易的关键异常会写入告警中心触发历史并复用 alert 路由主动外发通知：连续失败熔断打开记录 `failure_fuse_open`，连续已平仓亏损门禁记录 `consecutive_loss_limit_reached` / `consecutive_loss_recovered`，AlphaSift 筛选异常记录 `alphasift_screen_failed`，数据质量阻断记录 `data_quality_stale` / `data_quality_unavailable`，账户级风控阻断记录 `cash_low_watermark` / `account_drawdown_limit_reached`，后台自动重试失败/未成交记录 `auto_retry_*`，`vnpy_paper` 提交态订单超时记录 `vnpy_order_timeout`，部分成交等待成交回报超时记录 `vnpy_partial_fill_timeout`，撤单请求超时记录 `vnpy_cancel_timeout`；这些事件使用 `rule_id=null`、`target=vnpy_paper`、`data_source=vnpy_paper_auto`，可通过 `/api/v1/alerts/triggers?target=vnpy_paper` 查询。通知结果会写入 `alert_notifications`，未配置 alert 渠道时会记录 `__no_channel__` 方便排障。
- Web 模拟交易页展示最近 `target=vnpy_paper` 的“自动交易告警历史”，可直接看到数据质量阻断、熔断、自动重试失败和 vn.py 订单超时等系统事件；完整诊断仍以告警中心/API 记录为准。
- Agent run 详情新增 `portfolio_change`，从当前持久化交易计划动态派生本轮实际持仓变化，并进入运行时间线与 JSON 导出。只有存在 Portfolio `trade_id` 的已入账数量才计入买入、卖出、净股数和成交额；`submitted`、尚未本地入账的订单级 `part_filled`、dry-run 和待审批计划分别显示待回报或仅计划。迟到成交、部分成交、审批成交或恢复成交写入账本后，重新读取详情会自动更新，不冻结过时账户快照。模拟交易页和 Agent 控制台展示同一结构。
- 自动模拟交易会根据 AlphaSift 返回的 `quality_status`、`warnings`、`source_errors`、`fallback_used` 和 `stale` 生成基础数据质量诊断；AlphaSift 候选补充阶段还会把行情、资金流和新闻检索分别汇总到 `source_health.candidate_context.quote/fund_flow/news`，记录观测数、成功/部分/失败次数、覆盖率、结果数和错误摘要。三类上下文来源会和 snapshot/daily 一起进入统一质量评分与跨 run 来源趋势。仅 `ok` 或可接受的 `partial` 会继续执行，`stale` 或 `unavailable` 会以 `data_quality_stale` / `data_quality_unavailable` 写入跳过审计，不提交模拟成交；配置最低质量分门禁后，单一上下文来源不可用也会降低本轮分数并可能阻止新增买入。
- 自动交易支持基础卖出风控，默认关闭；开启后会按止损百分比、止盈百分比、移动止损百分比、最大持仓天数、超时未走强或可选策略失效信号触发卖出，并写入候选决策和交易计划审计。持仓天数按完整成交与拆股历史分页读取，并按公司行动先于同日成交的 Portfolio 语义 FIFO 重放当前未平仓批次；清仓后重新建仓会重置持仓时钟，公司行动历史不可用时持仓年龄视为未知，不使用不完整证据触发期限卖出。默认整仓卖出，也可通过 `auto_sell_position_pct` 设置每次卖出的持仓比例，实现按比例分批退出；设置 `auto_no_progress_days` 后，持仓天数达到阈值且浮盈不高于 `auto_no_progress_min_return_pct`（留空按 0%）时会以 `no_progress_timeout` 触发卖出；开启 `auto_signal_exit_enabled` 后，当前持仓命中 active `DecisionSignal` 的 `sell/reduce/avoid` 信号时会以 `strategy_invalidated` 触发卖出。卖出风险处置先于数据质量、账户和市场买入门禁执行，后续买入被拦截时已提交卖单仍保留在订单、计划、决策和 run 计数中。开启 `auto_rebalance_enabled` 后，自动卖出会复用单票、总仓位和行业仓位暴露上限，超限时生成 `rebalance_single_position_value_exceeded`、`rebalance_total_position_value_exceeded`、`rebalance_total_position_pct_exceeded`、`rebalance_industry_position_value_exceeded` 或 `rebalance_industry_position_pct_exceeded` 卖出计划；也可通过 `auto_target_position_weights` 和 `auto_target_industry_weights` 配置股票或行业目标权益占比，超配时生成 `rebalance_target_position_weight_exceeded` 或 `rebalance_target_industry_weight_exceeded` 卖出计划。买入侧会把单笔预算缩减到股票和行业目标的最小剩余缺口，已有持仓可继续补足显式股票目标；换算为交易币种后再按市场整手取可执行金额，不足一手时以 `target_weight_below_min_lot` 跳过。候选原始载荷会记录 `target_weight_sizing`、`rebalance_plan`、目标权重、当前值、剩余缺口和可执行金额。`paper` 模式会直接写入本地模拟卖出；`vnpy_paper` 模式会把卖出委托提交给注入的 vn.py `MainEngine` 并记录为 `submitted`，只有后续成交回报同步后才会写入本地 Portfolio。移动止损会在 `data/vnpy_paper_trading.json` 记录每只持仓的本地峰值价，触发原因是 `trailing_stop_triggered`；未成交的 vn.py 提交态卖出不会提前清理峰值记录。当前不做分批止盈策略；基础跨币种预算与仓位估值已接入 Portfolio 汇率表，任意权重和多约束组合优化仍未完成。
- 同一 Agent run 中，只要某股票的退出卖单已提交或已形成计划，该股票即使再次进入 AlphaSift 候选也会以 `same_run_exit_reentry_blocked` 跳过买入；诊断保留 `same_run_exit_symbols`。下一轮仍会重新评估，避免止损、止盈或持有期限退出后在同一价格立即买回。
- 开启 `auto_score_weighted_allocation_enabled` 后，系统把 `auto_allocation_budget` 视为每轮组合预算；留空时使用 `auto_cash_per_order`。`auto_allocation_method=score_weighted` 按候选评分分配；`score_inverse_volatility_20d` 使用 AlphaSift 同一决策时点的 `volatility_20d_pct`，按 `score / max(volatility, auto_risk_volatility_floor_pct)` 生成风险调整权重。`score_inverse_volatility_20d_correlation_capped` 在此基础上读取运行日及之前的本地 `stock_daily` trailing returns，按候选顺序保留排名更高者，并排除与已保留候选相关系数高于 `auto_max_pairwise_correlation` 的标的。`target_tracking_min_variance_20d` 使用同一时间边界的重叠收益构建年化协方差矩阵，以剩余股票目标缺口（未配置目标时使用候选评分）作为目标向量，通过 `auto_covariance_risk_penalty` 控制目标跟踪与组合方差的权衡。波动率字段缺失、历史收益或重叠样本不足时逐票或整轮 fail-closed，不静默退化。每票仍以 `auto_cash_per_order` 为上限，并同时受现金保留、每日预算/订单槽位、最大持仓数、单票/总仓位/行业空间和显式目标权重约束；候选触顶后，剩余预算会重新分配给仍有空间的候选。Agent run 和候选仓位计划会审计风险模型、目标向量、协方差矩阵、收敛状态、波动率、相关性样本/阈值/冲突标的、约束、分配金额、排除原因和整手残差。该能力默认关闭。
- 每轮 AlphaSift 结果都会生成 `diagnostics.data_quality.score`（0~100）和等级，固定按筛选运行完整性 45%、候选字段/来源覆盖 35%、snapshot/daily 与候选上下文来源健康 20% 汇总；该分数是可解释规则分，不是成功概率或 LLM 置信度。没有来源健康快照时会标记 `unobserved` 并按 70 分计，不当作健康满分。`auto_min_data_quality_score` 默认 60；旧配置缺失、空字符串或 `null` 也会归一为 60，低于阈值的候选轮次会以 `data_quality_score_below_threshold` 阻断所有新买入、记录候选跳过决策并写入自动交易告警。需要只观察分数而不启用软评分门禁时必须显式设为 0。原有 `stale/unavailable` 状态门禁继续始终生效，不能由 0 绕过。
- 候选实时行情复用 `DataFetcherManager` 多源顺序；候选资金流在配置可用 `TUSHARE_TOKEN` 时按 `tushare_ths -> akshare` 使用独立 THS/AkShare 来源，未配置或权限不足时透明回退。两类数据都使用 manager 级恢复状态机：同一市场/provider 连续 3 次请求异常后进入 5 分钟冷却，冷却期自动跳到后续来源，到期只允许一个半开探测；成功后关闭熔断，再次异常则重新冷却。单股票空结果属于不确定结果，不累计整源失败。状态原子写入 `DATABASE_PATH` 同目录的 `provider_source_health.json`，API 重启会恢复 24 小时内的有效失败/熔断状态，不恢复旧的半开探测名额；旧 `realtime_source_health.json` 会自动读取并迁移，损坏或过期文件只告警并从空状态继续。`diagnostics.source_routing.candidate_context.quote/fund_flow` 保存本轮 provider 状态、失败数、冷却剩余和脱敏错误，Agent 控制台在“数据源动态路由”中展示。Tushare THS 返回的万元字段统一换算为 CNY，5 日净额使用接口字段，10 日净额由最近 10 个交易日的单日净额求和。
- 候选级缺失字段会按实际启用的风控分级处置：启用 ST、停牌或涨跌停过滤时，候选若明确标记 `missing_fields` 包含 `trading_status`，会以 `candidate_trading_status_unavailable` fail-closed；配置 `auto_min_turnover` 后无法取得成交额时，会以 `liquidity_data_unavailable` fail-closed，成交额低于阈值仍使用 `liquidity_below_threshold`。名称、行业等非当前硬门禁字段仍进入候选质量分和 Agent 复核警告，不会仅因缺失而一刀切跳过。

## 配置与数据

- 页面交易设置持久化到 `data/vnpy_paper_trading.json`；可选 vn.py runtime 托管通过 `.env` 中的 `VNPY_RUNTIME_*` 配置显式开启，默认关闭。
- 成交、资金流水、持仓快照复用 Portfolio 表；因此模拟成交也会出现在现有持仓页和持仓风险计算中。
- 重置账户会更新 `settings.account_id` 指向新模拟账户，并清空 `auto_trailing_peaks`，避免新账户继承旧持仓的移动止损峰值；不会删除旧 Agent run、候选决策或交易计划审计。
- 可执行 `python scripts/check_vnpy_adapter.py` 验证当前 Python 环境的 vn.py adapter 状态；未安装 vn.py 时脚本输出 `local_paper_fallback` 诊断并以 0 退出，安装 vn.py 后会尝试实例化真实 `OrderRequest`。使用 `--require-vnpy --reconnect-cycles 3` 还会对内置 `DSA_SIM` 执行三轮在途订单断线重连，校验订单缓存保留、恢复后恰好成交一次、订单/成交编号不重复以及资金和持仓连续性。桥接提交可以由宿主进程注入 `MainEngine`，也可以显式设置 `VNPY_RUNTIME_ENABLED=true` 让 DSA 在启动时创建 EventEngine/MainEngine，并按需 add gateway/connect。
- `GET /api/v1/vnpy-paper/gateway/preflight` 与 Web 模拟交易页“生产预检”会针对当前进程已加载的 gateway 重新读取外部连接 JSON，并执行与启动门禁一致的零连接检查。结果包含 gateway 是否注册/外部、配置是否位于仓库内、默认键/已提供键数量、缺失键和 reason code；不会调用 connect、订阅或下单，也不会返回连接文件路径、配置值或读取异常正文。配置通过后仍需重启进程使新 runtime 设置生效，并继续执行真实连接与长跑验收。
- Docker 默认镜像保持轻量本地 paper 能力；自行构建时可用 `--build-arg INCLUDE_VNPY=true` 安装 `requirements-vnpy.txt`，Compose 则使用 `DSA_INCLUDE_VNPY_DOCKER=true`。可选镜像会在构建阶段真实导入 vn.py 核心模块和 `DsaSimulatedGateway`，但不会自动猜测或打包具体券商 gateway。
- `agent_calibration_shadow` 后台采样会按 `trigger_source + market + strategy` 读取最近一次已完成 run，未达到配置间隔时不创建重复样本，并在任务事件中记录 `cadence_skips`、剩余秒数和下一可采样时间。最多 5 分钟的调度宽限用于吸收多市场串行耗时和 scheduler 抖动；手工显式 dry-run 不受该后台节流影响。
- 每次自动模拟交易会写入 `stock_selection_agent_runs`、`stock_selection_agent_decisions` 和 `stock_selection_agent_trade_plans`，保存策略、候选/持仓、买入或卖出动作、评分、跳过原因、计划金额、执行模式、数据质量诊断、结构化 `agent_plan`、结构化 `agent_summary`、候选级 `strategy_evidence`、`position_plan`、规则 `risk_review`、规则 Agent 买入前 `agent_review`、可选 LLM 买入前 `llm_review`、成交 `trade_id` 和候选原始载荷，供页面审计。五个候选审计对象在决策表和详情 API 中都有独立字段，同时保留 `order_result` 内嵌副本兼容旧客户端；成交、撤单或恢复回报替换订单结果时不会覆盖独立审计字段，旧数据库启动时自动补列，旧记录则从原 `order_result` 回退。`strategy_evidence` 使用版本化合同保存本轮实际策略、排名、筛选/最终分数、上游明确提供的逐规则命中项、因子分解、解释和 LLM 覆盖信息；没有逐规则或因子证据时只标记 `summary_only`，不会根据分数反推规则命中。`agent_plan` 会记录规则派生的 `plan_profile`、`execution_policy`、`sizing_plan`、`adaptive_controls` 和可选 `llm_dynamic_plan`，用于说明本轮计划档位、执行路由、预算上限、已启用风控层、LLM 是否调参和降级动作。`agent_summary.review_quality` 会基于规则 Agent 复核、可选 LLM 复核和数据质量生成 `audited`、`guarded`、`needs_review` 或 `idle` 状态、质量分、覆盖率和风险标记，便于判断是否需要人工确认。手动触发 LLM 复盘后，结果会写入 run 诊断中的 `llm_recap`，失败只记录错误原因，不影响交易计划和成交状态。
- 自动 Agent 调用 AlphaSift LLM 重排时默认使用独立的 45 秒单次超时并将结构化结果重试设为 0，可通过 `VNPY_AUTO_ALPHASIFT_LLM_TIMEOUT_SEC` 和 `VNPY_AUTO_ALPHASIFT_LLM_MAX_RETRIES` 调整。模型超时、输出无效或覆盖率不足时，AlphaSift 立即回退确定性的 `screen_score` 排序，不因同一份无效结果再等待一轮；手工选股仍使用 `LLM_TIMEOUT_SEC` / `LLM_MAX_RETRIES` 的常规配置。自动 Agent 默认启用跨 run LLM 熔断：连续失败达到 `VNPY_AUTO_ALPHASIFT_LLM_FAILURE_THRESHOLD`（默认 1）后，在 `VNPY_AUTO_ALPHASIFT_LLM_COOLDOWN_MINUTES`（默认 60）内跳过 LLM；冷却到期只放行一个 `VNPY_AUTO_ALPHASIFT_LLM_PROBE_TIMEOUT_SEC`（默认 45 秒，且不超过正常请求预算）半开探测，成功关闭熔断，失败重新计时。恢复探针与正常请求使用同一默认预算，避免健康渠道和完整结构化输出被更短的人工阈值反复打回熔断。并发 run 在探测的 5 分钟租约内跳过 LLM；探测异常退出且租约过期后按失败重新进入冷却，避免永久锁死。可用 `VNPY_AUTO_ALPHASIFT_LLM_CIRCUIT_BREAKER_ENABLED=false` 关闭该策略。每轮运行在 `diagnostics.alphasift_llm_policy` 和 `diagnostics.alphasift_llm_result` 记录状态、决策、失败原因和恢复时间线。
- AlphaSift 结构化排序经 DashScope OpenAI-compatible 通道调用 Qwen 3 系列时，DSA 的请求级 LiteLLM bridge 默认注入 `extra_body.enable_thinking=false`，避免模型先消耗长思考预算后才生成 JSON。该行为只影响 AlphaSift 候选重排；显式 LiteLLM `model_list` 中的 `extra_body` 优先，可按部署需要重新开启思考模式。
- AlphaSift LLM 重排默认输出上限为 3072 tokens，实测可让当前五候选结构化合同完整闭合；用户显式 `LLM_MAX_TOKENS` 仍优先。1024 tokens 会在首个详细候选对象闭合前触发 `finish_reason=length`，其审计原因表现为 `invalid_structured_output` / `no_json_found`。
- 每轮自动 Agent 运行在 `diagnostics.stage_timings` 持久化单调时钟耗时，分别记录 `planning_seconds`、`preflight_seconds`、`alphasift_screen_seconds`、`candidate_decision_execution_seconds` 和 `total_seconds`。筛选异常会保留到 `alphasift_screen` 阶段，数据质量阻断和正常结束会保留到候选决策/执行阶段；Agent 控制台运行时间线通过 `performance` 事件直接展示总耗时和 AlphaSift 耗时。
- AlphaSift 候选增强会在 DSA 请求级回调中强制执行预排序 3 候选上限，复用预排序阶段已取得的行情构建基础面估值，并对最多 3 个最终候选并发补齐新闻等完整上下文；结果和 warning 仍按原排名汇总，避免第三方默认 5 候选、重复行情请求和逐票串行搜索把自动运行耗时线性放大。
- vn.py 事件同步已提供外部回写 API：订单状态回报会按 `vt_orderid` 更新提交态/失败态审计；成交回报按“交易日 + `vt_tradeid`”幂等写入 Portfolio，同一交易日的重复回报不会重复入账，跨交易日复用成交号仍可正常入账。链路支持一张委托分多笔累计成交，只有累计数量达到计划数量才标记 `filled`，期间保留 `part_filled`、累计数量、加权均价、剩余数量和成交明细。账户和持仓快照会写入 `diagnostics.vnpy_sync_state` 供状态页和后续风控诊断读取；未匹配到计划、重复回报或本地账本校验失败会以明确 reason code 返回，不会静默写入。
- Agent run 详情会从运行状态、结构化 `agent_plan`、结构化 `agent_summary`、结构化 `agent_workflow`、数据质量、候选决策和交易计划合成 `timeline` 时间线；候选决策顶层返回 `strategy_evidence`、`position_plan`、`risk_review`、`agent_review` 与 `llm_review`，并在 `order_result` 保留兼容视图；Web 页面优先读取顶层合同，旧运行记录或旧后端再回退内嵌字段。页面直接展示“Agent 计划”“运行总结”“Agent 工作流”“运行时间线”、候选级“策略证据”和“Agent 复核”，用于解释本轮任务为何按该策略、市场、预算和执行模式运行，以及最终为何成交、计划、警告或跳过。
- 自动交易写入的同日同策略同股票订单带有 `dedup_hash`，避免定时任务重复买入同一个候选；`vnpy_paper` 模式还会检查同股票同方向是否已有 `submitted` / `part_filled` / `cancel_requested` 活跃计划，命中时以 `active_vnpy_order_exists` 跳过，避免在真实成交回报到达前重复提交买入或卖出委托。
- 定时任务注册在 API/Web/Desktop 进程内的 `RuntimeSchedulerService` 后台任务中；即使每日分析定时任务未开启，只要自动模拟交易开启，后台 scheduler loop 也会运行该任务。
- `python main.py --serve-only` / `--webui-only` 只禁用每日分析 daily job，不会禁用自动模拟交易等 runtime scheduler 后台任务；页面保存“定时自动买入”后会触发 reconcile。
- scheduler reconcile 会复用按任务名保存的进程内互斥锁；若重载前一代的同名任务尚未结束，新一代不会并发执行，而是写入 `status=skipped`、`reason=task_already_running` 的持久化事件。状态中的任务级 `overlap_guarded` 表示该保护已启用，`previous_generation_running` 表示当前看到的是重载前任务仍在收尾；异常退出也会释放互斥锁，不影响下一轮调度。
- 状态接口会返回 `scheduler` 状态，包含后台任务名、是否运行、任务级 `next_run_at`、最近错误、最近跳过原因和最近 `task_events`；Web 页面用这些字段展示自动交易后台任务是否已注册、下次触发时间以及“后台任务日志”。
- Web 模拟交易页新增“任务健康检查”，基于 `GET /api/v1/vnpy-paper/task-health` 汇总 `vnpy_paper_auto_trade` 和 `vnpy_paper_auto_retry` 是否注册、是否运行、是否被配置停用、最近事件是否失败/跳过以及下次运行时间；任务健康摘要会优先使用持久化最近事件，便于 API 进程重启后继续判断自动选股和恢复扫描为什么未执行。
- Web 模拟交易页的“后台任务日志”会读取 `GET /api/v1/vnpy-paper/task-events`，支持按任务名和 started/completed/skipped/failed 状态筛选数据库持久化的最近任务事件；API 进程重启后仍可用于定位自动买入、自动恢复扫描或事件监控到底在哪一步被跳过或失败。
- Web 模拟交易页的“任务趋势”会读取 `GET /api/v1/vnpy-paper/task-event-summary`，基于最近持久化任务事件展示 completed/skipped/failed/started 分布、任务级失败率、平均耗时和最近失败/跳过时间，用于判断后台自动化是否持续健康。
- Web 模拟交易页的“长期稳定性”会读取 `GET /api/v1/vnpy-paper/task-metrics`，可切换 7/30/90 天窗口，展示终态运行数、成功/失败/跳过率、平均与 P95 耗时、当前连续失败、任务级明细和逐日趋势。成功率分母只包含 `completed`、`skipped`、`failed` 终态事件，`started` 仅单独计数，避免一次运行被重复计算。
- Agent 控制台会读取 `GET /api/v1/vnpy-paper/agent-runs/data-quality-trends`，按 7/30/90 天窗口展示跨 run 的 `ok`、`partial`、`stale`、`unavailable`、`unknown` 分布、降级率、警告、source error、逐日趋势和 `snapshot/daily + source` 来源健康观测。来源级结果返回观测数、降级次数/比例、最新状态和最新/最大失败计数；旧 run 没有整体质量或来源快照时分别归入 `unknown` 或“无快照”，不会被误算成健康。
- 模拟交易页的 Agent 记录区会读取 `GET /api/v1/vnpy-paper/agent-runs/return-risk-calibration-trends`，按 7/30/90 天及当前策略、市场、状态筛选持久化 `candidate-return-risk-v1` 快照，展示观测覆盖、目标状态、成熟样本、风险效用、应用率和市场/策略/版本分组。统计单位是每次 Agent run 保存的滚动快照；窗口会重叠，因此接口明确不声明独立样本数，旧 run 归入 `unknown`。
- 每轮自动选股会在 `diagnostics.agent_plan.recent_run_context` 保存同触发源、策略和市场最近 5 次 run 的确定性摘要，包括状态/数据质量分布、候选/计划/成交/跳过总数、历史成交率、连续失败、人工验收结论和逐 run 摘要。Agent 控制台可把每轮标记为 `approved`、`needs_changes` 或 `rejected`，并保存审阅人和备注；日总结聚合验收分布。该反馈只作为后续规则计划审计和可选 LLM 动态计划上下文，固定使用 `context_only_never_bypasses_risk_gates` 策略，不会放宽或绕过数据质量、仓位和交易风控。
- Agent 控制台可调用 `POST /api/v1/vnpy-paper/agent-runs/backtest`，按当前策略、市场和运行时间筛选已持久化买入候选，返回 1/5/10/20 个交易日的覆盖率、胜率、平均/中位收益和平均最大有利/不利波动。评价锚点使用候选价格与决策日期，只读取严格晚于决策日期的 `stock_daily`；默认 `refresh_missing=false`，不会联网补数、重跑历史策略、修改 Agent run 或触发交易。缺价和缺少未来日线会进入 `unable_reason_counts` 并降低覆盖率。该结果不包含手续费、滑点、基准超额收益或 point-in-time 全市场策略重放。
- 每轮自动选股还会把同策略、同市场的持久化买入候选汇总为 `diagnostics.cross_run_quality`。状态使用配置的前瞻交易日、成熟样本阈值、最低胜率和有界扫描数，记录覆盖率、胜率、平均/中位收益、平均最大不利波动、逐日收益风险指标、前一状态和状态迁移。版本 `candidate-return-risk-v1` 的效用公式为“平均前瞻收益 - 0.5 × 期限下行偏差 - 0.25 × 平均最大不利波动绝对值”；期限下行偏差由候选逐日收益的下行偏差按前瞻交易日平方根缩放。只有成熟候选在窗口内具有完整有效的逐日收盘价时才生成效用，缺失中间收盘价不会被跨日收益替代，而会进入 `unavailable`。成熟样本效用为负时状态至少为 `guarded`，平均收益也为负时为 `blocked`；证据不足保持 `insufficient_evidence`。`auto_cross_run_quality_gate_enabled` 默认关闭；开启后，组合状态 `blocked` 或本地评价异常会以 `cross_run_quality_gate_blocked` 阻断新增买入并告警，自动止损/止盈等卖出检查仍先执行。门禁关闭时，风险效用只能通过跨市场目标收紧候选数量和单票预算。正式自动交易和只读状态接口只读取本地持久化决策与严格晚于决策日的 `stock_daily`；每日 `agent_calibration_shadow` 会在保存新质量快照前，对已达到最短观察期但缺少日线的股票执行一次按股票去重的有界补数，尚未到期的股票不会联网。补数只写行情缓存，不提交订单，也不让 LLM 决定状态。
- 持久化后台任务事件默认保留 30 天，runtime scheduler 写入新事件后会按 `DSA_RUNTIME_SCHEDULER_TASK_EVENT_RETENTION_DAYS` 和 `DSA_RUNTIME_SCHEDULER_TASK_EVENT_CLEANUP_INTERVAL_SECONDS` 做低频清理；该清理只删除 `runtime_scheduler_task_events` 中的任务日志，不删除 Agent run、交易计划、Portfolio 成交或资金流水。保留天数设为 `0` 时不自动清理，适合由外部归档策略接管的部署。
- 绩效接口会基于本地 paper 账本返回 `daily_returns` 和 `monthly_returns`，按每日/每月最后权益聚合盈亏、收益率、累计收益率、回撤和交易数；Web 模拟交易页展示最近日度收益表和月度收益表。该视图用于 paper 运行复盘，不等同接入完整历史行情后的回测级净值曲线。
- 后台恢复任务会先主动对账提交满 60 秒的 `vnpy_paper` 活跃计划；若 `submitted` 计划超过 30 分钟仍无订单终态、成交回报或 MainEngine 证据，会标记为 `failed` / `vnpy_order_timeout` 并保留原 `vt_orderid` 和 timeout 审计。`cancel_requested` 和 `part_filled` 分别归档为 `vnpy_cancel_timeout`、`vnpy_partial_fill_timeout`，且所有状态不明超时结果都禁止自动重提。撤单请求期间发生的成交以及超时归档后的迟到成交仍可按原 `vt_orderid` 回写并把计划恢复为实际成交状态。
- 状态接口会返回 `diagnostics.vnpy_adapter`、`diagnostics.vnpy_bridge`、`diagnostics.vnpy_event_bridge`、`diagnostics.vnpy_runtime` 和 `diagnostics.trading_window`，用于区分当前是 `local_paper_fallback`、已具备 `vnpy_order_request` adapter 能力、已经具备 `MainEngine` 提交通道、已由 DSA 启动可选 vn.py runtime，还是自动交易正在等待下一个交易窗口；未安装 vn.py 或未配置 `MainEngine` 时仍保持本地 paper 可用。
- Web “自动选股 Agent 记录”区块支持导出当前选中 run 的 JSON 明细，包含 Agent 计划、运行总结、候选决策、交易计划、诊断和成交关联信息，便于复盘或排障。
- Web 新增“Agent 控制台”入口 `/agent-console`，独立展示最近自动选股 run 历史、分页、策略/市场/状态/时间范围筛选、运行详情、时间线、候选决策、交易计划和 JSON 导出；该页面支持 `/agent-console/<run_uid>` 独立详情路由，并兼容 `/agent-console?runUid=<run_uid>` 查询参数深链，复用 `/api/v1/vnpy-paper/agent-runs*` 审计接口，不直接提交订单。
- Web Agent 控制台的候选决策表会展示 `order_result.strategy_evidence` 的策略、证据状态、排名、筛选分、规则命中与因子分解，以及 `order_result.agent_review` 的复核状态和摘要；开启 `auto_llm_review_enabled` 后，也会展示 `order_result.llm_review` 的 LLM 买入复核状态和摘要。今日 Agent 总结会聚合 `agent_review_counts`、`llm_review_counts`、`review_quality_counts`、`review_quality_flag_counts` 和 `review_quality_score_avg`，用于快速识别当天候选是通过、警告、被阻断、LLM 调用失败，还是需要人工复核。
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
- `PUT /settings`：保存页面设置，并触发 runtime scheduler 重新 reconcile 后台任务。可设置 `auto_execution_mode="vnpy_paper"` 和 `vnpy_gateway_name`，用于让自动买入委托走 vn.py `MainEngine` 桥接；可用 `auto_failure_fuse_auto_recovery_enabled` 和 `auto_failure_fuse_cooldown_minutes` 开启熔断冷却后的单轮恢复探测；也可设置默认关闭的 `auto_llm_plan_enabled` 和 `auto_llm_review_enabled`，分别让自动买入在选股前追加 LLM 动态计划、在规则风控通过后追加 LLM 买入复核。
- `POST /failure-fuse/reset`：重置连续失败熔断统计基线，返回轻量状态并更新 `diagnostics.failure_fuse.reset_at`；历史 Agent run、候选决策和交易计划仍保留。
- `POST /orders`：提交一笔模拟委托。默认 `execution_route="local_paper"` 时写入 Portfolio 交易流水；传 `execution_route="vnpy_bridge"` 时会尝试通过注入的 vn.py `MainEngine.send_order` 提交委托，成功后返回 `status="submitted"`、`source="vnpy_main_engine"` 和 `raw.vt_orderid`，不会立即写入本地成交。
- `POST /vnpy-events/trades`：同步一笔 vn.py 成交回报。请求至少包含 `vt_orderid`、`quantity` 和 `price`；后端按 `vt_orderid` 找到提交态交易计划后写入本地 Portfolio 交易流水，并把计划/决策状态回写为 `filled`。
- `POST /vnpy-events/orders`：同步一笔 vn.py 订单状态回报。`rejected`、`cancelled`、`inactive` 等状态会把对应交易计划回写为 `failed`；`parttraded` / `partial_filled` 会回写为 `part_filled` 并记录已成交数量/价格；`submitted`、`alltraded` 等状态只更新提交态审计，不会写入本地 Portfolio 成交。只有 `/vnpy-events/trades` 成交回报会写入本地 Portfolio。
- `POST /vnpy-events/account`：同步 vn.py 账户资金快照到 `diagnostics.vnpy_sync_state.account`，用于区分本地 paper 账本与外部 vn.py 账户状态。
- `POST /vnpy-events/positions`：同步 vn.py 持仓快照到 `diagnostics.vnpy_sync_state.positions`，当前作为诊断快照保存，不直接改写本地 Portfolio 持仓。
- `POST /vnpy-events/attach`：当应用进程已注入 `app.state.vnpy_event_engine` 或 `app.state.vnpy_main_engine.event_engine` 时，注册订单、成交、账户和持仓事件 handler，把 vn.py EventEngine 事件自动转入上述同步入口；重复 attach 会先注销旧 bridge。
- `POST /auto/run`：立即运行一次 AlphaSift 选股驱动的自动模拟买入，响应中包含 `agent_run_uid`。
- `POST /auto/run` 可传可选 JSON 请求体，例如 `{"execution_mode":"dry_run","ignore_auto_trade_enabled":true}`，用于一次性演练自动选股和交易计划生成；也可传 `{"execution_mode":"vnpy_paper"}` 临时把本轮买入委托交给 vn.py bridge。该请求只影响本次运行，审计 diagnostics 会记录临时覆盖参数。
- 每次自动运行会在 LLM 动态计划之前生成 `diagnostics.market_objective` 和 `diagnostics.agent_plan.market_objective`。A 股、港股、美股、日股、韩股和台股使用不同目标画像；只有最近人工意见要求修改/拒绝、连续运行失败或跨运行质量降级时才收紧候选上限与单票预算。该规则只收紧保存配置，不能降低最低分或绕过数据质量、账户、仓位、交易时段及其他风控；收紧后的有效值会同时用于 AlphaSift 筛选、Agent 计划和交易计划。
- `GET /performance?run_limit=50&created_from=2026-07-01T09:00:00&created_to=2026-07-02T15:00:00`：读取本地 paper 绩效摘要，聚合当前模拟账户权益、相对初始资金收益、成交额、买卖次数、FIFO 卖出胜率、权益路径、日度收益、月度收益、最大回撤、换手率、当前仓位、最近 Agent run 状态、成交计划状态、跳过原因、成交股票分布和按策略/行业归因；`created_from` / `created_to` 可选，用于按 Agent run 创建时间筛选窗口级归因。
- `POST /trade-plans/{plan_uid}/approve`：审批一笔 `manual_approval` 模式生成的待执行交易计划，成功后写入本地 paper 成交并回写计划、候选决策和运行统计。
- `POST /trade-plans/{plan_uid}/retry`：重试一笔 `failed` 或可恢复 `skipped` 的 `manual_approval` / `paper` / `vnpy_paper` 交易计划；响应和计划审计中的 `raw.retry` / `order_result.retry` 会记录尝试次数、最大次数、上次原因和下一次可重试时间。
- `POST /trade-plans/{plan_uid}/cancel`：对 `vnpy_paper` 的 `submitted` / `part_filled` 交易计划发起撤单请求；成功后返回 `status="cancel_requested"` 并保留 `raw.cancel.cancel_request_payload`，随后仍需通过 `/vnpy-events/orders` 同步 `cancelled` / `rejected` / `filled` 等终态。
- `GET /trade-plans/recovery-summary?limit=100&include_terminal=false`：读取只读交易计划恢复矩阵；默认只扫描非终态计划，返回状态/执行模式/恢复状态计数、疑似卡住计划数、可撤单数、可重试数和需要关注的计划列表。
- `POST /trade-plans/recovery/run?max_plans=5&scan_limit=200`：手动运行一次受限交易计划恢复扫描；复用后台到期重试逻辑，返回超时归档数、扫描数、尝试数、提交数、跳过数、失败数和消息列表。
- `GET /agent-runs`：读取最近的自动选股 Agent 运行摘要；支持 `limit`、`offset`、`trigger_source`、`strategy`、`market`、`status`、`created_from` 和 `created_to` 过滤；响应包含过滤后的 `total`。
- `GET /agent-runs/daily-summary?date=2026-07-06`：聚合某一天的自动选股 Agent 运行摘要，返回 run 数、候选/计划/成交/跳过计数、状态分布、执行模式分布、数据质量分布、Agent 复核状态、LLM 复核状态、复核质量状态/风险标记/平均分、工作流状态/阶段分布、交易计划状态、主要跳过原因和热门标的；当复核质量出现 `guarded` 或 `needs_review` 时，摘要 `health` 会进入 `warning`；支持 `trigger_source`、`strategy`、`market`、`status` 过滤。
- `GET /agent-runs/data-quality-trends?days=30`：按 1 至 90 天窗口汇总跨 run 的整体筛选数据质量、降级率、警告/source error、逐日趋势和来源级 `source_health_items`；支持 `trigger_source`、`strategy`、`market`、`status` 过滤，最多扫描 5000 条，`truncated=true` 表示结果已截断。
- `GET /agent-runs/return-risk-calibration-trends?days=30`：按 1 至 90 天窗口汇总已持久化的收益/风险目标快照，返回观测率、状态/版本/市场/策略分布、效用范围、成熟样本、逐日数据和市场/策略/版本分组；支持 `trigger_source`、`strategy`、`market`、`status` 过滤。`methodology.overlapping_rolling_samples=true` 表示这些是重叠滚动快照，不能解释为独立回测样本。
- `GET /agent-runs/calibration-evidence`：只读评估 A 股、港股、美股最近 90 天 `agent_calibration_shadow` 已完成 run，返回统一 ready/pending 结论、逐市场运行/观测/成熟样本/观察天数、明确失败项，以及最近校准状态告警的投递与有限重试状态。该接口固定排除普通 Agent run，不创建 run、交易计划或订单；Agent 控制台以桌面表格和移动端逐市场明细展示同一合同。
- `GET /agent-runs/cross-run-quality`：按当前保存的策略、市场和前瞻门禁参数，只读计算当前跨运行前瞻状态、成熟样本、覆盖率、胜率、收益、逐日波动率、下行偏差、最差 20% 日度收益均值、风险效用、最终复核质量、状态迁移与是否会阻断下一轮新增买入。最终复核按“有 LLM 则使用 LLM，否则使用规则 Agent”去重；通过或阻断成熟样本分别达到 `auto_cross_run_min_mature_samples` 后，低于 `auto_cross_run_min_win_rate_pct` 的通过精度或阻断避损率会把组合状态设为 `blocked`。证据不足不阻断；接口不创建 Agent run、不联网补数、不提交订单。
- `POST /agent-runs/backtest`：除候选与策略的 1/5/10/20 交易日前瞻矩阵外，还返回逐日收益/风险指标和 `review_quality_matrix`。前瞻矩阵给出逐日观测数、日均收益、日波动率、下行偏差、期限下行偏差、最差 20% 日度收益均值、版本化风险效用和最大有利/不利波动；复核矩阵按规则 reviewer 或 LLM model、`prompt_version`、`evaluator_version` 分组，分别计算成熟样本覆盖率、通过精度、阻断避损率、通过/阻断平均收益与收益差。`include_skipped=true` 会纳入真实 `action=skip` 风控阻断候选，但仍只读取决策日之后的本地日线。
- `GET /agent-runs/export?limit=50&include_details=true`：导出最近 Agent run；`include_details=true` 时内联候选决策、交易计划和时间线；同样支持 `trigger_source`、`strategy`、`market`、`status`、`created_from` 和 `created_to` 过滤。
- `GET /agent-runs/{run_uid}`：读取单次运行的候选决策、交易计划、风控/跳过原因和模拟成交关联。
- `POST /agent-runs/{run_uid}/llm-recap`：基于该 run 的结构化审计数据生成一次可选 LLM 复盘，并写入 `diagnostics.llm_recap`；请求体支持 `max_output_tokens`（200~2000，默认 800）。LLM 不可用、空响应或调用失败时返回 `accepted=false` 和失败原因，不改变订单、交易计划或自动交易状态。
- `PUT /agent-runs/{run_uid}/feedback`：幂等保存该 run 最新的人工验收结论，`verdict` 取 `approved` / `needs_changes` / `rejected`，可附 `reviewer` 和最多 2000 字备注；返回更新后的完整 run 详情和人工反馈时间线事件。

## 可选 vn.py runtime 配置

默认不创建 vn.py runtime。vn.py 4.4.0 发布元数据覆盖 Python 3.10 至 3.13；建议为该能力单独使用 Python 3.13，避免让系统默认 Python 3.14 环境承担未经上游声明支持的 GUI/数值依赖。Windows PowerShell 可执行：

```powershell
.\scripts\setup_vnpy_runtime.ps1 -PythonExecutable "python"
.\.venv-vnpy\Scripts\python.exe scripts\check_vnpy_adapter.py --require-vnpy --fault-matrix --reconnect-cycles 3
```

安装脚本会校验 Python 版本，安装项目依赖与 `requirements-vnpy.txt`，优先选择 LiteLLM wheel，并把 pip 构建缓存放在 `.venv-vnpy/.pip-cache`。若 AlphaSift 的远程 Git 安装受限，可先准备固定提交的本地源码，再传 `-AlphaSiftSource <repo-relative-path>`；仅诊断 adapter 时可传 `-SkipProjectDependencies`，但该模式不能作为完整 DSA API 运行环境。验收脚本会构造真实 `OrderRequest`，调用测试 MainEngine bridge，启动内置 `DsaSimulatedGateway` 完成标准订单、显式拒单、重复成交回报去重和三轮在途订单重连，再关闭 vn.py EventEngine/MainEngine。

真实 gateway 配置完成后，可在不下单的情况下做长跑连接验收：

```powershell
.\.venv-vnpy\Scripts\python.exe scripts\check_vnpy_gateway_soak.py `
  --duration-seconds 21600 `
  --sample-interval-seconds 5 `
  --startup-grace-seconds 60 `
  --min-connected-ratio 0.995 `
  --require-external-gateway `
  --require-all-default-keys `
  --require-reconnect `
  --require-event account `
  --output-json "$env:TEMP\vnpy-gateway-soak.json"
```

该命令读取现有 `VNPY_*` 环境配置，并默认先在独立子进程运行零连接预检；只有预检通过才会创建 MainEngine 并连接 gateway。`--require-external-gateway` 会拒绝内置 DSA_SIM 和仓库内连接文件，`--require-all-default-keys` 会要求连接 JSON 覆盖 gateway 声明的全部默认键。预检失败的 schema v3 报告固定返回 `connection_attempted=false`。进入长跑后，脚本禁用 DSA 业务事件桥，只注册只读事件计数器，不创建 Agent run、交易计划或订单；观测时长只计算采样窗口，不包含 runtime 关闭耗时。连接率、从未确认连接、运行时不可用、时长未完成及任一 `--require-event` 缺失都会返回非零退出码。`--require-reconnect` 只适合预期验收窗口内会发生重连的场景，会强制要求至少一次重连尝试、至少一次重连成功且结束时保持 connected；未计划故障注入的稳定性长跑可省略。对内置 `DsaSimulatedGateway` 使用 `--simulated-disconnect-at-seconds <秒数>` 时会自动启用相同门禁，并额外要求断线确实已注入。空仓账户不一定产生 position 事件，因此只在明确有持仓时要求 `--require-event position`；order/trade 也只应在独立模拟账户已有外部活动时要求。schema v3 JSON 仅包含脱敏预检结果、gateway 类/名称、聚合连接状态、事件计数和重连统计，不包含连接文件路径或参数内容。

预检默认最多运行 120 秒，可用 `--preflight-timeout-seconds` 在 5 至 600 秒内调整；超时同样按预检失败处理且不会创建 MainEngine。上述脚本会创建独立 MainEngine，适合验证 gateway 插件本身，但不能证明承载 Web/API 的服务进程持续健康；部分真实通道还会限制同账户重复登录。API 已按部署配置启动后，应优先另开终端运行下面的只读主进程验收：

```powershell
python scripts\check_vnpy_deployed_runtime_soak.py `
  --base-url http://127.0.0.1:8000 `
  --duration-seconds 21600 `
  --sample-interval-seconds 5 `
  --min-api-success-ratio 0.995 `
  --min-runtime-ready-ratio 0.995 `
  --min-connected-ratio 0.995 `
  --min-event-bridge-ratio 0.995 `
  --require-external-gateway `
  --expected-gateway-class vnpy_ctp:CtpGateway `
  --expected-gateway-name CTP `
  --require-observed-event account `
  --min-observed-event-count 1 `
  --max-event-handler-failures 0 `
  --max-process-changes 0 `
  --max-gateway-changes 0 `
  --max-reconnect-failure-count 0 `
  --output-json "$env:TEMP\vnpy-deployed-runtime-soak.json"
```

该脚本只调用轻量 `/api/v1/vnpy-paper/status` GET 接口，不修改设置、不触发选股，也不提交或撤销订单。默认要求订单、成交、账户、持仓四类回调在每个成功样本中持续注册，并校验 runtime/MainEngine/EventEngine/gateway 就绪、连接确认、contract 版本、`process_started_at` 和 gateway 身份稳定性，以及重连计数器不回退。生产证据应显式使用 `--require-external-gateway`，并填写实际 gateway 类和名称；该门禁会拒绝 `DsaSimulatedGateway` / `DSA_SIM`，防止把内置模拟链路误报为真实通道。`--require-observed-event` 检查的是验收窗口内的新回调增量，而不是注册状态或启动前累计值；空仓账户可只要求 `account`，有持仓时再要求 `position`，外部确有委托活动时才要求 `order` / `trade`。事件桥只暴露分类计数、处理成功/失败数和最后观测时间，不保存事件载荷。计划在窗口内由外部断网或 broker 模拟故障验证自动恢复时，可加 `--min-reconnect-success-count 1`；容许受控滚动重启或 gateway 切换时才提高相应变化上限。schema v2 JSON 仅记录部署版本、gateway 类/名称和聚合指标，不包含连接文件路径、账户参数或回报内容。

API 与自动任务需要联合长跑时，可另开终端执行只读 scheduler 验收：

```powershell
python scripts\check_vnpy_scheduler_soak.py `
  --base-url http://127.0.0.1:8000 `
  --duration-seconds 21600 `
  --sample-interval-seconds 5 `
  --min-api-success-ratio 0.995 `
  --min-loop-running-ratio 0.995 `
  --min-task-registration-ratio 0.995 `
  --require-task vnpy_paper_auto_trade `
  --require-task vnpy_paper_auto_retry `
  --max-failed-count 0 `
  --max-overlap-skip-count 0 `
  --output-json "$env:TEMP\vnpy-scheduler-soak.json"
```

脚本只调用 `/status` 和 `/task-events` 两个 GET 接口。首次成功读取的事件列表仅作为历史基线，终态、失败和重叠跳过只统计随后新增事件；`--require-task` 同时要求任务在配置比例的成功样本中持续注册，并在本次窗口至少产生一个 `completed`、`skipped` 或 `failed` 终态。验收时长应覆盖自动买入和恢复任务各自至少一个执行周期；若自动买入被关闭，只要求 `vnpy_paper_auto_retry`。连接失败、接口错误、循环退出、任务消失、缺少终态、失败或重叠跳过超过上限均返回非零退出码。该脚本不调用写接口，也不触发选股、计划或订单。

需要验证完整“在线选股 Agent -> 决策/计划 -> vn.py 模拟回报 -> Portfolio”链路时，先运行默认零下单门禁：

```powershell
.\.venv-vnpy\Scripts\python.exe scripts\check_online_agent_vnpy_e2e.py `
  --execution-mode dry_run `
  --min-candidates 1 `
  --output-json "$env:TEMP\online-agent-vnpy-dry-run.json"
```

确认当前 API 使用内置 `DsaSimulatedGateway` 后，才可显式执行模拟委托验收：

```powershell
.\.venv-vnpy\Scripts\python.exe scripts\check_online_agent_vnpy_e2e.py `
  --execution-mode vnpy_paper `
  --allow-simulated-orders `
  --temporarily-disable-time-gate `
  --isolated-account `
  --output-json "$env:TEMP\online-agent-vnpy-fill.json"
```

要验证用户配置的“定时自动选股并直接模拟交易”而不是手动 `/auto/run`，使用隔离调度模式：

```powershell
.\.venv-vnpy\Scripts\python.exe scripts\check_online_agent_vnpy_e2e.py `
  --execution-mode vnpy_paper `
  --trigger-mode scheduler `
  --allow-simulated-orders `
  --isolated-account `
  --timeout-seconds 900 `
  --output-json "$env:TEMP\online-agent-vnpy-scheduler.json"
```

脚本默认要求至少一笔计划经 EventEngine 到达 `filled`，并等待关联决策达到相同终态，再核对决策/计划/Portfolio trade id、run 级持仓变化、账户现金和持仓数量。`scheduler` 模式不会调用手动触发接口；它以启用前 run 集合作为基线，捕获首个新 run 后立即关闭后续周期，并要求持久化后台任务终态事件引用同一 `agent_run_uid`。`--isolated-account` 会先暂停自动买入并记录现有账户集合，通过 `/account/reset` 创建干净账本；完成或失败后恢复原账户及原自动交易/时段门禁/执行模式/间隔设置，并默认把新归档测试账户从历史列表隐藏。即使 reset 已生效但响应丢失，也会通过账户 ID 差分寻找临时账本；任一恢复/清理状态不确定时保持自动买入关闭并非零退出。调试时可加 `--keep-isolated-account-visible` 保留历史入口。

已有持仓导致所有候选按规则跳过时，可显式增加 `--allow-no-fill`，但该结果只证明安全跳过和设置恢复，不能替代成交验收。`vnpy_paper` 模式必须传 `--allow-simulated-orders`，且脚本只允许内置 `DsaSimulatedGateway`；不会对真实券商 gateway 放行订单。`--temporarily-disable-time-gate` 和 `--isolated-account` 只在该模式可用。JSON 证据只保留运行摘要、聚合变化和 runtime 状态，不输出账户连接参数。

runtime 启动前会创建部署工作目录下已忽略的 `.vntrader/`，供 vn.py 保存本地运行状态，避免受限服务账户回退写入用户主目录。需要由 DSA 托管 vn.py EventEngine/MainEngine 时，显式设置：

- `VNPY_RUNTIME_ENABLED=true`：启动时尝试创建 `vnpy.event.EventEngine` 与 `vnpy.trader.engine.MainEngine`。
- `VNPY_GATEWAY_CLASS`：可选 gateway 类导入路径，支持 `module:Class` 或 `module.Class`。
- `VNPY_GATEWAY_NAME`：可选 gateway 名称；若启用 `VNPY_CONNECT_ON_START` 则必填。
- `VNPY_CONNECT_SETTINGS_PATH`：可选连接参数 JSON 文件路径；账号、密码、token 等敏感字段只放在该外部文件中，不写入仓库。
- `VNPY_CONNECT_ON_START=false`：显式改为 `true` 后才会调用 `MainEngine.connect(settings, gateway_name)`。
- `VNPY_PRODUCTION_PREFLIGHT_ENABLED=false`：真实通道部署时显式开启；启动连接和后续自动/手动重连会在调用 `connect()` 前拒绝内置 DSA_SIM、仓库内连接文件、无效 JSON 或缺失 gateway `default_setting` 键。失败不会拖垮 API 启动，`diagnostics.vnpy_runtime.production_preflight` 和 `connect.reason=production_preflight_failed` 提供脱敏原因，自动重连不会反复重试静态错误。
- `VNPY_AUTO_ATTACH_EVENTS=true`：runtime 创建成功后自动注册订单、成交、账户和持仓事件 handler。
- `DSA_RUNTIME_SCHEDULER_TASK_EVENT_RETENTION_DAYS=30`：后台任务事件持久化保留天数；设为 `0` 时不自动清理。
- `DSA_RUNTIME_SCHEDULER_TASK_EVENT_CLEANUP_INTERVAL_SECONDS=3600`：写入后台任务事件后最多每隔多少秒触发一次旧事件清理；设为 `0` 时每次写入后都检查。

安装 vn.py 本身不会让 bridge 变为可提交状态。只做本地模拟时，可使用仓库内置且默认关闭的即时撮合网关：

```dotenv
VNPY_RUNTIME_ENABLED=true
VNPY_GATEWAY_CLASS=src.services.vnpy_simulated_gateway:DsaSimulatedGateway
VNPY_GATEWAY_NAME=DSA_SIM
VNPY_CONNECT_ON_START=true
VNPY_PRODUCTION_PREFLIGHT_ENABLED=false
VNPY_AUTO_RECONNECT_ENABLED=true
VNPY_AUTO_RECONNECT_INTERVAL_SECONDS=60
VNPY_AUTO_RECONNECT_MAX_INTERVAL_SECONDS=300
VNPY_AUTO_RECONNECT_CONFIRMATION_GRACE_SECONDS=30
VNPY_AUTO_ATTACH_EVENTS=true
```

内置网关无需 `VNPY_CONNECT_SETTINGS_PATH`，并且必须保持 `VNPY_PRODUCTION_PREFLIGHT_ENABLED=false`。API 重启后，模拟交易设置会在未保存 gateway 名称时继承 `VNPY_GATEWAY_NAME`；把执行模式设为 `vnpy_paper` 后即可开启定时自动买入或执行手动委托。合法限价单默认延迟 500 毫秒全量成交，订单、成交、账户和持仓均通过真实 vn.py EventEngine 回到 DSA；DSA Portfolio 仍是跨进程持久化账本。

内置网关只用于功能验收和本地模拟，不提供真实行情撮合、手续费、滑点、部分成交概率或网关状态持久化；进程重启会重置网关内存账户，但不会删除 DSA Portfolio 流水。接真实通道时仍需安装具体 gateway 插件，把敏感连接参数放入外部 JSON，并显式配置 `VNPY_CONNECT_SETTINGS_PATH`。未配置 gateway 时，runtime 和事件引擎可以为可用状态，但 `diagnostics.vnpy_bridge.available=false` 且 reason 为 `gateway_name_not_configured`，这是预期的安全状态。

### 连接前 gateway 预检

在允许任何连接前，可先验证 gateway 类是否可加载、注册名称是否正确，以及外部连接 JSON 的键名是否覆盖 gateway 的 `default_setting`：

```powershell
.\.venv-vnpy\Scripts\python.exe scripts\check_vnpy_gateway_preflight.py `
  --gateway-class src.services.vnpy_simulated_gateway:DsaSimulatedGateway `
  --gateway-name DSA_SIM
```

真实通道的部署门禁应增加 `--require-external-gateway --require-all-default-keys`，并通过 `--settings-path` 指向仓库之外的连接 JSON。生产门禁会拒绝 `DSA_SIM`，也会拒绝位于仓库内的连接文件。输出只包含键名、计数和错误类型，不包含文件路径或字段值。该脚本强制关闭启动连接、事件自动挂接和自动重连，不订阅行情、不调用 gateway 网络接口、不创建订单；通过预检只证明插件和静态配置契约可用，真实连接、回报及长跑仍须使用 soak 工具单独验收。

## vn.py bridge 边界

- `vnpy_paper` 模式负责把 DSA 通过风控后的买入计划和自动卖出计划转换为 vn.py `OrderRequest` 并调用 `MainEngine.send_order`，也可把提交态计划转换为 vn.py `CancelRequest` 并调用 `MainEngine.cancel_order` 发起撤单；订单状态、成交、账户和持仓回报可通过 `/vnpy-events/*` 同步回 DSA，或在宿主进程注入 EventEngine 后通过 `/vnpy-events/attach` 注册自动回调。DSA 也可以在 `VNPY_RUNTIME_ENABLED=true` 时创建 EventEngine/MainEngine 并按配置 add gateway/connect；连接请求与确认状态分开审计，已知断开状态会阻止新增委托。内置 `DsaSimulatedGateway` 默认在重连时保留资金、持仓、订单和计数器，恢复未完成延迟成交且保证单次成交；每个新进程实例生成独立会话前缀，显式设置 `preserve_state_on_reconnect=false` 重置时也会更换前缀，避免订单号和成交号跨会话复用。该模拟网关已完成事件回写与循环重连验收；真实券商 gateway 的连接参数、重连和长跑仍需部署方显式验证。
- vn.py 4.4 原生 `Status` 的中文值（提交中、未成交、部分成交、全部成交、已撤销、拒单）会统一映射到 DSA 状态。新计划写入 `vt_orderid` 后立即对账一次 MainEngine 终态，修复同步拒单/成交事件早于计划落库的竞态；查询不到终态或查询异常时保持提交态，由正常 EventEngine 回调和既有恢复扫描继续处理。
- `DsaSimulatedGateway` 的连接 JSON 可显式设置 `reject_every_nth_order`（默认 `0`，不注入拒单）和 `duplicate_trade_event_count`（默认 `1`，范围 1-5）做故障验收；生产模拟运行应保持默认值。`--fault-matrix` 会临时启用并在完成后恢复默认值。
- Agent 完成一轮候选处理后会从持久化交易计划重算运行级提交/跳过计数，避免同步拒单先回写后被初始“已接受委托”计数覆盖。并发到达的同交易日相同 `vt_tradeid` 由 Portfolio 唯一约束兜底去重，后到回报按成功幂等 no-op 返回，不会新增流水或把对账误报为失败；相同成交号出现在不同交易日时视为不同成交。
- 自动候选未提供有效价格时会先按股票独立请求实时行情；行情仍不可用或请求异常时，该候选以 `price_unavailable` 跳过并在 `raw.price_resolution` 记录请求价格和解析来源，同时保留候选上下文及预算。该失败不会向 gateway 下单，也不会阻断同轮其他候选。
- bridge 提交成功只代表 vn.py 接收了委托请求，交易计划状态会记录为 `submitted`；订单状态中的部分成交会记录为 `part_filled` 供审计；只有收到并同步成交回报后，本地 Portfolio 才会写入成交并更新现金和持仓。
- bridge 不可用、gateway 未配置、`OrderRequest` 构造失败或 `send_order` 抛错时，自动交易会把该计划记录为 `failed` 或 `skipped`，不会让整轮 Agent 运行变成 500。

## 回滚

关闭页面中的“定时自动买入”即可停止新增后台自动订单；关闭“启用模拟交易”会阻止手动和自动模拟成交。误点“重置账户”时，旧账户只是被归档，必要时可在 Portfolio 账本中重新启用或按账本纠错流程处理。若需要彻底回滚代码能力，移除 `vnpy-paper` API、Web `/paper-trading` 与 `/agent-console` 页面和 `RuntimeSchedulerService` 中的后台任务注册即可。已有 Portfolio 模拟流水不会自动删除，可在持仓页按账本纠错流程删除。
