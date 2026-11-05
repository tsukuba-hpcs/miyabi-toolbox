# bypass-lora 的 miyabi-mcp 改进建议：只读设计审核

审核日期：2026-11-05（JST）。审核对象：`reports/bypass-lora-miyabi-mcp-improvements-2026-11-03.md`，下称“原文”。代码基线为 `3da787611c72eed4289fbb5594ef087af6bcfaff`。

**结论。** 原文发现的 MIG 标签失真、执行资源证据未持久化、通知诊断不足、工作目录误解均值得处理，但不能按原建议整体实现。尤其应改掉“从 exec_vnode 的 `_migN` 后缀推导规格”“服务端自动 ensure 客户端 daemon”“只改配置就能加快轮询”“在脚本前面自动插入 cd”这几项。优先修复证据和通知正确性，再改善易用性；跨单位预算引擎、自适应轮询和多退出码不宜作为第一批工作。依据见下文逐条审核及遗漏问题。

**证据范围与标记。** “已核实”指直接读到当前代码、账本、产物或本站命令输出；“静态确定”指代码存在明确可达的失败路径，但本次没有执行该路径；“推断”与“未核实”均明确标出。项目研究笔记只能证明当时记录了什么，不等于本次重新验证了 GPU 数值机制。

为缩短证据路径，本文使用以下前缀，均可直接展开：`M/` = 本仓库 `miyabi-mcp/`；`C/` = 本仓库 `miyabi-mcp/src/miyabi_mcp/`；`T/` = 本仓库 `miyabi-pbs-shell-template/`；`B/` = `/work/xg24i002/x10041/bypass-lora/`；`L/` = `B/.miyabi-mcp/`；`U/` = `/work/xg24i002/x10041/.miyabi-mcp/`。账本引用除文件外还标明有关字段，避免把大段通知消息及确认 token 抄入报告。

本次只读取上述代码、模板、测试、账本，以及原文所指现象直接关联的少量 bypass-lora 产物、研究笔记和约束。未审核 smooth-research 一侧的改进报告，未运行其 CLI、修改其研究记录或重新裁定研究结论。唯一新增文件是本报告；未运行 qsub/qdel、notify poll/list/listen 或 notifyd 的任何管理命令。部分 MCP 查询会刷新账本或创建状态目录，故本次使用文件读取和有界原生 qstat 查询，不调用这些接口。相关副作用见 `C/jobs.py:154`、`C/jobs.py:353`、`C/notify.py:334`、`C/config.py:56`。

**基线和使用概况的核对。**

| 原文陈述 | 审核结果与证据 |
| --- | --- |
| 当前代码为 3da7876，提交日期 10-31 | 已核实 HEAD 一致，受审代码、模板没有工作树改动。`git log -1` 的提交时间是 `2026-10-31T16:35:42Z`，即 JST 的 **11-01 01:35:42**；若全文统一 JST，应修正日期或注明 UTC。当前 Git 状态不能单独证明项目期间从未运行过临时修改版。命令记录 Q1。 |
| 459 个作业记录，458 个结束，415 成功、42 失败、1 取消，另 1 个提交失败 | 已核实，但准确名称是 **459 条 submission 记录、458 个获 PBS ID 的作业**。`L/jobs/` 只有 458 项，`L/submissions/*/record.json` 有 459 项；`outcome=success` 是 PBS 层正常退出，不等于研究结果有效。Q2；`C/states.py:69`、`M/DESIGN.md:412`。 |
| 请求 279.04 MIG-h，实际 152.99 MIG-h，约 38.2 node·h | 账本汇总能复现。279.042222 MIG-h **包含被 qsub 拒绝的 1 分钟请求**；成功获 ID 的请求量为 279.025556 MIG-h。已记录实际量为 152.992509 MIG-h，按项目规则为 38.248127 node·h，约为总预算 19.124%。应把“实际用掉”限定为“账本已记录用量”。Q2；`L/submissions/20261103-161142-0cb534/record.json`；`B/docs/constraints.md:34`。 |
| 有用量的 456 个，中位 4 分钟，287 个 ≤10 分钟、313 个 ≤15 分钟 | 数字对应 **正用量** 作业。实际有 457 条 usage，其中 `3757929` 明确记录 0 秒；正用量 456 条的原始 walltime 中位为 241 秒，两个计数正确。含零值时为 457 条、中位 239 秒、288/314 条。缺 usage 与已测得零不可混淆。Q2；`L/jobs/3757929/record.json` 的 `usage`；`C/states.py:29`。 |
| Codex 和 Claude Code 都直接使用 MCP | 提交记录支持：325 条 actor 为 Codex，134 条为 Claude Code。通知账本的 **153 条全部是 `deliver.method=listener`、`notifier=miyabi-g2`**；不能据此声称 Codex 自动走了 codex_queue，也不能从账本还原所有 wait/listen 调用。Q2。 |

**1. MIG 规格、标签和资源选择（原文 P0）。**

1. **标签错误已核实，根因诊断基本成立，但定位不完整。** `B/research/artifacts/ATT-0415-1/mig-profile.json:5` 绑定 PBS `3787033`，`:6` 为 mg1112，`:8` 为 `1g.24gb`；`L/jobs/3787033/record.json` 的 `usage.hardware` 却为 `GH200 MIG 2g.24gb`，`quantity=2.106667`。同一探针也保存在 `L/logs/t0035-s0-W1-20261103-160614-814afd.log:2`。`U/config.json:2` 的固定字符串经 `C/observe.py:92`、`C/states.py:40` 进入所有这类 usage。错误在于把一次实测推广为每次运行的硬件事实；实例小时数本身仍按 walltime × 实例数计算，不能因标签错误就说这笔 MIG-h 数量错了。`M/DESIGN.md:836` 确实作了这个过强假设。

2. **“事后查不到切片”说得过满；“从 vnode 后缀推导”在当前输出上不可用。** 账本确实不存 `exec_vnode/exec_host`，但 `job_get.detail` 已经暴露两字段，历史仍在时可以再查，部分项目日志也保存了原始 qstat。实际有界查询 `qstat -H -f 3787033 3766228` 返回的分别是 `exec_vnode=(mg1112:…:nvnodes-mig=1)`、`(mg1109:…:nvnodes-mig=1)`，**没有 `_mig3` 后缀**；`exec_host` 是 `mg1112/3*18`、`mg1109/3*18`。原文把项目使用的切片称呼当成了 PBS 原始字段格式。不能未经站点保证把 `/3` 的 execution slot 当成稳定 profile 标识。证据：Q3；`C/states.py:105`、`C/jobs.py:183`；`L/logs/v2-f-smoke-20261101-170706-d1a5d1.log:20`。

