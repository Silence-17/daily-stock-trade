# 在线选股 Agent 与自动模拟交易目标拆解

本文档记录“在线选股 Agent + 自动化选股 + 自动模拟交易”的目标状态、当前完成度和待完成事项。

截至 2026-07-20，本系统已经具备 AlphaSift 在线选股、本地模拟交易账本、Web 入口、定时自动买入、可交易窗口诊断、跨模块系统健康视图、关键异常 alert 路由通知、vn.py 提交态审计、内置即时撮合 gateway、成交回报同步入口、规则 Agent 买入前二次复核、默认关闭的 LLM 动态计划、默认关闭的 LLM 买入前复核、基础复核质量摘要、手动可选 LLM 复盘、Agent run 人工验收反馈，以及可分别验证手动触发和后台调度触发的在线 Agent -> DSA_SIM 验收门禁。2026-07-20 的隔离调度 run 已证明定时任务能自动完成 3 候选、3 提交、3 成交并恢复原账户与设置；尚未达到的目标只包括真实 gateway 长跑与外部全市场长期验收。

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
| 在线选股能力 | AlphaSift 可用，策略接口返回 8 个策略，候选级数据质量/缺失字段/来源已有基础标注，Web 可查看 source health，自动 Agent 已按历史/当前健康动态调整 snapshot 来源顺序，实时行情、资金流和新闻具备 provider 级故障切换；行情与资金流健康可跨 API 重启延续 | 约 93% | 仍需补外部权限下的全市场长任务稳定性证据 |
| 选股 Agent | 已有自动选股运行记录、结构化 Agent 计划、跨市场动态目标、规则派生计划档位/执行路由/预算上限/降级动作、默认关闭的 LLM 动态计划、每轮运行总结、基础复核质量摘要、基础 Agent 工作流状态机、严格样本外跨运行质量状态与可选买入门禁、版本化逐日收益/风险效用目标、7/30/90 天持久化校准趋势、按日结构化运行总结、候选级仓位计划/规则风控复核、规则 Agent/LLM 模型与版本的 1/5/10/20 日复核质量矩阵、最终复核成熟质量自动校准/门禁、默认关闭的 LLM 买入前复核、手动可选 LLM 复盘、run 级人工验收及后续上下文复用、候选决策审计和交易计划审计，但还不是完整 Agent 工作流 | 约 99% | 校准采集与观察能力已完成，仍缺生产样本和不同市场长期验证证据 |
| 自动模拟交易 | 已能从 AlphaSift 候选生成交易计划，支持 dry-run、手动审批、按交易时段写入本地 paper 订单，并支持可交易窗口诊断、首次自动买入调度对齐下一交易窗口、基础止损/止盈/移动止损/持仓天数卖出、超时未走强卖出、按比例分批卖出、active 防守决策信号触发的策略失效卖出、单票/组合/行业仓位暴露上限、跨币种预算、按股票/行业目标缺口缩量补仓、候选评分加权、20 日逆波动率、两两相关性上限或目标缺口/协方差优化分配和基础组合再平衡、股票黑名单、候选级/账户级/市场级基础风控 | 约 99% | 仍缺真实 gateway 长跑验证 |
| Web 操作页 | 已有选股页、模拟交易页和独立自动选股 Agent 控制台，支持暂停确认、一键恢复、立即 dry-run 演练、后台调度状态、持久化可筛选后台任务日志、任务趋势摘要、7/30/90 天后台任务长期指标、任务健康检查、模拟账户历史筛选/恢复/切换/隐藏式批量清理、权益曲线、日度收益表、月度收益表、按 Agent run 创建时间筛选的窗口级绩效矩阵、已记录候选的 1/5/10/20 日样本外前瞻评价、时间点因子采集/覆盖检查/单日重放、逐日期历史全市场分批恢复、进程重启孤儿租约诊断与安全接管、现金/整手/等权或显式股票目标权重组合回测、自动或显式分红/拆并股、显式零碎股现金补偿、固定或 2005 年以来 A 股历史双边/单边印花税、版本化常规/低佣/零成本档位、自定义实际账户费率与成本审计、可交易窗口、风控统计、复核质量摘要、运行时间线、分页历史、策略/市场/状态/时间范围筛选、`/agent-console/<runUid>` 独立详情路由、查询参数兼容深链和单次/最近 run JSON 导出 | 约 99% | 仍缺外部权限下的全市场与公司行动长任务证据 |
| vn.py 集成 | 已有可选 `OrderRequest` / `CancelRequest` 映射、`MainEngine.send_order` / `cancel_order` 桥接入口、订单/成交/账户/持仓回写 API、注入式 EventEngine attach、opt-in runtime bootstrap、内置即时撮合 gateway，以及 Python 3.13 隔离环境中的真实 vn.py 4.4.0 Agent 计划到 Portfolio 成交验收 | 约 90% | 真实 gateway 插件/账户连接、回报、重连和长期订阅稳定性仍未验证 |
| 稳定性与可观测 | 有状态接口、错误提示、跨模块 `system_health` 健康视图、AlphaSift source health 页面视图、候选级数据质量标注、自动交易数据质量诊断、可交易窗口诊断、读取持久化最近事件的后台任务健康检查、持久化可筛选后台任务日志、任务趋势摘要、7/30/90 天终态运行长期指标、7/30/90 天跨 run 数据质量趋势、7/30/90 天收益/风险校准趋势、snapshot 动态来源权重与审计、实时行情/资金流 provider 熔断/冷却/半开恢复、跨 API 重启健康延续与 Agent 审计、任务事件保留/低频清理策略、Agent run 时间线、基础失败重试、交易计划恢复矩阵、手动恢复扫描、MainEngine 主动订单对账、暂停买入时独立恢复、多笔成交幂等累计、撤单竞态/迟到成交恢复、对账异常保护、熔断状态/手动恢复/持久化冷却自动恢复、自动交易系统事件告警历史和 alert 路由通知尝试审计 | 约 99% | 缺少真实 gateway 长跑恢复验收 |

综合判断：