3. **持久化建议正确，应在现有运行/结束观测时保存，而非只在结束时新增查询。** 根因不只 `states.usage()` 丢弃字符串：`C/states.py:65` 返回的 observation 没有执行资源，`C/ledger.py:130` 的事件白名单及 `:140` 的 state 投影也不会保存它们。最小一致改法是在现有 detail 读取路径中保存有界、白名单化的执行事实及其来源时间；记录不存在时保持 unknown，已有运行观测不被后续缺失值清掉。避免存整个 `Variable_List`。终态通常不再刷新，补历史数据必须走显式、带证据的修订流程，不能期望普通 refresh 自动补齐。证据：`C/observe.py:100`、`:116`、`:128`；`C/ledger.py:106`；`M/DESIGN.md:148`、`:399`、`:401`。

4. **profile 应来自作业内对实际分配设备的测量，suffix 映射至多是注明依据的推断。** 单独执行 `nvidia-smi -L` 后取第一条 MIG 行不够：应关联分配给进程的设备/UUID，记录 profile、host、job ID、采集时间和来源产物；检测失败时记录 unknown。多实例作业可能包含不同 profile，接口宜保存设备列表；可以另给单一 profile 的便利摘要，但不得把 mixed 或 unknown 压成一个确定字符串。探针应由用户可见的模板/作业脚本执行，留在计算分配内，不能由登录侧服务直接运行 GPU 检查。证据：上述 `mig-profile.json` 的 `cuda_visible_devices`、`uuid`、`selected_line`；本站 Q4 的 `AVAIL_MIG=1,2,4`；`M/DESIGN.md:43`、`:148`、`:427`。此为设计建议，未实现或运行新探针。

5. **保持历史字节稳定是对的，永久把新作业也称为 2g 则不对。** 原文“hardware 保持不变”能暂时保住旧累计桶，却保留了已证伪的物理含义。建议先在原生 record/event 中提供独立的真实执行元数据，并明确旧 `usage.hardware` 是历史记账标签、不可用于性能或数值分组；随后与下游约定新事件的正确标签或中性 `GH200 MIG` 标签及生效边界。历史事件不要原地改写，也不要导出时读取现在的 record 给旧 Observation 临时加字段。`M/DESIGN.md:824` 要求同 event ID 导出字节不变，`:826` 又按 hardware 分桶；修正硬件语义需要明确修订这部分契约。已有 `reconciled/correction_of` 是候选路径，但其对用量更正、跨 hardware 桶迁移的下游行为本次未核实，不能假定可直接批量回填。证据：`C/export.py:15`、`:31`、`:34`；`C/ledger.py:110`、`:135`；`M/tests/test_flow.py:277` 的现有导出字段断言。

6. **“job_get、job_list 和导出都带 profile”须分接口落地。** `job_get` 与 `job_list` 可先在 `ledger` 下返回已保存的元数据，复用 `C/jobs.py:96`，不为每次列表新增 N 次 qstat 或 GPU 查询。原生账本导出可以包含新增字段；smooth-research 格式必须先确认消费者 schema，或暂由已有 `execution_ref.locator` 引用 record。不能为了“一致出现字段”改掉旧事件的导出内容。证据：`C/jobs.py:104`、`:154`、`:382`；`C/export.py:24`；`M/DESIGN.md:133`、`:824`。

7. **vnode 提交失败、dry run 漏检均已核实；完整的“服务器接受性检查”承诺过强。** `L/plans/bc713d1a9cc81878d70a1f0f46a9a9cb.json` 保存了 `select=1:mpiprocs=1:vnode=mg1112_mig3`，对应 submission 的 `error` 是 `qsub: Unknown Resource: vnode`。`C/submit.py:47` 只提取 chunk 数，`:186` 直接保留 select，未验证资源名。建议先按本站已支持的 select 语法和资源集合校验，明确拒绝已知不支持的 `vnode`，对未知扩展给出明确的“不支持/待确认”，而非伪装成服务器预执行。本站 qsub 手册列出 chunk 的 `mpiprocs/ompthreads/mem`，但资源存在也不等于队列 hook、取值和组合都接受；本次未发现可证明完整接受性的无副作用预提交接口。不要拿 `qsub -h` 或“试投后删除”实现 dry run。证据：`/usr/local/share/man/man1/qsub.1:312`；`C/pbs.py:92`、`:110`；`M/DESIGN.md:149`、`:150`。

8. **可选性和计费已有官方公开依据，无需全部悬置。** 官方页面说明 MIG#0–2 为 32 SM/24 GB，MIG#3 为 26 SM/24 GB，无法通过提交选项避开 MIG#3；批处理 MIG 的系数统一为每实例 0.25。公开规则支持“规格不能保证、批处理按实例计费”的说明；它没有给出 NVIDIA 的 `1g/2g` 字符串与 PBS execution slot 的稳定映射。项目中两种 profile 的存在已经测到，但不要把名称理解为两倍算力。原文第 5 条可改成“引用当前官方规则；特殊计费或映射疑问再向站点确认”。互动 MIG 不消耗站点 token，也说明项目预算折算和站点计费不能混为一谈。[站点 MIG 说明](https://www.cc.u-tokyo.ac.jp/supercomputer/miyabi/service/mig.php)，核对于 2026-11-05；项目预算另见 `B/docs/constraints.md:34`。

9. **研究影响有记录支持，但列举的原因应拆开，不能都归为 MCP 导致的数值错误。** `ATT-0158-1` 是 profile guard 主动退出，直接证据为 `B/runs/ATT-0158-1/pbs.out:1` 和 `L/jobs/3766228/record.json` 的 exit 3。V2-03 重载失败和 2g 重跑一致记在 `B/research/notes/T-0016-results.md:24`、`:38`；RB1-02 的跨 profile 差异及同分配零差异记在 `B/research/notes/T-0027-results.md:22`、`:28`。`B/research/reviews/RV-0087.md:5`、`:9` 的 medium 是对跨作业门禁与重跑成本的审核意见，不是另一场已执行的失败。CPU 初始化修复明确是 E-0029 up-only 初始化一致性问题，与 BF16 舍入区别开，见 `B/research/notes/T-0032-portable-init-repair.md:5`、`:12`、`:19`；不能说整个项目的初始化都统一搬到了 CPU。D-0019/D-0050/D-0060 的引用可在这些笔记及 `B/research/notes/T-0031-delivery.md:153` 交叉看到，但本次没有读取研究数据库重新核定各决定。补全 MCP 元数据有助于诊断，不会自动保证跨作业位级一致；科学比较的门禁属于上层研究设计，符合 `M/DESIGN.md:42`。

**优先级：建议将此节拆开。** 错误标签说明、执行事实持久化和已测 profile 的可追溯性为 **P1，且排在第一批**；若正在依据错误硬件标签批准严格复现结论，应立即停止这种解释，按项目证据重新判断。当前证据不足以把所有结果正确性归咎于 MCP，故不支持整节 P0。新导出契约/历史迁移是 P1 的后续协调项；资源名预检 P2，可与已有合法 MIG 规格被拒的问题一并修；后者见遗漏项 O6。

**2.1 daemon 停止与恢复（原文 P1）。**

- **已核实的是心跳语义，不是两次崩溃原因。** `C/notify.py:73` 的 `daemon_running` 只表示最近 `2 * poll + 60` 秒内有 poll 心跳，不做进程存活检查；心跳还在 `notify.poll` 开始时写入，不能证明该轮成功。`notify_list=false` 可能是正常空闲退出、未启动、连接故障、轮询过慢、notifier 不匹配或进程终止。当前 `U/notifyd/miyabi-g2.json:13` 记录 11-03 19:15:13 启动、`:14` 为 `exited_idle`，19:55:17 退出；这只证明最后一次退出正常，不能追溯原文的两次事故。日志 `U/notifyd/miyabi-g2.log` 没有完整的 start/exit 生命周期记录，本次检索未见 poll_failed 或异常 traceback。研究笔记 `B/research/notes/T-0033-progress.md:524` 支持一次重启叙述，不能独立证实“11-02/11-03 各一次且都是异常退出”。
- **listener 每 60 秒检查大体属实，但有条件。** `C/notifyd.py:278` 先检查 known 列表，`:284` 可以直接退出 3，然后才到 `:289` 的 ensure。它是“仍在等待时周期检查”，不是持续监督所有通知。更关键的是本项目所有 153 个通知均为 listener，原文第 2.3 节还描述研究者自己的 listener；“非 Claude 没开 listener”不能直接作为这两次事件的根因。Q2；`C/deliver.py:23`；`C/server.py:41`。
- **不赞成在 job_submit/notify_create 内无条件 ensure。** 服务端在 Miyabi，daemon 在代理机器；远端客户端时服务端启动的是错误位置的进程。即使本地部署可做，也不能成为只适用于拓扑 A 的隐式例外。返回值已经带 heartbeat、hint、start_command；缺的是准确的健康语义、客户端执行启动步骤及退出原因。让 dry run 在未启动 daemon 时硬失败还与“先登记通知，客户端随后启动”以及空闲自动退出冲突，可能制造启动后还未 poll 就失败的假阻塞。证据：`M/DESIGN.md:151`、`:652`、`:653`；`C/notify.py:158`、`:184`、`:82`；`C/notifyd.py:221`。
- **较简单的改进：** 保持客户端负责生命周期；把心跳新鲜度、最后成功处理时间、实际间隔、错误及空闲退出分开报告，保留旧字段时解释其含义。对有待处理通知而健康未知/过期给出显眼提示；列表增加概况字段比调整 JSON 键顺序更可靠。应记录 start/exit/reason、失败的项目及 poll 成功/失败摘要；若需要进程死后自动恢复，另由代理侧 supervisor 负责，创建时 ensure 一次不构成长期保障。优先修 O1 的静默错误路径，再用日志定位真实退出原因。依据：`C/notifyd.py:173`、`:191`、`:243`；`C/notify.py:212`；`M/DESIGN.md:135`。

**优先级：P1 合适，但目标应是失效可见与正确恢复位置，而非服务端代启进程。**

**2.2 五分钟轮询与短作业（原文 P1）。**

- **默认 300 秒已核实，“最多 5 分钟”不是硬上界。** `C/notifyd.py:236` 是完成 poll/投递后再睡一个间隔；PBS 查询、共享缓存、历史可见性、提交宽限期和失败退避都会增加延迟。`C/observe.py:112` 有默认 120 秒提交宽限期；`:38` 的缓存与 `C/notify.py:338` 的 60 秒快照缓存也会参与。调短 daemon 间隔不一定同比缩短真实 qstat 数据新鲜度。`M/DESIGN.md:654` 原本也限定为“约 5 分钟加一次 poll 耗时”。
- **例子的三个阶段应全部列出。** `3789152` 的 PBS 结束 19:29:42、账本观测 19:34:10（268 秒后）、通知 fired/delivered **19:35:16**（334 秒后）、ack 19:35:50（368 秒后）。19:34:10 不能当成 daemon 投递时间，也不能把 ack 的耗时归给 PBS 轮询。证据：`L/events/2026-11.jsonl:1354`；`L/notifications/N-20261103-192840-bbde.json` 的 history；`U/notifyd/miyabi-g2.log:159`；Q5。
- **遗漏了一个真实配置缺陷：只写 README 让用户调 `notify_poll_seconds` 不会奏效。** 该配置只被 `daemon_info` 用来显示/判断心跳、被 `notify.poll` 放进返回结果；生成的启动和监听命令没有 `--poll-seconds`。CLI 默认 300，daemon 使用 `Options.poll_seconds` 睡眠，并不读取 poll 返回的 `poll_seconds`；已存活 daemon 的 ensure 还会直接返回，不更新选项。于是配置与实际轮询可分离，甚至让健康检查误报。证据：`C/notify.py:75`、`:83`、`:367`；`C/cli.py:42`、`:50`；`C/notifyd.py:143`、`:211`、`:236`。
- **建议先统一间隔的来源与报告，再考虑调度策略。** 最小方案是明确 daemon 级别的生效配置/CLI 优先级、把请求间隔传到客户端启动命令、报告实际间隔，以及已运行实例配置不一致时的行为；不能谎称 ensure 已应用新配置。可把 60–120 秒作为待测候选，而非本次直接规定的站点安全频率；所有等待继续共享快照，验证调用量和历史查询成本。按 walltime“快结束了”加速不可靠：walltime 是上限，排队时长未知，提前结束/失败更需要及时报告；`expected_seconds` 也只是提示。每通知独立轮询会放大查询量，若以后确有必要，应由一个 daemon 合并调度并设全局最低间隔。证据：`C/submit.py:293`；`M/DESIGN.md:133`、`:150`、`:650`。

**优先级：配置接线错误 P1；降低默认间隔需测量；自适应/单通知间隔 P2 或推迟。**

**2.3 listener 退出码 3（原文 P1 子项）。**

**行为和文档缺口已核实，六次事件的归因未核实。** `C/notifyd.py:283` 的判断仅凭“成功 poll 后，本 ID 不在 known 且 inbox 没有待取消息”；它没有区分 cancelled、delivered、acknowledged、failed、错误 notifier 或未知 ID，更不知道是谁读取了消息。`C/notifyd.py:273` 用 rename 将 `.json` 变成 `.shown`，支持“另一个 listener 先取走”的可能性，但没有记录消费方身份。账本证实一个取消通知 `L/notifications/N-20261101-180134-9305.json`；无法从通知文件恢复“恰好 6 次 exit 3，其中 5 次由研究者接收”的完整调用历史。该数量应标为协调者回忆并补原始 CLI 输出。