- 本地 paper 的在线选股、自动计划、风控、审计和恢复链路已达到可用 MVP；生产验收仍取决于真实数据与运行窗口。
- 2026-07-17 运行态复验使用 build `strict-market-timestamps-20260717`：严格 provider 时间戳配置回显为开启，`DSA_SIM` 连接已确认，订单/成交/账户/持仓 4 类 EventEngine 回调已注册，系统健康无必需阻断项；当前 warning 仅表示 A 股交易窗口尚未开放。本次未运行 Agent、未创建计划、未提交订单。
- 长期稳定版仍缺真实 gateway 长跑、外部权限下的全市场与公司行动长任务证据，以及生产样本下多模型/多市场长期校准；2005 年以来历史税制、版本化参考成本档位、自定义实际账户费率、Agent run 人工验收、历史意见上下文复用、版本化收益/风险目标和风险门禁隔离已完成。
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
- 完整状态快照会把估值覆盖率、新鲜率、降级状态和 provider 使用量按 15 分钟桶持久化，保留 120 天；`diagnostics.system_health` 与 Web 模拟交易页展示 7/30/90 天有效/总观测数、时间平均/最低及持仓观测加权覆盖率与新鲜率、降级占比和 provider 份额。空仓单独计数且不进入健康分母，轻量状态只读取历史，不伪造当前观测。
- `GET /api/v1/vnpy-paper/status` 已新增 `diagnostics.alphasift`，readiness 会展示 AlphaSift 选股依赖是否启用、可用、版本和策略数量，便于解释自动交易是否具备候选来源。

未完成：

- 状态接口已有轻量查询、完整快照短 TTL 缓存、页面级估值降级提示、当前价格源健康指标和 7/30/90 天持久化趋势；仍需外部环境长跑样本与恢复证据。
- 页面级“为什么不可用”已有后端 readiness 诊断摘要、跨模块 `system_health` 健康视图、API/contract/build/Python/进程启动信息、旧后端 contract 兼容提示、AlphaSift 依赖状态、状态加载失败分类和持仓估值降级提示；后台任务已有 7/30/90 天长期指标，Agent run 已有 7/30/90 天整体数据质量与 provider 明细趋势，snapshot、资金流和新闻已消费 30 天趋势形成请求级权重，仍需补真实环境长期样本和更完整的自动恢复策略。

需要做：

- `GET /vnpy-paper/status` 已通过 `include_snapshot` / `include_recent_trades` 提供轻量状态与完整快照两个层级，Web 首屏先请求轻量合同、再后台刷新持仓和成交；2026-07-17 本地连续五次轻量请求为 24~78 ms。
- 持仓估值短 TTL 缓存、价格源失败页面级降级说明、结构化当前指标和 7/30/90 天跨时间趋势已完成；继续积累真实长跑样本。
- Web 页面已展示后端 API/contract/build 兼容状态、本地账本、选股来源、自动化调度、调度窗口、交易窗口、持仓估值覆盖/新鲜率/来源分布及其 7/30/90 天趋势、AlphaSift 依赖、熔断、vn.py bridge 可用性摘要、后台任务长期指标和跨 run 数据质量趋势，并对旧后端、状态路由 404、状态超时和本地连接失败提供明确提示；继续补真实 gateway 恢复证据。
- API 超时和错误码测试已覆盖路由 404、状态超时、价格源缺失页面提示、估值历史存储异常脱敏降级，以及轻量状态只读趋势不写观测；继续积累外部价格源长跑异常样本。

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
- 自动 Agent 的 AlphaSift LLM 重排已和手工选股拆分运行预算：自动运行默认单次最多等待 45 秒、不重试无效结构化结果，可通过专用环境变量覆盖；失败立即回退 `screen_score`。跨 run 熔断默认在一次失败后跳过后续 LLM，冷却 60 分钟后只允许一个 10 秒半开探测，成功恢复、失败重新冷却；策略与结果写入 `diagnostics.alphasift_llm_policy` / `alphasift_llm_result` 和运行时间线。真实 gateway 长跑仍需继续验证端到端耗时分布。
- 自动 Agent 已持久化计划、前置门禁、AlphaSift 筛选、候选决策/执行和总耗时，成功、数据质量阻断与筛选异常路径都能在 Agent 控制台 `performance` 时间线事件中定位耗时；后续生产样本可据此形成阶段耗时基线和长期告警阈值。
- AlphaSift 预排序上下文已在请求级回调中强制执行声明的 3 候选上限，并复用同票实时行情构建基础面估值；最终 3 个候选的完整新闻/基础面增强改为有界并发并按排名稳定汇总，消除真实 dry-run 中确认的第三方默认 5 候选、重复行情和串行新闻等待。
- Python 3.13.14 真实服务的三轮同条件 dry-run 均为 3 候选、0 订单：总耗时从 135.36 秒降至 91.91 秒，AlphaSift 从 127.91 秒降至 83.99 秒；最终日志在 snapshot 与 LLM marker 之间严格只有 3 次候选行情调用。45 秒 LLM 超时已接入跨 run 健康熔断和单探测恢复，后续生产样本用于校准失败阈值与冷却时长。
- 自动模拟交易会写入 `stock_selection_agent_runs`，记录 run id、触发来源、策略、市场、参数、候选数、成交数、跳过数和诊断。
- 自动模拟交易 run 诊断新增 `agent_plan`，记录策略、市场、候选数量、每票预算、执行模式、规则派生 `plan_profile`、`execution_policy`、`sizing_plan`、`adaptive_controls`、仓位计划模板、风控预算、候选过滤器、gate 和预期产物。
- 自动模拟交易设置新增默认关闭的 `auto_llm_plan_enabled`：开启后会在调用 AlphaSift 前生成本轮 `llm_dynamic_plan`，允许 LLM 在已知策略白名单内选择策略，并只在已保存上限内收紧候选数、每票预算和最低分；失败时回退保存配置并落审计。
- 自动模拟交易会写入 `stock_selection_agent_decisions`，记录候选代码、名称、评分、理由、风险/跳过原因、计划金额、成交数量、成交价和关联 `trade_id`；候选 `order_result` 内含版本化 `strategy_evidence`、`position_plan`、`risk_review` 和 `agent_review`。`strategy_evidence` 保存实际上游提供的规则命中、因子分解、筛选/最终分数和解释，证据不足时明确标记 `summary_only` 而不反推命中。
- 自动模拟交易会写入 `stock_selection_agent_trade_plans`，记录计划金额、执行模式、计划状态、成交回报和跳过原因；交易计划 `order_result` 同步保存候选级仓位计划、规则风控复核、规则 Agent 买入前二次复核和可选 LLM 买入前复核。
- 自动模拟交易设置新增默认关闭的 `auto_llm_review_enabled`：开启后在规则风控通过后调用 LLM 生成 `order_result.llm_review`；`blocked`、模型不可用、JSON 解析失败或调用异常都会 fail-closed 跳过候选，不会继续生成或提交买入计划。
- 自动模拟交易 run 诊断新增 `agent_summary`，聚合本轮结果、候选/计划/成交/跳过计数、数据质量、主要跳过原因、风控复核统计、Agent 复核状态统计和 LLM 复核状态统计。
- `agent_summary.review_quality` 已能基于规则 Agent 复核、可选 LLM 复核和数据质量输出 `audited`、`guarded`、`needs_review` 或 `idle` 状态、质量分、覆盖率、风险标记和是否建议人工确认。
- Agent run 详情会派生 `diagnostics.agent_workflow`，按计划、数据质量、候选复核、交易计划和执行阶段生成状态机摘要，记录当前阶段、整体状态和建议下一步。
- 每轮规则 Agent 计划会持久化 `recent_run_context`，汇总同策略/市场最近 5 次 run 的状态、数据质量、候选/计划/成交/跳过、成交率和连续失败；默认关闭的 LLM 动态计划复用同一上下文，Agent 控制台展示最近运行状态。
- 每轮运行会基于同策略/市场已持久化候选的严格样本外结果生成 `cross_run_quality`，记录成熟样本、覆盖率、胜率、收益、前一状态和状态迁移；默认关闭的前瞻门禁可在成熟胜率不足或评价不可用时阻断新增买入，证据不足不阻断且卖出风险处置继续运行。
- Web 模拟交易页的 Agent run 详情展示“Agent 计划”和“运行总结”摘要，并在运行时间线中包含 `agent_plan` 与 `agent_summary` 阶段。
- Agent 控制台新增“今日 Agent 总结”，通过 `/api/v1/vnpy-paper/agent-runs/daily-summary` 聚合 run 数、候选/计划/成交/跳过计数、状态分布、执行模式、数据质量、Agent 复核状态、LLM 复核状态、复核质量状态/风险标记/平均分、主要跳过原因和热门标的。
- Agent 控制台新增 run 级人工验收，可幂等保存 `approved`、`needs_changes` 或 `rejected`、审阅人和备注；详情、列表、时间线与今日总结统一展示，最近 5 次验收进入后续 `recent_run_context`，但不会绕过任何现有风控。
- 自动运行新增确定性的 `market_objective`：按 A 股、港股、美股、日股、韩股和台股生成市场目标画像；当最近人工意见要求修改/拒绝、出现连续失败或跨运行质量降级时，结合市场风险系数只收紧候选数量和单票预算。收紧后的同一份有效配置会进入 AlphaSift 筛选、Agent 计划和交易计划，LLM 动态计划只能在该边界内继续收紧；Agent 控制台展示配置值、有效值和触发原因。
- Agent 前瞻评价新增 `review_quality_matrix`，把规则 `agent_review` 和 LLM `llm_review` 按 reviewer/model、prompt/evaluator 版本分组，并在 1/5/10/20 日成熟后向样本上计算通过精度、阻断避损率、平均收益差和覆盖率；真实 `action=skip` 风控候选已纳入可选样本。
- 跨运行质量新增最终复核策略：每个候选按 LLM 优先、规则 Agent 兜底选取一次最终裁决，成熟后用通过精度、阻断避损率、通过收益和收益差生成 `review_quality_state`；它与候选整体质量取更严格状态，接入现有可选买入门禁和跨市场只收紧目标，证据不足不阻断。

未完成：

- Agent 计划阶段已有基础诊断、跨市场动态目标、规则派生计划档位/降级动作、最近运行与人工验收上下文、默认关闭的 LLM 动态计划、基础复核质量摘要、按规则/LLM 模型版本分组的长周期复核质量矩阵、成熟质量自动校准/门禁、版本化逐日收益/风险目标、基础可解释状态机和跨运行前瞻质量迁移；仍缺生产样本长期校准和不同市场长期验证证据。
- 候选级结构化解释已具备独立持久化/API 合同：`strategy_evidence`、`position_plan`、规则 `risk_review`、规则 Agent `agent_review` 与可选 LLM `llm_review` 分列保存，订单生命周期更新不会覆盖解释；旧库自动迁移，旧记录兼容回退。
- LLM 动态计划和买入前二次复核已有默认关闭路径，并记录基础 `prompt_version` / `evaluator_version`；基础复核质量摘要、按模型/版本分组的 1/5/10/20 日质量矩阵和人工验收闭环已能标记风险并反馈到后续上下文，仍缺生产规模下的多模型长期校准证据。
- 每轮运行总结已有基础结构化 `agent_summary`，跨 run 的每日结构化总结和最近 5 次运行上下文已落地；手动可选 LLM 复盘已能基于结构化审计数据写入 `diagnostics.llm_recap`，但尚未进入自动交易决策闭环。

需要做：