**更好的最小方案是保留 3 的兼容含义，增加结构化 reason 和状态证据。** 若向服务端确认到 cancelled/delivered 等，附通知 ID、notifier、投递方式、相应时间；只有实际记录了 listener receipt 才写“由某接收者消费”。`delivered` 对 listener 只代表已写入 inbox，不能冒充“代理已经看到”。查询失败则说明状态不明。客户端诊断应为有界查询，不改动 delivery/ack 状态。把每个原因分配不同退出码会扩大 shell 调用契约，未见消费者需要，建议推迟。文档也应覆盖 **75（无法维持 daemon）**：该码已由 `C/notifyd.py:258`、`:264`、`:290` 实现，原文漏掉了它。`C/cli.py:4` 主要说明工具 JSON 命令的 0/1/2，没有完整说明 listener 例外。

**优先级：P2 的诊断/文档改进；若定位到漏投递，按 P1 处理漏投递原因。** 证据：`C/deliver.py:48`、`C/notify.py:207`、`:392`；`M/DESIGN.md:268`、`:686`。

**2.4 job_wait 后仍未 ack（原文 P1 子项）。**

- **当前契约下这是独立状态，不是 job_wait 少做了一次确认。** `job_wait` 仅刷新作业并判断条件，不消费通知或调用 ack。知道作业结束不等于收到完整通知；它还可能只等一个大分组的部分成员。`M/DESIGN.md:624` 要求 token 只出现在通知消息中。自动 ack、在列表暴露 token，或者按“任意 wait 已结束”取消通知都不合适。证据：`C/notify.py:439`、`:457`、`:235`。
- **积压现象成立，但“恰好一次 6 个且均由 wait 导致”未被账本单独证明。** 当前 153 个通知中 20 个为 delivered、132 个 acknowledged、1 个 cancelled；例如 `L/notifications/N-20261101-170707-5690.json` 仍 delivered。通知账本没有 job_wait 调用记录，不能据此判断谁已收到内容、为何未 ack。Q2。
- **原文的关联提示方向正确，应收窄为信息呈现。** wait 可从记录的 `notifications` 返回相关通知 ID、当前状态与合适的收件/确认流程，不擅自触发、取消或确认；要覆盖 started、early_failure、timeout、未封口及多通知情形。`notify_list(status=["delivered"])` 过滤能力已经存在，可补“待确认”数量、投递时间、错误说明。`notify listen --id` 仅能取得尚在本地 inbox 中的消息；若已被另一 listener 改名 `.shown`，再次 listen 不能可靠取 token，还可能重新启动 daemon。应说明该限制，并设计显式的消息查看/恢复流程，而非把 listen 宣传成通用取 token 命令。证据：`C/jobs.py:96`、`C/notify.py:136`、`:212`、`C/notifyd.py:262`、`:271`。

**优先级：P2 的工作流可见性；保留人工/代理显式 ack。** 同时应先修 early/final 确认混用，见 O5。

**3. 账本、预算与导出（原文整体 P1）。**

**3.a 统一预算。** 原文对项目约束和 `budget_windows` 的描述基本准确：`B/docs/constraints.md:34` 是实际总成本 ≤200 node·h、80% 报告；`C/policy.py:139` 检查的是时间窗内已提交请求量加本次请求，按原生单位比较，不看累计实际量。但这首先是两种预算契约的差异，并非现有窗口算法算错。`ledger_query(format="summary")` 已提供按单位的 requested/used，无需解析每个事件再累计；全项目实际成本也不能把 submitted/running/ended 各事件的 usage 重复相加。证据：`C/jobs.py:364`、`M/README.md:103`。

**项目当前全部是单实例 MIG，最简单的立即方案是展示折算。** 直接将 summary 的 MIG 用量乘既定 0.25，并保留原始单位、覆盖范围和缺失数量，足以解决手算；这不是要把 0.25 写死成系统全局常数。跨单位硬预算只有在明确要由 miyabi-mcp 强制执行后再加。`M/DESIGN.md:32` 原本把 usage_report 排在后续版本，`:42`、`:44` 把研究阶段语义留给上层；“阶段结束报告”仍应由协调层组织。证据：Q2；`C/jobs.py:372`、`:375`；`B/docs/constraints.md:18`。

**若增加总预算闸门，必须先补齐以下语义，否则原建议不安全也不够完整：**