- 扩展 `stock_selection_agent_runs`，Agent 计划阶段、跨市场动态目标、规则派生计划档位/降级动作、默认关闭的 LLM 动态计划、每轮运行总结、每日结构化总结、规则 Agent 复核统计、LLM 买入前复核统计、基础复核质量摘要、版本化收益/风险目标、默认关闭的 LLM 买入前复核和手动 LLM 复盘已有基础诊断；继续积累生产样本的长期校准与多市场验证证据。
- 候选决策 schema 已将 `strategy_evidence`、`position_plan`、规则 `risk_review`、规则 Agent `agent_review` 与可选 LLM `llm_review` 升级为独立字段，并保留 `order_result` 兼容视图；LLM 动态计划和买入复核已记录基础提示词/评估版本，run 级人工验收已有独立表级合同。候选级策略命中、因子分解和证据完整性状态已落地，继续积累多模型质量合同的生产样本。
- “拉候选 -> 补上下文 -> LLM/规则复核 -> 风控 -> 生成交易计划”工作流、Agent 解释/本轮计划视图、任务历史列表和独立详情页均已完成；继续积累生产样本、多模型和不同市场长期校准证据。

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
- 自动交易支持账户级基础风控，能以 `cash_low_watermark`、`account_drawdown_limit_reached`、`consecutive_loss_limit_reached` 跳过现金低水位、最大回撤超限和连续已平仓亏损冷却期内的买入候选，并写入告警中心系统事件、复用 alert 路由外发通知。
- 自动交易支持可选大盘红绿灯风控，能以 `market_light_red` / `market_light_yellow` 在最近大盘复盘快照为红灯或黄灯时跳过买入候选。
- 自动交易支持默认关闭的独立市场宽度和热点退潮门禁：前者检查最近持久化快照的 breadth 分数，后者比较最近两次快照的 limit 强度回落；任一市场门禁启用后会校验最新快照日期，默认只接受 7 个自然日内的快照，缺失、非法、未来或超期证据均 fail-closed，只阻断新增买入并保留完整诊断。
- 自动交易支持可选连续失败熔断，能以 `failure_fuse_open` 在同策略/同市场连续失败达到阈值后跳过自动买入；状态接口和 Web 页面已展示熔断是否打开、阈值和连续失败数，并提供手动恢复熔断基线入口；熔断打开会写入告警中心 `target=vnpy_paper` 的系统触发历史，并复用 alert 路由外发通知、记录通知尝试。
- 连续失败熔断保持锁存语义，并新增默认关闭的持久化冷却自动恢复：冷却到期只放行一轮恢复探测，再次失败会重新熔断；手动恢复入口继续保留，状态和告警审计会展示恢复时间与自动恢复事件。
- 自动交易支持基础卖出风控，默认关闭；开启后在 `paper` 模式下按 `stop_loss_triggered`、`take_profit_triggered`、`trailing_stop_triggered`、`max_holding_days_reached`、`no_progress_timeout` 和 `strategy_invalidated` 写入本地模拟卖出和 Agent 审计，在 `vnpy_paper` 模式下会把卖出提交给 vn.py bridge 并等待成交回报入账；默认整仓退出，也可通过 `auto_sell_position_pct` 按持仓比例分批卖出，设置 `auto_no_progress_days` 后可按超时未走强退出，开启 `auto_signal_exit_enabled` 后会消费 active `sell/reduce/avoid` 决策信号触发策略失效卖出。
- 持仓天数已按每页最多 100 条的服务契约读取完整成交与拆股历史，并按公司行动先于同日成交的顺序 FIFO 重放当前未平仓批次；清仓重建仓会重置计时，公司行动历史不可用时不使用不完整证据触发期限卖出。数据质量、账户和市场买入门禁不会吞掉此前已执行的止损卖单，run 的提交/跳过计数与计划、决策终态保持一致。
- 模拟交易页支持“立即 dry-run”一次性演练，可不保存配置、不开启后台自动交易，临时生成自动选股交易计划和审计记录。
- 交易计划支持基础失败恢复，`manual_approval`、`paper`、`vnpy_paper` 计划若变为 `failed` 或可恢复的 `skipped`，可在页面重试提交并回写交易计划、候选决策和 run 计数。
- vn.py 订单状态回写支持部分成交审计和多笔成交累计：`parttraded` / `partial_filled` 会标记 `part_filled`，成交回报按 `vt_tradeid` 幂等写入 Portfolio 并累计数量、加权均价和剩余数量，达到计划数量后才标记 `filled`。超时恢复会先查询 `MainEngine.get_order` / `get_all_trades` 补同步漏失回报；查询异常会保护活跃计划，所有订单/部分成交/撤单超时结果均禁止自动重下单。
- `serve-only` / `webui-only` 启动现在只禁用每日分析 daily job，不再压制自动模拟交易后台任务；自动买入开启后可在 Web/API 长运行进程中注册 `vnpy_paper_auto_trade` 和 `vnpy_paper_auto_retry`。
- Runtime scheduler reconcile 已按任务名复用进程内互斥锁；重载前同名任务未结束时，新代任务以 `task_already_running` 审计跳过，状态与 Web 可识别旧代任务仍在收尾，异常路径也会释放互斥锁。

未完成：

- 自动交易已有基础止损、止盈、移动止损、持仓天数卖出、超时未走强卖出、按比例分批卖出、active 防守决策信号触发的策略失效卖出，以及基于候选评分、同一决策时点 20 日逆波动率、本地 trailing returns 顺序两两相关性上限或目标缺口/协方差目标函数的约束分配、缺口补仓与卖出侧组合再平衡；真实 gateway 长跑验收仍未完成。
- 交易计划表已有基础状态、手动审批成交、受控计划重试、retry 次数/冷却审计、vn.py 多笔成交累计、MainEngine 超时对账、对账异常保护、提交态/部分成交/撤单请求安全归档、主动撤单、活跃提交态防重复、只读恢复矩阵和手动恢复扫描，但还缺完整订单生命周期的真实 gateway 长跑验证。
- 自动失败恢复已有页面/API 受控重试、后台到期重试调度、页面手动恢复扫描、MainEngine 漏回报对账、状态不明 fail-closed、数据质量阻断告警历史、熔断状态诊断、交易计划恢复矩阵、告警中心系统事件历史和 alert 路由通知尝试审计；剩余重点是真实 gateway 重连、缓存保留和迟到回报长跑验收。

需要做：

- 扩展交易计划层：已具备 `planned`、`submitted`、`part_filled`、`cancel_requested`、`filled`、`skipped`、`failed`、受控重试、后台到期重试、页面手动恢复扫描、提交态/部分成交/撤单请求超时归档、主动撤单、活跃提交态防重复、只读恢复矩阵、关键异常告警历史和 alert 路由通知基础语义；继续补真实 gateway 长跑验证和完整自动恢复策略。
- 已支持 dry-run、手动审批、受控计划重试和自动成交；继续补完整订单生命周期。
- 扩展交易规则：基础止损、止盈、移动止损、持仓天数、超时未走强、按比例分批卖出、策略失效卖出、评分加权/20 日逆波动率/相关性上限/目标缺口协方差优化、暴露上限驱动和目标权重驱动的缺口补仓/基础组合再平衡，以及跨币种估值和预算风控已落地；继续补真实 gateway 长跑验证。
- 已把下次可交易窗口和调度器下次运行时间通过 readiness 关联起来；持久化 `last_auto_run` 的 run id、时间、原始跳过/失败原因和计数也已接入 readiness、统一系统健康与 Web 可用性诊断，不依赖 task event 保留期。
- Web 已展示每次自动交易的成交、跳过原因、本轮实际持仓变化并支持单次与批量 JSON 导出；继续积累真实环境长期审计样本。

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
- 连续亏损已使用 paper 账本 FIFO 已实现盈亏独立统计，可配置亏损笔数上限和冷却时间；达到上限仅暂停新增买入，冷却到期或盈利/持平平仓后恢复，并写入开启/恢复告警审计。
- 市场级基础风控已支持可选大盘红绿灯 gate，拒单原因分别为 `market_light_red`、`market_light_yellow`。
- 连续失败熔断已有基础实现，拒单原因为 `failure_fuse_open`。
- 完整模拟交易状态会返回行业归属覆盖率、已解析/缺失持仓数、缺失代码和行业市值分布；Web“可用性诊断”展示该组件，配置行业上限或目标权重时覆盖不完整会明确标为阻断。

未完成：

- 每日最大买入次数和每日最大买入金额已按账户基准币种统计，外币汇率缺失时新增买入 fail-closed。
- 候选级基础风控依赖 AlphaSift/DSA 返回的结构化字段；仍需补更稳定的股票状态源和涨跌停状态源。
- 市场环境过滤已有基础大盘红绿灯、持久化快照市场宽度、涨跌停强度退潮 gate、可配置快照新鲜度、盘中主指数/A 股实时涨跌家数宽度和版本化跨市场联动规则；指数 fallback 已区分 provider、抓取时刻、实时/session bar/收盘日线粒度，并可默认严格要求指数与宽度均有完整、新鲜的 provider quote as-of。剩余缺口是 efinance/AkShare 等来源本身尚未提供统一 provider 时间戳、交易所级时间同步和生产样本阈值校准；当前严格模式会对这类免费源明确 fail-closed，操作员只能通过可审计开关显式降级。
- 最大回撤已具备基于已观测权益峰值的持久化锁存和恢复缓冲，恢复时写入 `resolved` 审计事件；连续失败熔断已具备页面诊断、手动恢复、可选冷却自动恢复、告警中心历史和 alert 路由通知。

需要做：

- 扩展风控配置：单票上限、总仓位金额上限、行业上限、跨币种预算、最大持仓数、每日次数和每日预算已有基础实现。
- 强化股票状态过滤：股票黑名单、ST/退市风险、停牌、涨跌停、成交额过低已有基础实现；继续补独立状态数据源、价格不可用与跨市场规则。
- 强化账户级风控：最低现金余额、权益峰值最大回撤、回撤锁存/恢复缓冲、连续已平仓亏损门禁、连续失败熔断、两类冷却自动恢复和现金低水位告警已有基础实现；剩余重点是真实 gateway 长跑验收和跨市场账本精度证据。
- 强化市场级风控：大盘红绿灯、盘后宽度/热点退潮、盘中指数/A 股实时宽度、provider 时间严格门禁和跨市场联动已有基础实现；继续补免费源统一 quote as-of、生产阈值校准和更多市场宽度数据。
- 所有风控结果写入候选决策和交易计划。

验收标准：

- 最大持仓数、单票金额、总持仓金额、总仓位比例、行业金额、行业仓位比例、每日次数、每日预算、股票黑名单、ST/退市、停牌、涨跌停、流动性、账户级现金/回撤、市场红绿灯、盘后宽度/热点退潮、盘中指数/A 股实时宽度、跨市场联动和连续失败熔断拒单已有明确 reason code；生产行情时间一致性仍需外部证据。
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

- 当前 AlphaSift snapshot/daily 源已有 Web 可见失败计数、错误摘要和具体来源跨 run 降级率；候选补充阶段的行情、资金流和新闻也已作为 `candidate_context.quote/fund_flow/news` 接入同一健康快照、质量评分和跨 run 趋势，并新增 `quote/<provider>`、`fund_flow/<provider>`、`news/<provider>` 明细。自动 Agent 已按最近 30 天同策略/市场历史与当前连续失败生成有边界的 snapshot 来源权重，并以请求级参数动态调整资金流和新闻 provider 顺序；新闻异常继续 fallback，完整尝试链进入跨 run 证据，不同显式路由隔离缓存，且 provider 明细不重复计入本轮质量分。行情、资金流和新闻都具备 5 分钟冷却、单半开恢复和 24 小时内跨 API 重启健康延续；新闻无有效结果视为不确定，不错误推进熔断。
- 自动选股运行已按筛选完整性、候选字段/来源覆盖和 AlphaSift snapshot/daily/候选上下文来源健康生成确定性 0~100 统一质量评分；最低分门禁默认 60，旧空值也归一为 60，`poor/critical` 轮次默认阻止新增买入，显式设 0 才仅审计。行情、资金流和新闻不可用会独立降分并保留错误摘要，不再被笼统视为候选补充失败。
- 数据质量 `stale/unavailable` 和 AlphaSift 筛选异常已写入告警中心系统事件历史并复用 alert 路由外发通知，Web 模拟交易页已展示最近自动交易告警历史；实时行情、资金流与新闻具备自动切换/恢复，行情与资金流状态可跨 API 重启延续。

需要做：