| 必须确定的语义 | 建议及依据 |
| --- | --- |
| 生命周期总额还是滑动窗口 | 新增明确的全项目总额，不偷换已有 `budget_windows`；否则早期请求移出窗口就丢失总预算约束。`C/policy.py:114`、`:143`；`B/docs/constraints.md:34`。 |
| 提交时如何防止并发超支 | 用“已结束已知实际量 + 未结束/不确定作业的保守预留 + 本次完整请求”校验，在现有提交锁内原子复核；运行中用完整预留时不要再加一次同作业部分实际量。仅看累计 actual 会漏掉在途承诺；仅累计历史 requested 又会把已结束作业未用完部分永久占住。`C/submit.py:455`、`:474`；`M/DESIGN.md:149`。 |
| 失败、取消、未知用量 | qsub 明确拒绝不占实际预算；已运行失败仍计用量；确认未运行与历史缺失分开处理；缺失实际量不能默认零，不确定提交在对账前保留风险预留。`C/policy.py:126`；`C/states.py:69`；`M/DESIGN.md:429`；`L/jobs/3787063/record.json` 为 no-start、无 usage。 |
| 折算的含义与维度 | 保存原生 measured usage，另给带规则来源的预算派生值；不是把 MIG 小时改写成 measured node_hours。仅按 unit 映射不能推广到所有系统：G/C 都可用 node_hours，而站点 token 系数不同，互动队列也有例外。[站点 token 说明](https://www.cc.u-tokyo.ac.jp/supercomputer/miyabi/service/token.php)。`C/states.py:37`、`:44`；`C/export.py:39`。 |
| 阈值提醒 | summary 的 80% 标记是被动可见；主动提醒需明确谁接收、何时触发、如何去重，不能默默新增外部通知。项目既有约束是先报告，超出前获授权，不是 80% 自动停止。`B/docs/constraints.md:34`；`M/DESIGN.md:151`。 |

**优先级：** 折算说明、coverage 和 summary 正确性 P2；若用户明确要求机器强制执行项目硬上限，总预算闸门 P1。建议推迟通用汇率式预算引擎，先为已知的原生单位和明确系统范围提供一种一致算法。

**3.b usage 示例。** 建议正确且足够小，P2。README 目前只有工具表和政策示例，没有完整的 Observation JSONL 示例；应分别解释原生记录中的 `usage.quantity/unit`、smooth-research Observation 中的 `usage.accounting_ref`、省略 usage 的情形、以及 measured zero。例子还需区分 MCP 的 JSON envelope（`observations` 数组）和 CLI `--jsonl` 的逐行对象，cursor 写 stderr，避免把 stdout/stderr 合并后当观测导入。证据：`M/README.md:67`、`:103`；`C/export.py:34`；`C/cli.py:161`；`M/DESIGN.md:826`。

**3.c “两条写同一行 / Extra data”。** 未取得 11-01 原始损坏字节、生成命令及下游调用，不能认定 exporter 有换行 bug。合法多行 JSONL 交给仅接受一个 JSON 文档的解析器也会报 Extra data；`raw_decode` 能读过并不证明缺换行。当前 CLI 明确逐对象 `print(canonical(item))`，`canonical` 会转义字符串内换行；事件追加也显式写 `\n`，并受事件锁保护。证据：`C/cli.py:164`、`C/util.py:117`、`:172`、`C/ledger.py:102`。本次逐行读取 1,358 条账本事件全部可解析，其中 **1,353 条**属于导出种类；原文“当前 1,350 条”应补生成时间/cursor，不能代表当前完整账本。新增的 3 条可能来自最后一个作业 `3789321`，这是由末尾事件时间支持的推断，不是原导出文件的复原。证据：Q2；`L/events/2026-11.jsonl:1355`。

可以补一个 **CLI 输出契约测试**：至少两个对象、非 ASCII 及字符串换行、stdout 每行独立 JSON、末尾换行、stderr cursor 分离。现有 `M/tests/test_flow.py:166` 测映射、稳定性与 cursor，`:252` 测字段契约，不能替代 stdout 分帧测试。但应标为协议覆盖补充（P2），不要标成已复现缺陷，更不要在生产导入路径加入吞错的 raw_decode 来掩盖来源问题。本次未审核 smooth-research 的解析实现。

**4. 作业脚本（原文 P2）。**

**4.a 工作目录。** 现象和两次失败有直接证据。`L/jobs/3757929/script.sh:3` 直接运行相对路径，`L/logs/v0-env-probe-20261101-113643-a55184.log:1` 为文件不存在，record exit 127。`L/jobs/3761331/script.sh` 没有 cd，用 `$PWD/src` 和相对 pytest 路径；`L/logs/v2-f-smoke-20261101-170706-d1a5d1.log:42` 的 jobdir 为 `/home/x10041`，`:58` 的 `PBS_O_WORKDIR` 却正确指向快照；`B/runs/ATT-0054-1/tests.txt:1` 为相对路径找不到。故根因是脚本没有切换执行目录，不是 qsub cwd 没生效。

`M/DESIGN.md:541` 与 `C/tools.py:99` 定义的 workdir 本来就是 **提交 cwd / PBS_O_WORKDIR**；`/usr/local/share/man/man1/qsub.1:780` 也这样定义。批处理执行 cwd 不是这一参数的承诺。最小改进应在工具说明、dry-run 的说明性检查及 batch 示例里明确“脚本显式 cd”，并给出在 PBS 指令之后、相对路径之前的 `cd -- "${PBS_O_WORKDIR:?}" || exit 1` 或明确的快照绝对路径。

**不建议隐式前插 cd，也不建议解析任意 shell 的相对路径执行顺序。** 在首部插可执行语句会改变 shebang/指令布局；PBS 指令只在首个可执行语句前解析，现有代码也按此处理。还会改变依赖 HOME 起始目录的已有脚本、source/子 shell 行为，以及脚本摘要与实际提交字节的关系。若以后用户明确选择自动包装，必须成为可见的接口变更，最终脚本参与 dry run、摘要和存档，并保留有效 PBS 指令；本需求不需要为此增加包装层。启发式“cd 前相对路径”很难正确处理函数、变量、source、heredoc 和子 shell，简单提示比虚假的语义校验更一致。证据：`C/submit.py:55`、`:286`、`:502`；`M/DESIGN.md:560`。

**模板已具备关键保护，不应写成从零添加。** 三模板已有 `context.py --require-compute`，也已有显式 `cd "$PROJECT_ROOT"`：`T/assets/pbs/single-node.pbs:15`、`:30`，`torchrun.pbs:15`、`:53`，`mpi-workers.pbs:15`、`:48`（worker 内另有 cd）。这不是 MCP `allocation_check` 的完整 live PBS 检查，两者应区分；若换用后者，需考虑计算节点上 launcher/解释器可用性及 G/C 架构，不宜给所有模板强制增加服务安装依赖。`nvidia-smi` 可作为 G/MIG 专用、显式启用的采集片段，C/CPU 模板不应默认依赖它。证据：`M/README.md:110`、`:149`；`T/assets/pbs/single-node.pbs:14`。

**优先级：P2 合适。** 优先改解释与示例；保留脚本执行语义。

**4.b 注释占位符。** 代码行为已核实：`C/submit.py:26`、`:165` 对全文正则检查，因此示例注释中的 `<label>`、`<sha>`、`<pytest target>` 都会命中。该次失败请求没有保存在目前的有效 plan/submission 中，本次未取得原始拒绝响应；只能确认此例按当前代码会失败，不能复原发生次数。还存在 `allow_placeholders=true` 的现有显式覆盖（`:166`、`:169`），原文应提及，但不建议默认开它。

**“跳过注释行”不能直接落地。** `#PBS` 本身是注释形式却有提交语义；heredoc 中以 `#` 开头的一行是数据而非 shell 注释。全部去掉 `#` 行会漏检有效指令中的占位符，且与 `M/DESIGN.md:539` 现有“无残留占位符”的严格契约不同。最小先做行号/上下文诊断、说明显式覆盖；如要放宽，仅对有把握识别的普通注释降级为 warning，保留有效 `#PBS`、命令和 heredoc 数据的检查，不能用 `lstrip().startswith('#')` 假装 shell 解析。即使不放宽也可由作者改写说明性注释，代价很低。测试应覆盖普通注释、PBS 指令和 heredoc 边界，而不只是一个示例字符串。**P2，放宽范围需明确决定。**

**5. job_list 部分字段 null（原文 P2）。**

“先记录请求再复现”的态度正确；没有具体字段和原响应，不能把它列成已确认丢字段 bug。当前至少存在四种不同原因：

| 情况 | 代码证据及判断 |
| --- | --- |
| 列表本来只有表格摘要，详细字段在 job_get.detail | `C/parse.py:87`、`C/jobs.py:112`、`:185`。不能要求列表默认等价于逐个 qstat -f。 |
| 请求未知字段或 `ledger.usage` 这种点路径 | `C/jobs.py:143` 只做顶层 `job.get(k)`，无字段名验证/嵌套投影，必然返回 null。这是确定的易用性缺口，但未证明是 11-01 的原因。 |
| 等待中的时间/用量尚不可知 | `C/parse.py:36`、`:44`、`:58`、`:67` 将 PBS 缺失符转为 None；`C/states.py:84` 仅终态附 usage，属于正常语义。 |
| 其他项目的作业 | job_list 只取当前 Store 的 ledger；job_get 还会按 MIYABI_PROJECT_DIR/index 找归属并读取另一项目记录。`C/jobs.py:114`、`:176`、`:180`。因此同一作业在二接口上 ledger 覆盖可以不同。 |

建议先补顶层字段说明、常见 null 原因及非法字段诊断；再用同一作业、同一项目、相近 observed_at、无 fields/带 fields 四组响应定位。不要给 job_list 每个成员补昂贵 detail 请求，也不要把所有 null 改成 0/空字符串。**P2 合适；历史 incident 本身暂不进入修复清单。** 证据：`M/DESIGN.md:133`、`:148`；`C/tools.py:70`。

**“值得保留的做法”逐项审核。**

| 原文条目 | 审核意见 |
| --- | --- |
| dry run + plan_digest 的两步提交 | 保留。`C/submit.py:428`、`:444`、`:474` 实现计划/摘要/时效/提交时复查。458 个接受、1 个 qsub 拒绝是 submission 样本事实，不含 dry-run 拒绝数，不能作为护栏完整性或研究成功率证据；还有 O7 的 notify 未绑定摘要问题。 |
| 一个通知覆盖一组作业，结束回合等待 | 保留。`C/notify.py:121`、`:161`、`:311`，`M/tests/test_notify.py:33`、`:63` 支持分组和封口。实际例子 `L/notifications/N-20261103-160419-5dfa.json` 含多作业及 early/final 消息。应补前提：已封口、投递方式适合代理、客户端接收路径已就绪；不能只创建对象就宣称一定唤醒。 |
| smooth-research 导出带 attempt_id | 条件成立：仅有 `client_ref` 的事件带 attempt_id，代码 `C/export.py:27`，契约 `M/DESIGN.md:811`。本账本 458 个已提交作业中 438 个有 client_ref；1,353 条可导出状态事件中 1,295 条带 attempt_id。不能笼统说每条都能直接绑定 attempt，也不应给没有研究 attempt 的维护作业伪造 ID。Q2。 |
| 代码可变性提示让所有作业都运行于 git archive 快照、都能追到提交 | 保留提示，删掉全称和因果保证。`C/submit.py:273` 仅检查 workdir/脚本父目录是否可写，产生 warning，不递归验证源代码、依赖、环境，不创建 git archive 或强制拒绝。459 条记录中 436 条 workdir 含 `/snapshots/`，没有一条 labels.code_commit；这不否定脚本可能另有绝对快照路径，但足以说明不能由该检查得出“全部受保证”。vnode 探针的 workdir 为项目根、无 client_ref，只有 nvidia-smi，无代码提交绑定。真正已归档的是提交脚本（`C/submit.py:502`），应用源码的冻结由调用方完成。`M/tests/test_flow.py:108` 明确测试的是 warning。 |

**原文遗漏但证据支持的问题。** 以下均未在本次触发故障或修改实现；注明“静态确定”的项应作为后续针对性验证目标，而非冒称本项目已发生的事故。

| ID / 优先级 | 具体失败路径、证据与最小修正方向 |
| --- | --- |
| **O1 / P1：项目级 poll 错误可使 daemon 假装成功并空闲退出** | `C/notify.py:334` 先写心跳，`:364` 捕获项目的 MiyabiError 放进 `errors`，`:366` 仍 `ok=true`。失败项目尚未增加 open_count/targets；`C/notifyd.py:211` 后忽略 errors，清零失败计数、用本轮 targets 覆盖 known，并按 open_count=0 计空闲。若有待处理通知的项目连续两轮失败且没有其他 open 项，daemon 会 `exited_idle`，不发 broken_notice。**静态确定**，不等于已定位原文两次退出。应把“无法判断是否有待处理项”与“确认无待处理项”分开；保留失败项目的 targets，显式报告/重试错误，只有成功确认空闲才递增 idle。`M/tests/test_notify.py:167` 仅覆盖 transport 失败，未覆盖此部分失败路径。 |
| **O2 / P1：轮询配置是两套来源** | 见第 2.2 节。服务端显示的 notify_poll_seconds 与真实 CLI/Options 不连通，也影响 daemon_running 的判断。应先修接线、实际配置报告与存活实例不匹配提示，再谈更短间隔；证据 `C/notify.py:75`、`C/cli.py:42`、`C/notifyd.py:143`、`:236`。 |
| **O3 / P1：其他代理照服务器说明只启动 daemon，默认 listener 仍无人消费** | `C/server.py:43` 指示非 Claude 运行 start_command，但没有像 `M/README.md:207` 那样明确要求选择适用 deliver。`C/deliver.py:24` 默认 listener，`:48` 仅写 inbox 就返回 delivered；daemon 活着也不能证明代理能被唤醒。153 条全是 listener、20 条 delivered 未 ack 是需要检查消费路径的证据，不能直接归因为全部漏投递。先使服务器 instructions/返回步骤完整表达“选通道 + 启动 + 接收”；保留一通知一个明确消费方的约定，避免协调者和研究者竞抢同一 listener。 |
| **O4 / P1：unknown 被称为 ended/started，通知可能过早结束观察** | `C/notify.py:19` 把 unknown 放进 SETTLED，`:313` 因而可输出 “all jobs ended”，`:453` 可让 job_wait.met=true；`started` 也接受它。活动/历史暂时找不到达到 missing_s 后，`C/observe.py:138` 会产生 unknown。**静态确定**，违反 `M/DESIGN.md:148` 的“未知不推断”。应让 unknown 引发明确的不确定性告警/人工处理提示，不能把正常完成条件说成满足，也不能悄悄关闭后续观测。 |
| **O5 / P1：early 消息提供 final 级 ack token，状态机不一致** | `C/notify.py:280` 提示组仍等待，但 `:284` 对 early/final 都附同一个 ack_token；`:242` 在 open 时拒绝 ack。若代理遵照通知立即确认，会收到 invalid_state；若旧 early 的 ack 延迟到 final 已 fired 时到达，`:245` 会将整个通知 acknowledged，使 poll 的 OPEN 过滤跳过尚待投递的 final。**静态确定**。本项目 4 个通知含 early（如 `L/notifications/N-20261101-231228-4c99.json`），但本次未证明发生后一个竞态。最小修复是不让 early 携带可确认 final 的操作，明确仅最终通知需 ack；若需要 early receipt，再设计按 message ID 的确认。现有 `M/tests/test_notify.py:73` 没有在 early 后 ack 的测试。 |
| **O6 / P1：合法 2/4 MIG 请求被当作节点数拒绝** | Q4 的批处理 MIG 队列为 `MIN_NODE=MAX_NODE=1`、`AVAIL_MIG=1,2,4`。`C/parse.py:173` 分别保存两者，但 `C/submit.py:115` 用 select_units 与物理节点 min/max 比较，没有使用 mig_sizes。故 select=2/4 在 shape 检查就失败；这是**合法请求被拒**，不是简单“漏拦 vnode”。离线 fixture `M/tests/fixtures/rsc_x.txt:15` 已有这个数据，但 `M/tests/test_parse.py:72` 只测解析、预算测试只提交 1 MIG。应按队列类型选择 MIG 可用数量集合或节点范围，并验证 1/2/4 可用、3 不可用。项目 459 条均 units=1，因此未被该项目覆盖。 |
| **O7 / P1：计划摘要未绑定 notify 的外部效果** | `C/submit.py:34` 的 STABLE 无 notify，`:284` 的 plan 也无该 spec；但 commit 在 `:535` 使用调用时的 notify 创建/加入通知。**静态确定：** 同一脚本/资源摘要下可以改变通知目标、方法或分组，仍通过 digest 检查；两步提交不能被描述为锁定了整个请求。应把通知意图及目标的规范化表示纳入计划和摘要，动态分组状态仍在提交时复核。依据 `M/DESIGN.md:531` 的“同样的参数/提交的是看过的计划”；已有测试 `M/tests/test_flow.py:40` 只改 walltime。 |
| **O8 / P2：summary 请求量包含 submit_failed，与预算窗口口径不一致** | `C/jobs.py:372` 对所有记录累加请求，`C/policy.py:126` 却排除 submit_failed。本项目恰好多计 0.016667 MIG-h（Q2）。可以保留“尝试申请量”作为单列，但默认 accepted/reserved/requested 名称和预算范围必须明确，否则用户会把未被 PBS 接受的成本当预留。应增加 usage coverage：457 有值、456 正值、1 个已提交 no-start 无值、1 个未获 ID。 |

O1、O2、O3、O4、O5 优先于自适应轮询、改变 listener 退出码或“自动清理未确认”。这些项分别可能导致静默失联、错误健康判断、没有消费方、完成语义失真或 final 消息被跳过；它们有具体代码路径，原文的两次未定位退出不能取代这些检查。

**应删除、改写或推迟的建议。**

| 处理 | 原建议 / 表述 | 理由与替代 |
| --- | --- | --- |
| 删除直接推导 | 按 exec_vnode 的 `_migN` 后缀确定 profile | 当前 PBS 字段没有该后缀；使用作业内设备测量，保留原始调度事实，映射须有站点依据。Q3；第 1 节第 2–4 项。 |
| 改为过渡安排 | hardware 永久不变即可解决 | 不能永久将 1g 表述为 2g；历史字节保留，新事实与记账标签分离，契约迁移显式决定。`M/DESIGN.md:824`、`:836`。 |
| 删除无条件服务端动作 | job_submit/notify_create 自动 ensure；因未启动 daemon 拒绝 dry run | 与客户端投递拓扑、先登记后启动流程冲突；返回健康证据与客户端操作，修 O1/O3。`M/DESIGN.md:652`、`:653`。 |
| 推迟 | 按 walltime 自适应、每通知独立轮询 | 先修实际生效的间隔与缓存协调，测延迟/调用量；walltime 不是结束预测。`C/notifyd.py:236`、`C/observe.py:112`。 |
| 推迟 | 为每种 listener 闭合原因增加退出码 | 当前没有接收者证据；先补 reason/时间/75 文档，避免无需求扩大 CLI 契约。`C/notifyd.py:258`、`:283`。 |
| 改写 | listen 是统一取 token 的办法 | 已消费消息不再匹配 `*.json`，且 listen 有 ensure 副作用；需明确消息恢复流程，不在列表泄露 token。`C/notifyd.py:262`、`:271`。 |
| 推迟通用化 | 跨系统统一单位预算引擎 | 当前全为 MIG，summary 折算足够解决手算；硬闸门先确定 lifetime、预留和缺失语义。`B/docs/constraints.md:34`；第 3.a 节。 |
| 删除隐式修改 | 提交时自动前插 cd | 改变脚本/指令和摘要契约；明确 workdir 语义，复用已有模板。`C/submit.py:55`、`:502`。 |
| 删除宽泛放行 | 跳过所有注释行 | 会漏检 `#PBS` 和 heredoc 数据；先提供行号诊断。`C/submit.py:55`、`:165`。 |
| 降为待复现 | exporter 缺换行、job_list 曾丢字段 | 没有具体原始响应，Extra data/null 有其他正常或客户端解释；保留协议测试及最小复现要求。第 3.c、5 节。 |

**需要用户决定或向站点确认的事项。** 这些是后续设计决策清单，不是本次索取授权，也未执行相应变更。

| 对象 | 选项及取舍 | 建议与证据 |
| --- | --- | --- |
| 用户 / 下游接口维护者：hardware 的后续契约 | A：先保留旧导出，用独立执行元数据和明确的 legacy 说明修正解释，兼容性好但错误标签仍存在。B：新事件使用真实/中性标签，历史通过带证据的更正与下游迁移处理，语义正确但需要处理累计桶和去重。 | 推荐 A 作为立即过渡，随后有计划地选择 B；禁止原地重写历史。`M/DESIGN.md:824`、`:826`；`C/export.py:31`。 |
| 用户：预算只是报告还是必须由 MCP 强制执行 | A：原生 summary + 项目折算与 80% 报告，最简单，但依赖协调层。B：项目 lifetime 预算 + 并发预留硬闸门，约束更可靠，但必须决定未知成本、adopted/外部作业和授权例外。 | 当前需求可先 A；如果目标是防止并发作业越过 200，需 B 的完整算法，不能以请求窗口冒充实际总额。`B/docs/constraints.md:34`；`C/policy.py:123`。 |
| 用户：通知延迟与进程生命周期 | A：按需启动/空闲退出，成本小，必须保证每次客户端 handoff。B：代理侧外部 supervisor 持续管理，恢复更强但引入运维依赖。间隔可保留 300 或试验 60–120 秒，前者负载低，后者短作业响应快。 | 先修 O1/O2/O3 并测量，再选监督方式及默认间隔；本次未启动 supervisor。`M/DESIGN.md:652`；`C/notifyd.py:191`。 |
| 用户：通知消费归属 | A：协调者单独持有每个分组 listener，再处理后续任务。B：研究者独占自己的通知，协调者只查状态。若确需双方接收，应为两接收方建独立通知/明确广播语义。 | 一个 listener inbox 竞争消费不能当广播。`C/notifyd.py:273`；原文第 2.3 节及 Q2。无需先做“多退出码”。 |
| 用户：占位符误报处理 | A：保留严格规则，改善行号和覆盖说明，改动最小。B：普通注释降级为 warning，体验更好但需能正确识别 `#PBS` 和 heredoc。 | 推荐先 A；B 是明确放宽契约，不能按“所有注释无害”实现。`M/DESIGN.md:539`；`C/submit.py:165`。 |
| 站点：PBS execution slot 与 GPU profile 的映射 | 请求确认 `exec_host` 的 `/N` 是否稳定对应 MIG 编号，是否会重配置，能否取得直接且可保留的 allocated MIG UUID/profile。 | 在确认前只把 raw PBS 字段当调度事实，profile 来自计算节点测量。Q3；`M/DESIGN.md:427`。 |
| 站点：资源预检与查询负载 | 是否有普通用户可访问、无副作用且稳定的资源定义/校验接口；可接受的共享 qstat 频率及调用限制。 | 没有保证时使用明确支持的资源集合和全局有界轮询，不能通过试投获得“检查”。`/usr/local/share/man/man1/qsub.1:312`；`M/DESIGN.md:150`。 |
| 站点：特殊计费例外（若项目确实需要） | 已有公开规则可用于普通批处理 MIG 的 0.25；若账户/服务有特约、需要精确对齐四舍五入后的 TOKEN，再查询站点。 | 不必把“1g/2g 是否等系数”当成阻塞项；项目约定的 0.25 已独立生效。`B/docs/constraints.md:34`；[官方 token 规则](https://www.cc.u-tokyo.ac.jp/supercomputer/miyabi/service/token.php)。 |

**原文所有显式 path:line 引用的核对索引。** 以下按 3da7876 的实际行号核对，没有发现整批行号漂移；主要问题是引用能支持的结论范围。

| 原文引用（原文行） | 对位结果 |
| --- | --- |
| `DESIGN.md:836`（19、31） | 准确对位固定 MIG hardware 的决策；这条设计假设已被产物反证，不能成为继续虚报真实 profile 的理由。 |
| `states.py:40-43`（19） | 准确对位 MIG 用量及标签生成；用量计数与硬件标签应分开评价。 |
| `states.py:33`（19） | 准确对位读取 exec_vnode；未持久化的责任还在 from_detail/ledger 白名单。详细查询已有输出字段，见 `C/states.py:105`。 |
| `notifyd.py:288-289`（41） | 对位 ensure；60 秒条件在 `:278`，此前可能 exit 3，不能概括成无条件 supervisor。 |
| `notify.py:75`、`notifyd.py:26`（46） | 都是 300 的默认来源，但分别属于服务端信息和 daemon Options；不能推出改前者会控制后者。 |
| `notifyd.py:283-287`（51） | 准确对位模糊提示和 return 3；没有接收方身份数据。 |
| `cli.py:4`（51） | 确为 0/1/2 的模块说明，但 listener 自身另记 3/75；应修 CLI 公开说明，而非说代码完全没有定义例外。 |
| `policy.py:139-157`（61） | 准确对位单位内时间窗请求量检查；不是项目实际总成本的实现。 |
| `submit.py:510-511`（68） | 准确对位以 workdir 作为 qsub cwd，符合 `M/DESIGN.md:541`；不是自动设置脚本执行 cwd。 |
| `submit.py:26`、`:165-167`（70） | 准确对位正则与全文检查；应同时提及 `:168-169` 已有显式覆盖，并保留有效 PBS 指令的检查。 |
| `submit.py:273-278`（81） | 准确对位 warning；不创建快照、不拒绝可写源码、不验证所有引用依赖，不能支撑“所有作业都可追溯”的保证。 |

原文另外引用但未给行号的 `B/docs/constraints.md`，预算相关内容已定位到 `:33-37`；其 smooth-research 改进报告引用按要求排除。研究事件 ID 的审计边界见第 1 节第 9 项。

**命令证据与验证边界。**

Q1：运行 `git status --short`、`git rev-parse HEAD`、`git log -1 --format='%H %aI %cI %s'`。起始状态只有 `?? reports/`；HEAD 为上述完整提交，author/committer 均为 `2026-10-31T16:35:42Z`。运行技能的只读 `context.py --target-system Miyabi-G` 返回：`hostname=miyabi-g3`、`architecture=aarch64`、`node_role=login`、`allocation_confirmed=false`、`runtime_execution_allowed=false`。

Q2：用 `/usr/bin/python3 -I` 的标准库在 `timeout 20` 内读取直接目录和逐条小 JSON；先检查文件规模，未递归扫描训练数据或加载模型。计数取 `L/submissions/*/record.json`；请求量取整数 `units * walltime_s` 后统一除 3600，实际量取已存的 `usage.quantity`；walltime 分布从 `usage.source_ref` 中的原始秒数还原。逐行 `json.loads` 检查 `L/events/2026-11.jsonl`，按 `C/ledger.py:22` 的种类集合筛选；未调用会刷新账本的导出 CLI。关键输出为：

```text
submissions=459; jobs=458
status: submitted=458, submit_failed=1
outcome: success=415, failed=42, cancelled=1, no_state=1
requested_all=279.042222 MIG-h; requested_accepted=279.025556 MIG-h
usage_records=457; positive_usage=456; used=152.992509 MIG-h
all walltime median=239s; positive median=241s; positive <=600s:287; <=900s:313
all submissions units=1; queues: debug-mig=296, short-mig=163
notifications=153; acknowledged=132, delivered=20, cancelled=1
delivery: listener=153; notifier: miyabi-g2=153
events=1358; unique event IDs=1358
event kinds: submitted=458, running=437, ended=458, annotated=5
exportable state events=1353; with client_ref/attempt_id=1295
submission actor: codex=325, claude-code=134
snapshot-looking workdir=436; labels.code_commit present=0
```

Q3：`timeout 25 qstat -H -f 3787033 3766228`（只摘与本审核有关的字段，退出 0）：

```text
Job Id: 3787033.opbs
resources_used.walltime = 02:06:24
exec_host = mg1112/3*18
exec_vnode = (mg1112:mem=26214400kb:ncpus=18:ngpus=1:nvnodes-mig=1)
Exit_status = 0
TOKEN = 0.5
Job Id: 3766228.opbs
resources_used.walltime = 00:00:01
exec_host = mg1109/3*18
exec_vnode = (mg1109:mem=26214400kb:ncpus=18:ngpus=1:nvnodes-mig=1)
Exit_status = 3
TOKEN = 0.0
```

Q4：`timeout 20 qstat --rsc -x`（退出 0）的 MIG 行为：

```text
QUEUE        MIN_NODE MAX_NODE AVAIL_MIG MAX_ELAPSE
debug-mig    1        1        1,2,4     00:30:00
short-mig    1        1        1,2,4     08:00:00
regular-mig  1        1        1,2,4     48:00:00
interact-mig_g1 (routing destination): 1 node, AVAIL_MIG=1, 02:00:00
```

Q5：从事件、通知 history 及 daemon 文本日志交叉读取 `3789152`：

```text
PBS ended              2026-11-03T19:29:42+09:00
ledger observed        2026-11-03T19:34:10+09:00
notification delivered 2026-11-03T19:35:16+09:00
notification ack       2026-11-03T19:35:50+09:00
```

**本次没有运行项目测试套件、fake PBS 流程、GPU 探针或通知集成测试。** 当前为登录平面，且任务禁止提交/取消作业及启动、停止、ensure notifyd；通知测试本身就会启动 daemon/listener（`M/tests/test_notify.py:125`、`:167`）。已完成的是静态实现/测试覆盖审阅、账本统计与逐行 JSON 解析、两项有界只读 PBS 查询和官方站点规则核对。后续实现应在授权的验证环境中覆盖本文列出的具体失败路径、旧导出字节稳定性及 CLI 兼容性；本报告不将这些预期行为写成已通过的测试结果。