- 基础数据质量合同已下沉到 AlphaSift screen 候选和 Web 选股页；Web 已展示 AlphaSift source health，Agent 控制台已提供整体质量、具体来源降级率趋势和本轮动态来源权重。
- 候选结果已标注数据来源、缺失字段和是否使用 screen cache，行情、资金流和新闻已有独立状态源；代码/价格是执行必需项，交易状态仅在启用 ST/停牌/涨跌停过滤时 fail-closed，成交额仅在配置最低成交额后 fail-closed，行业在启用行业约束时通过候选值或板块查询补齐且无法补齐会 fail-closed，名称等非执行字段保留质量扣分和警告。
- 自动交易只消费 `ok` 或可接受且分数至少 60 的 `partial` 数据；显式设最低分为 0 可仅审计软评分，但不能绕过 `stale/unavailable` 和候选关键字段硬门禁。候选字段已按实际启用风控分级处置，后续重点是更细的来源权重和自动恢复策略。
- 页面已展示 AlphaSift 源健康、本轮降级原因、模拟交易跨模块健康摘要、跨 run 整体质量、具体来源降级趋势、snapshot 动态路由，以及候选实时行情/资金流/新闻 provider 的优先级、熔断、冷却、半开探测、跨 API 重启恢复状态和下轮路由建议；下一项继续补外部权限下的长任务证据与真实 gateway 长跑验收。

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
- 内置 `DsaSimulatedGateway` 已支持默认保留状态的断线重连：资金、持仓、订单和计数器保持连续，在途延迟订单会恢复并恰好成交一次；`check_vnpy_adapter.py --require-vnpy --reconnect-cycles 3` 已验证三轮缓存保留、成交去重和编号唯一性，安装脚本默认执行该验收。
- 内置 gateway 新增默认关闭的确定性拒单与重复成交事件注入，安装验收的 `--fault-matrix` 已通过真实 MainEngine/EventEngine 证明拒单无成交、重复 `vt_tradeid` 仅保留一笔；同时修复 vn.py 中文原生状态映射，以及 `send_order()` 内同步终态早于 Agent 计划落库时的提交后即时对账竞态。
- 新增通用零下单 gateway 长跑验收器：读取外部 `VNPY_*` 配置，统计连接样本、状态切换、四类事件和自动重连结果，以最低连接率/必需事件作非零退出门禁，且输出不含连接路径或参数内容；`DSA_SIM` 单次断线验证已观察到自动恢复，真实账户仍待执行长窗口验收。
- vn.py runtime 已区分 `MainEngine.connect()` 请求受理与网关确认连接，连接异常不会拖垮 API 启动；支持状态钩子的网关会动态刷新连接状态，无法确认时健康状态 warning，明确断开时 blocked 且新增委托 fail-closed。
- vn.py runtime 已新增默认关闭的冷却自动重连监控：只恢复明确失败/断开的连接，每次重读外部参数文件，在已受理异步连接的可配置确认宽限期内避免误重连，并对连续失败执行有上限指数退避、恢复后复位；状态和统一系统健康保留线程、宽限期、基础/当前/最大间隔、连续失败、次数、时间、结果与下次检查审计。
- vn.py runtime 已新增一次性安全手动重连 API 和 Web 入口：后台自动重连关闭时也可使用，已连接、确认中或宽限期内不会重复登录；操作只恢复连接并返回审计，不触发 Agent run、交易计划或订单。
- 自动/手动重连的首次失败、退避封顶和恢复已通过独立队列写入现有告警历史并复用通知路由；重复失败去重、恢复 resolved、关闭排空和凭据脱敏已有确定性测试。
- 新增 `POST /api/v1/vnpy-paper/trade-plans/{plan_uid}/cancel` 和 Web “撤单”入口：`vnpy_paper` 的 `submitted` / `part_filled` 计划可映射为 vn.py `CancelRequest` 并调用 `MainEngine.cancel_order`，计划进入 `cancel_requested`，终态仍以 vn.py 订单回报为准。

未完成：

- 系统默认 Python 3.14.6 未安装 vn.py，继续保持本地 paper fallback；完整 vn.py 能力使用已验证的 Python 3.13.14 隔离环境。
- 已有 opt-in Gateway add/connect bootstrap、连接确认和冷却自动重连，但尚未安装和配置具体 gateway 插件/账户，真实 gateway 运行态、连接参数、回报和长期事件订阅稳定性未验证。
- 已能调用注入或启动期创建的 `MainEngine.send_order`，并支持订单状态、成交、账户和持仓回报通过 API 手动/外部同步；注入或启动期创建的 EventEngine 可自动 attach 回调，但真实 gateway 连接仍未验收。
- `vnpy_paper` 当前覆盖买入委托提交、自动按比例卖出提交、主动撤单请求、订单/成交状态回写、多笔成交累计、MainEngine 漏回报对账、对账异常保护、提交态/部分成交/撤单请求超时安全归档和活跃委托防重复；内置模拟 gateway 的重连、缓存保留和延迟成交去重已完成，真实 gateway 的长运行、重连和迟到回报验收仍未完成。
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
- Agent run 详情已从持久化交易计划和 Portfolio `trade_id` 动态派生本轮实际持仓变化，区分已入账、待回报、仅计划和无变化，并在模拟交易页、Agent 控制台、时间线和 JSON 导出中按标的展示买入、卖出、净股数与成交额；部分、迟到、审批或恢复成交入账后会自动更新。
- 模拟交易页已提供“重置账户”入口，可归档当前 `vnpy_paper` 模拟账户、创建干净账户并保留旧流水审计。
- 模拟交易页已展示“模拟账户历史”，可按全部、当前、已归档、活跃非当前筛选，并核对当前账户和已归档 `vnpy_paper` 账户；非当前账户可确认后恢复/切换为当前账户，后端只允许切换 `vnpy_paper` 账户并保留历史流水。
- 模拟交易页已展示基础绩效摘要、权益曲线、日度收益表、月度收益表和窗口级绩效矩阵，可按 Agent run 创建时间窗口筛选，聚合当前 paper 账户收益、收益率、成交额、买卖次数、FIFO 卖出胜率、最大回撤、换手率、当前仓位、最近 Agent run 成交/计划/跳过统计、主要跳过原因和按策略/行业归因。
- 模拟交易页已提供最近 Agent run 批量导出入口，可按策略、市场和状态筛选后导出运行详情、候选决策、交易计划和时间线 JSON。
- Web 已新增独立“Agent 控制台”入口 `/agent-console`，可分页浏览并按策略、市场、状态和时间范围筛选自动选股 run，查看今日结构化总结、复核质量摘要、运行详情、Agent 计划、运行总结、时间线、候选决策、交易计划，支持 `/agent-console/<run_uid>` 独立详情路由并兼容 `/agent-console?runUid=<run_uid>` 查询参数深链，并导出当前 run 或最近 run JSON；模拟交易页也已提供可用性诊断摘要。

未完成：

- 独立任务历史页已有 MVP 控制台、分页、筛选表格、`runUid` 查询参数兼容深链和 `/agent-console/<runUid>` 独立详情路由；后台任务日志已在模拟交易页基于 `scheduler.task_events` 和持久化 `/task-events` 展示，并支持按任务名/状态筛选。
- 一键重置测试账户已有基础能力，并已有旧归档账户历史列表、状态筛选、恢复/切换和隐藏式批量清理入口；隐藏式清理不删除 Portfolio 流水。
- 运行中状态、任务级下次运行时间、最近错误、后台任务日志和 Agent run 时间线已有基础状态字段。
- 绩效摘要已有 MVP，并已包含本地 paper 权益路径页面曲线、日度收益、月度收益、最大回撤、换手率、按策略/行业归因、窗口级绩效矩阵、Agent run 创建时间窗口筛选、已记录候选的 1/5/10/20 日样本外前瞻评价、带字段覆盖门禁的单日时间点策略重放，以及按现金、A 股整手和等权或显式股票目标权重执行的跨日组合回测；Tushare 生命周期元数据与已实施公司行动已接入持久化、分批、租约保护的可恢复采集，拆并股支持按显式结算价兑付零碎股。A 股印花税已按 2005/2007/2008/2023 官方生效日和买卖方向自动切换，买方税进入整手与现金约束；2005-01-24 之前仍 fail-closed。常规、低佣和零成本参考档位已有版本审计，实际券商条款通过 `custom` 显式输入而不由系统猜测。剩余为外部权限下的成功在线长任务证据。
- 全市场采集作业已把持久化租约与进程内任务队列对账并暴露 `active/orphaned/retryable/complete`，服务重启遗留作业可由普通恢复安全接管，活跃作业继续以 409 防重复；新增只读默认的在线验收器门禁规模、覆盖率、源错误、检查点回退和停滞。2026-07-20 外部探测确认当前 Tushare token 缺少 `stock_basic` 权限并返回 424，因此未创建长任务，成功全市场/公司行动在线证据仍未完成。

需要做：

- Agent 控制台和运行历史表格已有 MVP，独立详情路由已落地；后台任务日志已在模拟交易页落地，支持按任务名和状态筛选持久化最近事件。
- 增加详情页：候选、解释、风控、订单、日志；当前模拟交易页和 Agent 控制台已有内嵌候选、交易计划和运行时间线基础详情。
- 暂停确认、恢复、立即执行、立即 dry-run、深链详情、持久化可筛选后台任务日志、任务趋势摘要、任务健康检查、账户历史筛选/恢复/切换/隐藏式清理、日度收益表、月度收益表和导出已有基础入口；继续补完整历史行情驱动的长期绩效审计。
- 测试账户重置、旧归档账户历史列表、状态筛选、恢复/切换、隐藏式清理、单次 Agent run JSON 导出和最近运行批量导出已有基础入口，并支持策略/市场/状态/时间范围筛选。
- 运行中状态、任务级下次执行时间、最近错误、读取持久化最近事件的后台任务健康检查、持久化可筛选后台任务日志、任务趋势摘要、7/30/90 天终态运行长期指标、provider 级跨 run 数据质量趋势及 snapshot/资金流/新闻请求级权重、任务事件保留/低频清理策略和 Agent run 时间线已有基础展示；继续补真实环境长期可用率。

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
- API 端到端风控拒单矩阵已覆盖黑名单、ST/退市风险、停牌、涨停和流动性不足五类候选同轮 fail-closed，验证零成交，并逐项回查 Agent 决策与交易计划中的持久化拒绝原因。
- 数据源异常、来源健康降级和最低数据质量分门禁已有 fail-closed 与审计保留回归；真实 MainEngine/EventEngine 已新增三候选混合结果矩阵，覆盖两单成交、一单同步拒单、重复回报幂等、运行计数一致和订单隔离。
- 行情缺价已有候选级 fail-closed 与真实 MainEngine/EventEngine 三候选隔离矩阵；缺价候选保留价格解析审计并跳过，前后候选继续成交，未知价格不会进入 vn.py。
- 自动交易 dry-run、任务历史和详情 Web 路径已有回归覆盖；Web 全量测试门禁已恢复稳定通过。
- gateway 无下单 soak 验收已支持强制重连矩阵：要求观测到重连尝试、成功和最终 connected；DSA_SIM 断线注入会自动启用并校验该门禁，真实 gateway 仍需部署环境提供长时间证据。
- 新增部署态 runtime 只读长跑验收器：它直接观察承载 Web/API 的进程，不再用旁路 MainEngine 代替主系统证据；可门禁 API/runtime/连接/四类回调持续可用率、contract、进程/gateway 身份变化及自动重连计数增量与单调性，且不调用任何写接口。
- scheduler 后台任务的启动立即执行语义已按任务名跨重载保留：`vnpy_paper_auto_retry` 在连续注册生命周期只启动扫描一次，普通配置 reconcile 不重复扫描，禁用后重新启用会再次执行；自动交易、自动恢复和事件监控的注册状态彼此隔离。
- 新增 `scripts/check_online_agent_vnpy_e2e.py`，默认以 dry-run 验证真实在线候选、可追溯决策/计划和账本零变化；显式授权后仅允许内置 DSA_SIM 委托，可校验 run/计划终态、EventEngine 四类回调、成交 ID、Portfolio 资金/持仓变化和临时时段门禁恢复。2026-07-20 真实 API 的 dry-run 与全已有持仓安全跳过各通过一轮，正向成交证据由同日 `600015` 1400 股 @ 6.98 的真实 EventEngine 回写提供。
- 验收门禁已支持隔离账户正向成交：暂停自动买入、创建干净账本、校验成交后恢复原账户/设置并隐藏测试账本，reset 响应丢失也按账户 ID 差分恢复。真实 run `ss-agent-20260720101013-dd345609` 在临时账户完成 3/3 成交及精确资金/持仓对账，随后恢复原账户 2 和全部运行设置。
- 新增 scheduler 只读长跑验收器，可门禁 API 可达率、调度循环存活率、必需任务持续注册率、本次窗口新增终态事件、失败数和跨代重叠跳过数；首次事件读取只建立历史基线，脚本不触发选股、计划或订单。真实长窗口证据仍需在部署环境执行并留存 JSON。
- 2026-07-20 已在 Python 3.13 真实 API 进程中启用 MainEngine/EventEngine、DSA_SIM、四类事件回写和自动重连：75 秒 scheduler soak 共 74 次成功采样，API/loop/恢复任务注册率均为 100%，新增恢复终态 1 次、失败 0、跨代重叠跳过 0；临时 1 分钟任务间隔已恢复为原 1440 分钟。该证据验证内置模拟运行链路，不替代真实券商 gateway 长窗口验收。
- 同一部署进程通过新增的 75 秒 runtime soak：75/75 样本的 API、runtime、confirmed connection、事件桥和订单/成交/账户/持仓四类回调注册率均为 100%，contract v3，进程变化、响应错误和新增重连失败均为 0；全程未创建 run、计划或订单。该证据关闭了“旁路进程不能证明实际 API runtime”的验收缺口，但仍不替代真实券商 gateway。
- scheduler 完整测试文件已修复全局 `sys.modules` 回滚造成的测试污染，Python 3.13.14 与 3.14.6 均为 27/27 通过。
- AlphaSift 最终候选的实时行情交易状态映射已补齐：仅在非 ST 名称、正成交量/成交额和 A 股绝对涨跌幅低于 4.5% 的保守证据下派生对应否定状态，记录 provider、观测时间和字段依据；接近最低 5% 限幅或证据不全仍 fail-closed。2026-07-20 Python 3.13 真实 dry-run 从修复前 3 个候选全部 `candidate_trading_status_unavailable`，改善为 1 个有效 dry-run 计划、2 个 `position_exists` 跳过、0 提交和 0 新成交。
- 2026-07-20 已完成真实 Python 3.13 API 的在线选股 Agent -> DSA_SIM 模拟成交：3 个候选生成 1 笔 vn.py 提交、2 个 `position_exists` 跳过，`DSA_SIM.1` 经 EventEngine 回写 `filled`，Portfolio 新增 `600015` 1400 股 @ 6.98，成交总数 12->13、现金 27474->17702；临时时间门禁在 `finally` 路径恢复为开启，gateway connected 且订单/成交/账户/持仓四类回调保持注册。该证据完成内置 vn.py 模拟交易主路径，不替代真实券商 gateway 长窗口验收。

未完成：

- 端到端主路径已有基础覆盖并新增可重复的在线 Agent -> DSA_SIM API 验收门禁；scheduler 已覆盖配置重载跨代重叠、审计跳过、异常释放及启动恢复扫描的一次性注册/重新启用语义，真实 MainEngine/EventEngine 已覆盖多候选混合结果、显式拒单、重复成交回报和循环重连，仍缺真实 gateway 长时间运行场景矩阵。
- LLM 重排超时已有降级、跨 run 熔断、冷却恢复和单半开探测回归；scheduler reconcile 的同名任务防重已有确定性并发回归，重复成交事件和行情缺价隔离已通过真实 EventEngine 验证，仍需完成真实 gateway 长时间运行组合矩阵，非交易时段已有基础 gate 测试。
- 纸面交易绩效摘要已有基础能力，已包含权益曲线、日度收益、月度收益、FIFO 卖出胜率、最大回撤、换手率、策略/行业贡献评估、窗口级绩效矩阵、Agent run 创建时间窗口筛选、已记录候选的严格后向多周期评价、单日时间点策略重放和现金/整手/等权或显式股票目标权重组合回测；组合回测可从 Tushare 因子采集结果自动读取或显式覆盖现金分红、拆并股，按显式结算价兑付零碎股，选择版本化参考成本档位或自定义实际账户成本，并在固定卖出税或 2005 年以来 A 股分方向历史印花税之间选择，审计事件及逐笔/逐期/汇总成本。历史全 A 股逐日期成员解析和分批可恢复因子/公司行动采集已完成，仍缺具备外部权限后的成功在线长任务证据。
- 全市场验收门禁与重启孤儿租约自动接管已完成；当前环境因 Tushare `stock_basic` 权限不足无法生成历史成员，API 已按契约 424 fail-closed。待具备权限后运行 `scripts/check_full_market_ingestion_e2e.py --create --allow-external-ingestion ...` 留存成功长任务 JSON。

需要做：

- 端到端集成测试已有固定候选/固定成交价的成功主路径和五类风控拒单失败矩阵；继续扩展真实 gateway 与 scheduler 长运行组合路径。
- scheduler 自动任务测试已覆盖开启、关闭、重启 reconcile、启动立即任务不因普通重载重复运行、禁用后重新注册恢复，以及重载前任务未结束时的跨代防重、持久化跳过审计和异常后锁释放；只读 soak 工具已可生成门禁证据，继续在真实部署执行长窗口并留存 JSON。
- 继续按新增交互扩展 Web 任务历史和详情测试；当前页面/API 主路径已有覆盖。
- 基础模拟交易绩效摘要已覆盖收益、收益率、权益曲线、日度收益、月度收益、成交额、买卖次数、FIFO 卖出胜率、最大回撤、换手率、当前仓位、成交/计划、跳过原因、策略/行业归因、窗口级绩效矩阵、Agent run 创建时间窗口筛选、已记录候选的多周期前瞻评价、时间点策略重放，以及带版本化/自定义成本、基准、显式股票目标权重、重叠持仓保留、卖出受阻跨期延续、公司行动、2005 年以来 A 股历史印花税和基础停牌/涨跌停约束的跨日组合回测；继续补历史全市场在线证据。

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

- 行业仓位、单票/总仓位、黑名单、候选级基础风控、账户级基础风控、市场红绿灯 gate、数据质量风控和候选级缺失字段展示已落地；代码/价格、交易状态、成交额和行业已按对应启用约束分级 fail-closed，非执行字段保留质量扣分和警告，继续补来源自动恢复。
- 卖出、止损、止盈、暴露上限、跨币种预算、评分加权/20 日逆波动率/相关性上限/目标缺口协方差优化和目标权重驱动的基础调仓规则已落地；继续补真实 gateway 长跑验证。
- dry-run、手动审批、受控计划重试、交易计划恢复矩阵、手动恢复扫描、连续失败熔断、关键异常告警历史和 alert 路由通知已有基础能力；继续补完整订单生命周期真实 gateway 验证和完整自动恢复策略。
- 继续用真实 gateway 长跑验证自动失败重试、熔断探测和订单超时后的恢复策略。

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
