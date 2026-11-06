# miyabi-mcp 设计建议

状态：草案，供讨论（2026-10-30）。本文只含设计，不含实现。"已验证"指在 Miyabi 上有实测记录；"未验证"指只有设计或离线测试。站点数字都带日期，会过时，决策前请重新查。

## 0. 摘要

1. **定位。** 把 miyabi-toolbox 中可执行的 PBS 能力做成一个 MCP 服务（本目录 `miyabi-mcp/`），给 Claude Code、Codex、pi 等 agent 提供结构化、可审计、带护栏的 Miyabi 作业接口。四个 skill 保留为知识（作业脚本模板、实验平台方法论、站点规则），脚本迁入服务。
2. **服务端始终运行在 Miyabi 登录节点上**，因为 PBS 命令、作业日志和共享文件系统都在那里。agent 在登录节点上时直接以 stdio 启动；agent 在工作站上时，由 pi-env-mcp 在登录节点上托管同一个服务（`pi-env-mcp mcp add`），服务跑在 pi-env-mcp 已有的连接里，agent 只注册 pi-env-mcp（5.1、5.3）。分工不变：pi-env-mcp 管连接、文件、终端和传输，miyabi-mcp 管 PBS 语义；miyabi-mcp 的代码不依赖 pi-env-mcp，在登录节点上也可以直接以 stdio 启动。
3. **运行环境。** 服务运行在 `miyabi-mcp/.venv` 中（uv 管理，Python ≥ 3.11）。在 Miyabi 上以系统自带的 `/usr/bin/python3.11` 为基础建 venv，不下载解释器，也不往 `$HOME` 写。MCP 协议层用官方 Python SDK，核心逻辑只用标准库。每个工具都有对应的 CLI 子命令，输出同一份 JSON，所以只有 shell 的 agent（例如经 pi-env-mcp 的 `bash`）也能用。
4. **v0.1 覆盖前五项需求**：查询（`pbs_context`、`pbs_queues`、`job_list`、`job_get`）；提交（`job_submit`，先 dry-run 出计划，再凭计划摘要提交）；取消（`job_cancel`）；记录与管理（作业台账、事件日志、查询与导出）；通知（`job_wait` 有界等待；作业组通知、agent 所在机器上的 daemon 投递、确认）。第六项（pilot 占位排队与任务打包）排在 v0.3。
5. **通知（原称"唤醒"）。** agent 等作业时可能正在做别的事，所以这里是"通知"而不是"唤醒"。
   - agent 每次登记或加入通知后，都在自己所在的机器上运行返回的命令（`ensure` 是幂等的，不以"已在运行"为由跳过，9.3）。daemon 按 `notify_poll_seconds`（默认 180 秒）经 miyabi-mcp poll；条件判定仍在登录节点完成（共享的 qstat 快照加台账），投递在本机进行：Claude Code 用后台 listener，另有 herdr、tmux、codex_queue、具名命令和文件。
   - 一个通知可以绑定一组作业（如多个 seed），全部结束后发一条汇总通知；可选在任一成员失败时提前通知。
   - 通知通道中断（ssh 断开等）本身也会通知 agent，不会静默。
6. **台账。** 每个作业一份 JSON 记录，另有按月追加的事件日志。台账、脚本快照、PBS 日志等必须留在 Miyabi 上的产出，都放在项目目录下的 `.miyabi-mcp/` 中。项目目录默认是 `/work/<group>/<user>/<project>`，用户也可以另行指定。写入用原子 rename 和跨节点安全的锁文件。不用 SQLite，因为几台登录节点（g1、g2、g3）上可能同时有多个写者。事件可以 1:1 导出为 smooth-research 的 Observation/Usage。
7. **护栏。** 提交和取消默认 dry-run；策略文件设上限（每作业节点数、walltime、node·h，每个标签的并发，保留的 accepted 槽位）；幂等键防止重复提交；结果不确定时先对账再决定是否重试；默认只取消本项目台账内的作业；FINISH 不等于成功。

## 1. 目标与范围

### 1.1 目标

1. agent 无论在登录节点还是远端，都用同一组工具操作 Miyabi PBS。
2. 把站点方言（`qstat -ll`、`-H`、`-f`、`--rsc -x`、`--limit`、`--rscuse`、`show_token`）变成稳定的 JSON。失败要显式：沿用 `qstat_json.py` 的约定，返回 `ok:false` 和诊断，不把失败当成空列表。
3. 写操作（qsub、qdel）可审计、可复核、幂等，并且不超出用户授权的范围。
4. 长作业期间 agent 不占着回合；作业或队列状态变化时，可靠地通知正确的 agent，不论它正忙还是已经结束回合。
5. 为上层（smooth-research、实验控制器、编排 agent）提供稳定的作业记录。

### 1.2 版本范围

| 需求 | v0.1 | 之后 |
| --- | --- | --- |
| 1 队列与作业查询 | `pbs_context`、`pbs_queues`、`job_list`、`job_get` | `usage_report`（移植 `pbs_time_report.py`） |
| 2 提交 | `job_submit`（dry-run → 提交，可同时加入通知） | 作业内标记 hook；用 `qalter` 调整排队中的作业 |
| 3 撤销 | `job_cancel` | `job_hold`、`job_release` |
| 4 记录与管理 | 台账、事件日志、`ledger_query`（含导出）、`job_annotate`（含接管外部作业） | 归档压缩、报表 |
| 5 通知 | `job_wait`；`job_submit(notify=…)`、`notify_create/update/list/cancel/ack`；agent 所在机器上的 `notifyd`（listener、herdr、tmux、codex_queue、command、file） | codex_goal；herdr、tmux 的实测；channels（实验） |
| 6 打包与占位（可选） | — | v0.3 pilot pool（第 12 节） |

### 1.3 非目标

- 通用远程执行、文件读写、交互终端：这是 pi-env-mcp 的职责。miyabi-mcp 用 `interactive_plan` 校验交互分配，CLI `interactive-start` 在 pi-env-mcp 的 PTY 内运行并记录 `qsub -I`；终端输入输出仍通过 `terminal_*`（见 8.3.1）。
- 研究语义（RunSpec、attempt、结果是否有效）：这是 smooth-research 的职责。
- 在登录节点上跑工作负载：服务不提供任意命令执行工具。
- 实验专用的控制器逻辑（spec 展开、science version、选择与 TPE、统计）：留在 ml-experiment-platform skill 作为方法论。
- 通用 OpenPBS/Torque 适配：只针对 Miyabi 方言，和现有脚本一致。

## 2. 现有内容整理

### 2.1 清单

| skill | 内容 | 可执行部分 | 验证状态 | 在 MCP 中的去向 |
| --- | --- | --- | --- | --- |
| miyabi-pbs-cmd | 操作平面判定、登录节点执行边界、qstat/qsub 方言参考 | `context.py`（主机与分配判定）、`qstat_json.py`（作业表与历史 → JSON） | 在 miyabi-g1 上对 live 输出验证（09-05；09-29 解析 31 天 1,185 行历史） | 两个脚本并入 `pbs/` 和 `context` 模块；规则精简后放进 MCP `instructions`；参考文档留在 skill |
| miyabi-pbs-goal-wakeup | 长作业休眠与唤醒流程 | `job_wakeup.py`（timer/pbs 事件、`codex queue`、Goal RPC、Claude 后台 watcher、状态机），外加 `qstat_json.py` 副本 | Codex 0.158 实测（09-29）；Claude Code 2.1.284 两例（09-29） | 状态机和 Codex 投递代码移入 `watch/`、`deliver/`；"一作业一 poller"改为共享轮询和多作业条件 |
| miyabi-pbs-shell-template | 三个作业脚本模板；模块、Python 环境、存储、MPI、torchrun、Accelerate、vLLM 参考 | 无（模板） | 离线测试；G 登录节点上只验证了拒绝路径 | 保持为 skill；模板可加入可选的作业内标记 hook（v0.2） |
| miyabi-ml-experiment-platform | 晚绑定 pilot、walltime、成本闸门、存储、实验流程等方法论（约 4,850 node·h 的经验） | `pbs_time_report.py`、`assets/pilot.pbs` | time report 对 live `qstat -H -f` 验证 | time report → `usage_report`（v0.2）；pilot 协议的通用部分 → pilot pool（v0.3）；方法论留在 skill |

### 2.2 现有形态的局限

1. **没有提交和取消的封装。** 各项目自己写提交器（一个项目的 `scripts/submit.py`，平台的 `<pkg>/control/pbs.py`），上限检查、台账、作业标记各写一遍。
2. **没有统一的作业台账。** `.job-wakeup/` 跟着任务目录走，各项目自建的台账（如 `submissions.jsonl`）格式各异、位置不定。
3. **唤醒是一作业一 poller**（默认 300 s），没有"全部结束/任一结束"、队列状态和超时条件。Claude watcher 绑定会话，会话结束后事件停在 `waiting`。除 Codex 外不能唤醒其他 agent。
4. **`qstat_json.py` 在两个 skill 里各有一份**，需要手工 `cmp` 同步。
5. **入口不便。** agent 要先定位 `SKILL_ROOT` 再拼长命令；工作站上的 agent 没有直接入口。

## 3. miyabi-g 上的使用记录

按项目的细节（项目名、作业号、路径、时间线）记在本地文件 `no-git/usage-records-2026-10-30.md`，不入库（见本目录 `.gitignore`），与仓库"不含项目专属内容"的做法一致。这里只保留与设计有关的事实。

### 3.1 调查方法

2026-10-30 经 ssh 做只读调查，登录节点上只跑有界查询。来源：Claude Code 和 Codex 的 prompt 历史，深度不超过 4 的 `.job-wakeup/` 事件目录，一个多 agent 项目的提交台账和编排会话日志，以及站点命令的实际输出。两种 agent 的会话日志合计约 3 GB，没有全量扫描。

### 3.2 安装与使用面

- 自 10-20 起，Claude Code、Codex、pi 三套 skills 目录都以软链接指向 `/work/<group>/<user>/miyabi-toolbox/*`。
- 9 月底到 10 月底，约十个项目在 prompt 里点名用过这些 skill：skill 开发与测试、vLLM 模型部署、研究 agent 原型、联邦学习实验（实验平台 skill 的经验来源）、LoRA 基线、ComfyUI 部署（herdr 工作板任务）和一个多 agent 研究项目。
- 用户在使用中提出的规则修订：
  - 作业排队或运行期间先并行推进其他工作，只有必须串行等待时才休眠（09-30）。
  - debug-g 的 30 分钟作业没留余量，全部超时，walltime 要留宽。
  - 环境安装可以在目标系统的登录节点上进行（已改入 cmd skill）。
  - 不必占着交互作业，改为提交批处理作业后休眠。

### 3.3 唤醒事件统计（7 个 `.job-wakeup/` 目录，共 38 个事件）

| 来源 | agent | 事件 | acknowledged | cancelled | 要点 |
| --- | --- | --- | --- | --- | --- |
| LoRA 基线项目（10-08） | Codex | 18 | 8 | 10 | 01:52 一分钟内登记 12 个，各自一个 300 s poller；这 12 个作业都先处于 HELD；其中 6 个 watch 在作业 RUNNING 时被撤销；1 个用 Goal 模式 |
| 多 agent 项目的两个 worker（10-26→27） | Codex | 15 | 12 | 3 | 等待 0.1–1.9 h；worker 关闭前按编排者要求撤销 |
| goal-wakeup 原型（09-29） | Codex | 2 | 2 | 0 | 20 s timer；一个真实作业的 Goal 恢复：PBS 16:30:19 结束，16:31:19 发现（poll 180 s） |
| 联邦学习验证（10-07） | Codex | 1 | 1 | 0 | |
| 模型部署、研究 agent 原型 | Claude Code | 2 | 0 | 2 | 都在 HELD 或 QUEUED 时撤销，没有一次投递 |
| **合计** | | **38** | **23** | **15** | Goal 模式只有 2 个；其余 Codex 事件是 `codex queue` 消息模式 |

- 从投递到 agent 执行 `ack`：10.6–185.6 s，中位约 22 s。检测延迟的上限就是 poll 间隔（300 s）。
- 出现过成批 HELD 的作业，也出现过"FINISH 但从未开始"的作业。其中一个是依赖 12 个上游作业的汇总作业：有一个上游以 exit 1 结束后，它没有运行就结束了（推测是 afterok 不满足后被删除）；另一个的预计开始时间排在维护之后，随后被删除。两次唤醒都照常送达，但消息里没有 Exit_status，agent 要自己判断"从未运行"。
- Claude Code 侧在记录中可见的长等待，是编排者自写的后台 watcher 完成的（见 3.4），不是这个 skill。

### 3.4 一个多 agent 项目自建的提交器、台账和 watcher（10-26→30）

这个项目由 Claude Code 编排者带多个 worker（先是 herdr 里的 Codex，后来是会话内 subagent）。它没有直接用 goal-wakeup，而是自建了一套，正好说明 MCP 需要什么：

- **提交器。** 用户给每作业设的 16 node·h 上限在提交时检查（上限只用于检查，不作为申请值）；有队列白名单和 walltime 上限；代码绑定到 commit（`git archive`）或工作树；每个作业写一份 spec JSON；`qsub -o` 指向 /work 上的日志；追加一行到 `submissions.jsonl`；`--wait` 只用于几分钟的短作业。3.5 天共 650 个作业：debug-g 345、regular-g 305；绑定 commit 583、工作树 67；申请合计 303.1 node·h。
- **作业内标记。** 作业脚本在运行前写 `<log>.started`，结束时写 `<log>.exit`（退出码）。650 个作业中有 618 个两种标记都有。
- **等待协议。** worker 以一行 `WAIT <job ids> | <next action>` 结束回合。编排者为每一行起一个后台 `wait_jobs.sh`：每 120 s 对每个作业跑 `qstat -H -f`，全部为 `F` 才退出，并打印 Job_Name、Exit_status、walltime。退出时编排者被唤醒，再给 worker 发 "Job N (name) has left the queue, exit 0: <log>. Continue."。
- **队列 watcher。** 维护期间每 300 s 查一次 `qstat --rsc`，debug-g 恢复 START 后退出；另设一个定时 resume 兜底。
- **agent 停滞检测。** Codex worker 遇到瞬时模型错误会静默结束回合、闲置数小时，用户因此要求每 20 分钟检查一次（10-26）。10-29 用户又说明，会话内 subagent 的每次停止都会通知编排者，不需要这项检查。
- **并发约定。** 4 个 worker 共享项目的 16 个运行作业，每个 worker 同时最多 6 个。

### 3.5 站点实测（2026-10-30，miyabi-g1）

| 项 | 观察 |
| --- | --- |
| 登录节点 | `miyabi-g.jcahpc.jp` 解析到两个地址；有项目的作业从 miyabi-g2 提交（`Job_Owner …@miyabi-g2`）。服务端状态必须放在共享文件系统上，不能假设总连到同一台 |
| 系统与运行时 | RHEL 9.8；`/usr/bin/python3` 为 3.9.25。node、uv、herdr、Claude Code、Codex、pi 都是用户自装的 |
| PBS 命令 | `qstat`、`qdel`、`qhold`、`qrls`、`tracejob`、`show_token`、`show_quota` 是站点包装（`/usr/local/bin/.cltkexec`）；`qsub`、`qalter`、`qmove`、`qsig`、`pbs_release_nodes` 是原版（`/opt/pbs/bin`，PBS 2024.1.5） |
| qstat 方言 | 没有 `-F json`、`-u`、`-x`。有 `-ll`、`-H --hday --hnum`、`-f [-H]`、`--rsc -x`、`--limit`、`--rscuse`、`--nodeuse`、`--miguse`、`--system`。`-A`（组内作业）要求组管理员权限 |
| qdel/qhold 包装 | `qdel` 只接受作业号和 `-Wsuppress_email`；`qhold` 另有 `--fs`（文件系统 hold） |
| 陷阱 | `qstat -f <已结束作业>` 输出 "No unfinished job found." 且退出码为 0；每次输出前有计划停机横幅；`-f` 的长值以"换行 + 制表符"续行 |
| 队列 | `--rsc -x` 给出每个队列的 `[ENABLE, START]` 状态、节点范围、MAX_ELAPSE、REMAIN_ELAPSE（到计划停机的剩余时间）、内存和可用项目 |
| 项目上限 | `--limit`：Miyabi-G 每项目 128 accepted / 16 running / 256 节点 / 24 MIG；Miyabi-C 为 4 / 2 / 64。按项目（组）计，组内所有用户共享 |
| 占用（查询时） | debug-g/interact-g 94%，short-g 100%，regular-g 80%，debug-mig 12% |
| 预算 | `show_token` 给出组上限和已用比例（查询时组已用 73%）以及个人已用量 |
| 配额 | `/home` 50 GB / 102,400 个文件；`/work/<group>` 约 28 TB / 5,662 万个文件 |
| 计划停机 | 2026-11-25 09:00 JST |
| 延迟 | `qstat -ll` 0.30 s；`--limit` 0.82 s；3 个作业的 `-H -f` 1.42 s；`-H -ll --hday 31 --hnum 5000` 在 60 s 内没有完成（被终止前已输出 2,165 个已结束作业） |
| Exit_status | 记录中只出现过 0 和 1。10-30 实测：运行中被 qdel 为 271，walltime 超限为 −29，排队或 HELD 时被删除则没有 Exit_status 且没有开始时间 |

### 3.6 由记录得出的需求

- **R1 多作业条件。** 支持全部结束、任一结束、开始运行；一次通知汇总所有作业的终态（`wait_jobs.sh`；一次登记 12 个 watcher）。
- **R2 队列状态条件。** 支持"维护结束、队列恢复 START"（自建的队列 watcher）。
- **R3 低成本轮询。** 所有等待共享一次 `qstat -ll`；历史只按作业号或窄窗口查（31 天全量历史超过 60 s）。
- **R4 通知其他 agent。** 编排者需要通知 worker（herdr、SendMessage、Codex thread），而不只是通知自己。
- **R5 失效必须可见。** watcher 随会话消失、连接断开时不能静默（Claude 路径的事件停在 waiting）。
- **R6 台账。** 记录每个作业的意图、参数、脚本快照、日志位置、标签和状态变化。台账位置可预测（每个项目一处），其他项目的作业也能找到其记录。
- **R7 提交时检查上限。** 包括每作业 node·h、队列 walltime、每个 worker 的并发、项目 accepted/running。
- **R8 退出码与停止信号。** 作业内标记比 qstat 历史更早、更完整（618/650）。
- **R9 FINISH 不等于成功。** "FINISH 但没有开始时间"表示从未运行。
- **R10 与登录节点无关。** 服务和 daemon 可能在 g1 或 g2 上，状态和存活判断都基于共享文件。
- **R11 感知维护。** walltime 超过 REMAIN_ELAPSE 的作业在停机前不会开始。
- **R12 多 agent 共享限额。** 按标签（agent、worker、实验）计并发和预算。

## 4. 设计原则

1. 服务端放在 PBS 旁边（登录节点），远端通过 ssh stdio 接入。
2. 一次 `uv sync` 安装；核心只用标准库，MCP 层用官方 SDK；CLI 与 MCP 共用同一套实现。
3. PBS 是事实来源，台账是缓存和意图日志。未知就记为未知，不推断。
4. 写操作出错时一律拒绝（fail closed）：先计划后提交、策略上限、幂等键、结果不确定时先对账。
5. 登录节点上只做有界工作：固定命令（argv 列表，不经 shell）、超时、窄查询、共享缓存。
6. 条件判定留在登录节点，通知由 agent 所在机器上的 daemon 投递；通知通道失效本身也要通知 agent。
7. 不绑定特定 agent：投递做成适配器；同一事件可以导出给上层的记录系统。

## 5. 部署拓扑

### 5.1 三种接入方式

```
 工作站                                          Miyabi 登录节点（g1、g2、g3 之一）
┌────────────────────────────────┐               ┌───────────────────────────────────────────────────┐
│ Claude Code / Codex            │               │ pi-env daemon（每个连接一个）                     │
│  MCP miyabi-g (pi-env-mcp) ────┼── B、C ──────▶│ ├─▶ miyabi-mcp serve（B）─▶ qstat / qsub / qdel   │
│    B：miyabi_pbs__<工具>       │  每个 agent   │ └─▶ bash：miyabi-mcp <cmd>（C）                   │
│                                │  一条 ssh 会话│                                                   │
│  notifyd：ssh … notify poll ───┼──────────────▶│ miyabi-mcp notify poll / report（默认每 180 秒）  │
│  export_command：ssh … ────────┼──────────────▶│ miyabi-mcp ledger --format smooth-research        │
└────────────────────────────────┘ 同一个        │ /work 上的共享状态：                              │
                                  ControlMaster  │ <project>/.miyabi-mcp（台账、通知、日志）         │
 登录节点上的 agent ── A：stdio ────────────────▶│ miyabi-mcp serve（A）                             │
                                                 └───────────────────────────────────────────────────┘
```

| 方式 | agent 位置 | 启动 | 说明 |
| --- | --- | --- | --- |
| A | 登录节点（tmux 或 herdr 中） | `miyabi-mcp serve`（stdio） | notify daemon 也在这台登录节点上 |
| B | 工作站 | pi-env-mcp 在登录节点上托管 `miyabi-mcp serve`（5.3） | 推荐的远端方式。agent 只注册 pi-env-mcp，作业工具是它的 `miyabi_pbs__<工具>`。服务端走 pi-env-mcp 已有的连接，不另开 ssh 会话；不再另外用 `ssh … serve` 注册 |
| C | 工作站，pi-env-mcp 没有托管 miyabi-mcp | 经 pi-env-mcp 的 `bash` 调用 `miyabi-mcp <cmd>` | 不需要额外注册，功能相同 |

B、C 中，notifyd（9.3）和 `export_command`（11.3）仍在工作站上直接经 ssh 访问登录节点（`ssh -T -o BatchMode=yes <alias> …`，走同一个 ControlMaster）。pi-env-mcp 没有一次性执行命令的子命令；经它的 MCP 接口一次性调用，每次都要重新连接：Miyabi 上实测 2.3–2.5 s，经 ControlMaster 的 ssh 是 0.6 s（11-06，pi-env-mcp 一侧测得）。要让这两项也走 pi-env-mcp，需要在本地常驻一个持有连接的进程，以后再定。

### 5.2 为什么服务端放在登录节点

另一种做法是在工作站跑服务端，通过 ssh 或 pi-env 守护进程远程执行 PBS 命令。不选它的原因：

- PBS 命令、作业日志、作业内标记和台账都在登录节点可见的共享文件系统上。服务端放在本地，每次读文件和执行命令都要跨 ssh，等于重做一遍 pi-env-mcp。
- 放在登录节点，一套代码路径就能同时服务 A、B、C 三种方式。
- 远端的代价可以接受：托管时服务端跑在 pi-env-mcp 已有的连接里，一个 agent 一条 ssh 会话，由 bash、终端、文件传输和托管的服务端共用。连接断了，下一次调用时 pi-env-mcp 重新连接，并重新启动服务端。
- 通知的投递在 agent 所在机器上进行：那里运行一个由 agent 启动的 notifyd（9.3），它经 ssh 向登录节点 poll。

### 5.3 注册示例

agent 在工作站上（B）时，服务端注册在工作站的 pi-env-mcp 配置里，由 pi-env-mcp 在登录节点上启动；agent 只注册 pi-env-mcp。服务名用 `miyabi-pbs`，工具名就是 `miyabi_pbs__<工具>`：pi-env-mcp 在 Claude Code 里注册为 `miyabi-g` 时，提交工具是 `mcp__miyabi-g__miyabi_pbs__job_submit`。没有 `npm link` 时，下面的 `pi-env-mcp` 换成 `node <pi-env-mcp>/dist/cli.js`。

```bash
# 工作站：pi-env-mcp 里已经 add 并 trust 过主机 miyabi-g（见 pi-env-mcp 的 README）
pi-env-mcp mcp add miyabi-pbs --host miyabi-g -- \
  <path>/miyabi-mcp/bin/miyabi-mcp serve --project <project> \
  --notifier <工作站名> --local-launcher <工作站上的 bin/miyabi-mcp> --remote-alias miyabi-g
pi-env-mcp mcp list      # 在登录节点上启动它，列出状态和工具；有服务起不来时退出码为 1

# agent 只注册 pi-env-mcp（Claude Code）
claude mcp add --scope user miyabi-g -- pi-env-mcp serve --host miyabi-g

# agent 在登录节点上（A）：直接注册。写明 --project-dir 最稳妥（服务端工作目录在项目内时也可以省略）
claude mcp add --scope local miyabi-pbs -- \
  <path>/miyabi-mcp/bin/miyabi-mcp serve --project-dir /work/<group>/<user>/<project>
```

```toml
# Codex（~/.codex/config.toml）：同样只注册 pi-env-mcp
[mcp_servers.miyabi-g]
command = "pi-env-mcp"
args = ["serve", "--host", "miyabi-g"]
tool_timeout_sec = 3600   # pi-env-mcp 的 README：bash 默认可以跑 120 s，Codex 默认只等 60 s
```

- `<path>` 是 Miyabi 上本仓库的绝对路径；`<project>` 是 `/work/<group>/<user>/` 下的项目目录名（7.1）。
- `--project`：pi-env-mcp 启动服务端时，工作目录是主机配置的 cwd，没有配置时是远端 home，服务端从中推断不出项目，所以要写明。一份 pi-env-mcp 配置服务多个项目时可以不写，改在工具调用中传 `project` 参数。
- `--notifier`、`--local-launcher`、`--remote-alias` 让返回的通知命令和 `export_command` 在工作站上可以原样执行（9.3、11.3）。`--remote-alias` 有两个用处：它是 pi-env-mcp 里登录节点的主机名，interactive_plan 返回的 `terminal_open.host` 用它；它也是工作站 `~/.ssh/config` 里的 ssh 别名，notifyd 和 `export_command` 用它。所以两者要同名，这里都是 `miyabi-g`。
- 超时：pi-env-mcp 对托管服务端的每个请求默认等 60 s，收到 progress 时重新计时。job_wait 默认最多 45 s，等待时发 progress。job_submit 的提交、job_get 查历史和 job_cancel 都不发 progress；PBS 命令各有 30–60 s 的超时，提交时还要等锁，最坏情况会超过 60 s。超时后 pi-env-mcp 会取消服务端的这次调用，而提交可能已经完成（5.4）。所以在 pi-env-mcp 的配置里给这一项加 `"timeout": 300`（`mcp add` 没有对应的参数）。
- 不要给这一项设 `--description`：pi-env-mcp 会用它整个替换服务端的 instructions。

`bin/miyabi-mcp` 是一个 shell 启动器：用 `miyabi-mcp/.venv` 里的 Python 运行服务，不依赖远端 shell 的 PATH；venv 不存在时报错，并给出安装命令（6.1）。

### 5.4 连接与登录节点的注意事项

- **主连接与 BatchMode。** pi-env-mcp 的 shareConnection 主机、notifyd 和 `export_command` 都经工作站到 miyabi-g 的主连接（ControlMaster）登录。主连接不在时，pi-env-mcp 报 `no shared ssh connection`，ssh 在 BatchMode 下登录不了；用户要先 `ssh -fN miyabi-g`。ControlPersist（这里是 8 h）是从最后一个会话断开时开始计时的。
- **一条主连接最多同时 10 个会话。** 11-06 实测：主连接上已有 1 个会话时，再同时开 11 个，前 9 个成功，后 2 个被拒，ssh 报 `Session open refused by peer`（sshd 的 MaxSessions，默认值就是 10；Miyabi 的 sshd_config 普通用户读不到）。测试时把 ssh 的直连回退指向 127.0.0.1:9，没有对 Miyabi 发起登录。每个 pi-env-mcp serve（一个 agent 一个；它托管的服务端不另占）运行期间都占一个；notifyd 的每次 poll、report 和 ensure 时的目标检查，以及 `export_command`，各占一个，但很快释放。
  - 会话被拒后，ssh 会改为直接连接，而 BatchMode 下直接连接登录不了，所以错误的结尾是 `Permission denied (publickey)`，看起来像认证问题。11-06 联合测试 B 轮，两个协调者和各角色共开了约 8 个 MCP 会话，再加上 notifyd 和只读检查，`notifyd ensure` 的目标检查就因此失败了。
  - notifyd 遇到被拒的会话时，若远端什么都没有执行（stdout 为空），隔 2 s、5 s 各重试一次；仍被拒时报 `ssh_sessions_full`，写明原因，并请用户关掉不再用的 MCP 客户端。此时"通道中断"的消息不再建议重开主连接，因为主连接还在，只是满了。pi-env-mcp 从 8ba80d0 起也这样提示。
- **pi-env-mcp 托管（B）。** pi-env-mcp（476e15d 起）对托管的服务端有下面这些约束，miyabi-mcp 的对应做法分别如下。11-06 用假 PBS 在 miyabi-g1 上经 pi-env-mcp 实测过：没有真的 qsub，`job_wait_max_s` 临时调到 100，这一项不设 `timeout`。
  - 只把 instructions 的前 2000 个字符给 agent：INSTRUCTIONS 控制在 2000 以内（现在 1784），由测试检查。
  - 每个请求默认 60 s 超时，收到服务端的 progress 时重新计时。实测带 progressToken 的 job_wait 跑满 90 s，每 10 s 一条 progress；同时发出、不带 token 的那个在 60.0 s 报 `MCP request timed out after 60000ms`，因为当时 pi-env-mcp 只在 agent 要了 progress 时才向服务端要。pi-env-mcp 6aab119 改为总向服务端要 progress，只在 agent 要了时才往上转发（上游 pi 也是这样）。job_wait 默认最多 45 s，本来就碰不到这个上限；其他工具不发 progress，所以这一项要设 `"timeout": 300`（5.3）。
  - 超时后 pi-env-mcp 给服务端发 `notifications/cancelled`。miyabi-mcp 收到取消时，job_wait 随即结束；其他工具在后台线程里照常做完，只是结果不再返回。所以超时或被取消的 job_submit 提交可能已经提交了：带同一个 `request_key` 重试会拿回那个作业；没带 `request_key` 时先用 job_list 或 job_get 确认，不要直接再提交一次。job_submit 的工具说明也这样提醒 agent。
  - 取消要转发到服务端：实测取消正在进行的 job_wait 后，服务端 0.08 s 内就回答了下一个调用，被取消的那次 wait 不再轮询，也没有收到响应（符合 MCP 的规定）。
  - agent 退出时，pi-env-mcp 先关服务端的 stdin，2 s 后再 kill：job_wait 在 stdin 关闭或调用被取消后 0.1 s 内结束（69f2f5a 之前要拖到 8.6 s）。实测 job_wait 进行中关掉 pi-env-mcp 的 stdin，它 0.11 s 后退出，登录节点上没有留下进程。
  - 服务端退出时，它的进程组会被一并杀掉：miyabi-mcp serve 不启动需要比它活得久的进程。notifyd 在工作站上；`qsub -I` 在 agent 的终端里。
  - 不转发 resources、prompts，也不转发服务端发起的请求（sampling、elicitation）：miyabi-mcp 只用工具和 progress。
  - agent 看不到 `serverInfo.version`：`pbs_context.server.commit` 写出运行中的提交，checkout 前进后结果里带 `server_note`。重启托管的服务端，要么在 pi-env-mcp 的 `hosts` 里对该主机 `reconnect: true`，要么重启 agent 的 pi-env-mcp。前者会关掉这台主机上的终端，包括交互作业所在的终端。
- **多台登录节点。** 目前至少有 g1、g2、g3。同一主连接里的会话落在同一台，不同主连接可能落在另一台。台账、通知和轮询快照都在共享文件系统上；进程是否存活靠心跳文件判断，不靠 pid。
- **一个项目用一台登录节点。** 建议同一个项目的 MCP 服务、notifyd 和命令行调用都在同一台登录节点上。这只是使用建议，不是正确性的前提：锁在登录节点之间一致（7.6），观测的先后按查询开始的时间判断（7.3），各节点的时钟由 NTP 同步。几台登录节点同时使用同一个项目没有实测。
- **PATH。** 服务端用绝对路径调用 PBS 命令（`/usr/local/bin/qstat`、`/opt/pbs/bin/qsub` 等，可配置），不依赖 ssh 非登录 shell 的 PATH。
- **时区。** 站点时间是 JST。`qstat -f` 的时间不带时区，作业表的 START_DATE 不带年份。服务端统一输出带 `+09:00` 的 ISO 8601，优先用 `-f` 里的完整时间。

## 6. 软件结构

### 6.1 运行时

- **项目 venv。** 服务运行在 `miyabi-mcp/.venv` 中，由 uv 管理（`pyproject.toml` 加 `uv.lock`），要求 Python ≥ 3.11。宿主的 `/usr/bin/python3` 是 3.9，已停止维护，不再使用。
  - Miyabi 上以系统自带的 `/usr/bin/python3.11` 为基础建 venv：`uv sync --frozen --python /usr/bin/python3.11`。这样不下载解释器（uv 默认把下载的解释器放在 `~/.local/share/uv/python`），venv 和 uv 缓存都在 /work 上，`$HOME` 下什么都不写。
  - venv 与 CPU 架构绑定：在 G 登录节点（aarch64）上建的 venv 不能在 C 登录节点（x86_64）上用。两边都要用时，各建一个（`UV_PROJECT_ENVIRONMENT=.venv-$(uname -m)`），启动器按架构选择。
- **MCP 协议层用官方 Python SDK**（`mcp`，锁定在 1.x）。用它的低层 `Server` 接口显式声明每个工具的 `inputSchema`、`outputSchema` 和注解，返回 `structuredContent`，同时附带文本形式的 JSON。进度通知、取消、实验性的 `claude/channel` 也通过 SDK 实现。
- **核心逻辑只用标准库**，不导入 SDK。CLI，以及以后在计算节点上运行的部分（作业内标记、pilot），都不依赖 MCP。
- **长调用**支持取消和进度通知：Codex 和 pi 默认单次调用超时 60 s，pi 收到进度通知会重置超时。
- **报告运行的代码版本。** MCP 服务的生命周期跟随客户端会话，一直运行启动时的代码；克隆更新以后，agent 读到的 README、DESIGN 可能比它连着的服务新。11-06 联合测试中，协调者把这种版本差当成了缺陷。所以服务启动时记下克隆所在的提交：`serverInfo.version` 为 `0.1.0+<提交>`，`pbs_context.server` 带 `commit`；克隆已经前进时（每个工具调用最多每分钟查一次 `git rev-parse`），每个结果附 `server_note`，`pbs_context.server` 另带 `checkout` 和 `restart_needed`，请 agent 重启 MCP 服务（重启它的 MCP 客户端）。CLI 每次调用都是新进程，不会落后。

### 6.2 目录

```
miyabi-mcp/
  DESIGN.md                本文
  README.md                安装、注册、用法（实现时编写）
  pyproject.toml、uv.lock  依赖（mcp 1.x）与锁定版本
  .venv/                   uv sync 生成，不入库
  bin/miyabi-mcp           shell 启动器：用 .venv 里的 Python 运行 miyabi_mcp
  src/miyabi_mcp/
    cli.py                 子命令与 --json 输出；serve 启动 MCP
    mcp/                   stdio JSON-RPC、工具注册（schema、annotations）
    pbs/runner.py          固定命令：argv 列表、绝对路径、LC_ALL=C、超时、诊断截断
    pbs/tables.py          -ll / -H 作业表（来自 qstat_json.py）
    pbs/detail.py          -f / -H -f 记录：续行、时间、按节点的 JSON 值
    pbs/site.py            --rsc -x、--limit、--rscuse、show_token、停机横幅
    pbs/submit.py          qsub argv 构造、校验、作业号解析、对账
    context.py             来自 miyabi-pbs-cmd 的 context.py
    ledger/                作业记录、事件日志、锁、原子写、导出
    policy.py              上限与计划摘要
    notify.py              通知：作业组、封口、条件、poll、报告
    notifyd.py             agent 所在机器上的 daemon 与 listener
    deliver.py             本机投递：listener、herdr、tmux、codex_queue（来自 job_wakeup.py）、command、file
    report.py              （v0.2）来自 pbs_time_report.py
    pool/                  （v0.3）pilot pool
  share/job-hook.sh        （v0.2）可选的作业内标记
  tests/                   假的 qstat/qsub/qdel，加上记录下来的 Miyabi 输出（匿名化）
```

### 6.3 CLI 与 MCP 共用实现

每个工具对应一个 CLI 子命令，输入相同，输出同一份 JSON（含 `schema_version`、`ok`、`error.type/message`）。例如 `miyabi-mcp jobs --json`、`miyabi-mcp submit --dry-run …`、`miyabi-mcp notifyd ensure`。退出码沿用现有脚本：0 成功，1 执行或解析失败，2 参数错误。

## 7. 数据模型与存储

### 7.1 存储位置

**原则**：miyabi-mcp 在 Miyabi 上写的东西都放在 /work 上，`$HOME` 下什么都不写（Miyabi 的 /home 只有 50 GB、102,400 个文件）。必须保留的产出（台账、事件、脚本快照、计划、通知、PBS 日志）放在**项目目录**下；与项目无关的少量用户级文件放在 `/work/<group>/<user>/.miyabi-mcp/`。

**项目目录**

- 默认是 `/work/<group>/<user>/<project>`，用户可以另行指定。状态目录是其中的 `.miyabi-mcp/`。
- 解析顺序，取第一个有值的：
  1. 工具调用中的 `project` 参数（一个服务实例服务多个项目时使用）；
  2. 启动参数 `serve --project-dir <path>`，或 `--project <name>`（展开为 `/work/<group>/<user>/<name>`）；
  3. 环境变量 `MIYABI_MCP_PROJECT_DIR`；
  4. 按服务端的工作目录推断（方式 A：agent 在登录节点的项目目录里启动服务）。工作目录在 git 仓库或 worktree 内时，取主仓库所在目录（7.7）；否则，位于 `/work/<group>/<user>/<name>/…` 之下时取该 `<name>` 目录。
- 由 pi-env-mcp 托管时（方式 B），服务端的工作目录是 pi-env-mcp 主机配置的 cwd，没有配置时是远端 home，一般推断不出项目，所以注册时写明 `--project`，或在工具调用中传 `project`（5.3）。
- `<group>` 取启动参数 `--group` 或环境变量 `MIYABI_MCP_GROUP`；都没有时，取用户唯一可用的 PBS 项目组（`--limit`），有多个就报错并要求指定。`<user>` 是登录用户名。
- 项目目录必须已经存在（作业要用的代码本来就在那里），否则写操作报错，不会自动创建。
- 没有解析出项目时，只读查询照常可用（PBS 是事实来源）；提交、取消和通知报 `no_project`。
- `.miyabi-mcp/` 创建时设为 700，并在里面放一个内容为 `*` 的 `.gitignore`。项目是 git 仓库时，不必修改项目自己的 `.gitignore`。
- /work 的文件数配额宽裕（组上限 5,662 万），每个作业一个子目录没有问题。

```
<project_dir>/.miyabi-mcp/
  .gitignore                          内容为 *
  project.json                        项目级设置：日志目录、默认标签等
  policy.json                         项目级上限，覆盖用户级默认值（10.1）
  submissions/<submit_id>/record.json 作业记录（原子替换）；外部作业接管时用 adopted-<num>
  submissions/<submit_id>/script.sh   提交时的脚本快照
  submissions/<submit_id>/markers/    （v0.2）作业内标记，即作业里的 MIYABI_JOB_RECORD
  jobs/<num> -> ../submissions/<submit_id>   拿到作业号后创建的符号链接，按作业号查找
  logs/<name>-<stamp>-<id>.log        默认的 PBS 输出（-o），可在 project.json 或每次提交时改
  events/<YYYY-MM>.jsonl              本项目所有作业的事件，只追加（写入事件月份与已有最新月份中较晚的那个文件）
  requests/<key-digest>.json          幂等键 → 作业（在项目内唯一）
  plans/<plan>.json                   dry-run 计划（10 分钟有效；超过 20 分钟的由之后的 dry-run 删除）
  notifications/<id>.json             通知：成员作业、封口、条件、投递状态
  locks/                              跨节点锁
```

记录目录按 `submit_id` 命名，因为 `qsub` 之前还没有作业号，而 `-v` 传给作业的 `MIYABI_JOB_RECORD` 必须在提交前就确定。

**用户级目录** `/work/<group>/<user>/.miyabi-mcp/`（可用环境变量 `MIYABI_MCP_HOME` 改到别处）

| 文件 | 内容 |
| --- | --- |
| `config.json` | 站点命令路径、远端别名、默认轮询间隔、具名投递命令、默认 `group` |
| `policy.json` | 用户级默认上限 |
| `cache/active.json` | 共享的 `qstat -ll` 快照。`qstat -ll` 返回该用户的全部作业，与项目无关；可随时删除 |
| `cache/accepted.json` | 该用户最近一次 qsub 被接受的时间（任一项目）。提交的实时检查只用比它新的快照（8.3） |
| `projects.json`、`index/`、`locks/submit` | 跨项目的项目登记、作业索引和提交锁（7.7） |
| `notifiers/<名>.json` | 各 notifier（daemon）的心跳：最近一次 poll 的开始时间、最近一次无错误 poll 的时间、实际间隔、本轮 errors，以及 daemon 报告的退出（9.3） |
| `notifyd/` | 在登录节点上运行的 daemon 的本机状态（9.3） |

**跨项目**

- **一个作业只属于一个项目。** 提交时通过 `-v` 传入 `MIYABI_PROJECT_DIR` 和 `MIYABI_JOB_RECORD`。
- `job_get` 查看别的项目的作业时，从 `qstat -f` 的 `Variable_List` 找到它的记录，只读。
- `job_annotate(adopt)` 拒绝接管已经属于其他项目的作业。否则两个台账会为同一个作业产生 `event_id` 相同、内容不同的事件。
- 幂等键和 `client_ref` 在项目内唯一。
- 旧记录定期压缩成按月归档（v0.2）。

### 7.2 作业记录（示例）

```json
{
  "schema_version": 1,
  "job_id": "1234567.opbs",
  "origin": "submitted",
  "system": "Miyabi-G",
  "project_dir": "/work/<group>/<user>/proj",
  "name": "train-a",
  "labels": {"project": "proj", "agent": "worker-1", "stage": "smoke"},
  "client_ref": "ATT-0003-1",
  "request_key": "sr:ATT-0003-1",
  "submit": {
    "submit_id": "8c0f3b9e-…",
    "submitted_at": "2026-10-30T17:12:05+09:00",
    "host": "miyabi-g1",
    "actor": {"client": "claude-code 2.1.312", "label": "worker-1"},
    "argv": ["/opt/pbs/bin/qsub", "-q", "debug-g", "…"],
    "cwd": "/work/<group>/<user>/proj",
    "script": {"source": "/work/<group>/<user>/proj/scripts/train.pbs", "snapshot": "script.sh", "digest": "blake2b:…"},
    "queue": "debug-g", "group": "<group>", "select": "1:mpiprocs=1",
    "walltime_s": 1200, "requested_node_hours": 0.33,
    "output": "/work/<group>/<user>/proj/.miyabi-mcp/logs/train-a-20261030-171205-8c0f.log",
    "env_names": ["MIYABI_SUBMIT_ID", "MIYABI_CLIENT_REF", "SR_ATTEMPT_ID"],
    "plan_digest": "blake2b:…", "expected_seconds": 600,
    "note": "smoke of writer v2; next: read results/metrics.json"
  },
  "state": {
    "kind": "ended", "execution_state": "ended", "outcome": "success", "raw_state": "F Exit_status=0",
    "pbs_time": "2026-10-30T17:26:01+09:00", "exit_status": 0,
    "pbs": {"exec_host": "mg0123/0*72",
            "exec_vnode": "(mg0123:mem=104857600kb:ncpus=72:ngpus=1:nvnodes-g=1)"},
    "observed_at": "2026-10-30T17:27:02+09:00", "observed_ts": 1793348818.413, "source": "qstat -H -f", "seq": 4
  },
  "usage": {"node_hours": 0.1725, "basis": "measured", "token_reported": 0.2},
  "markers": {"started": null, "exit": null},
  "verdict": {"status": "unreviewed"},
  "notifications": ["N-20261030-181500-ab12"]
}
```

`verdict` 是 agent 或人对应用层结果的结论（`unreviewed`、`verified_success`、`verified_failure`），与 PBS 的 `outcome` 分开。

`state.observed_ts` 是得到这个状态的那次 PBS 查询开始的时间（epoch 秒），用来丢弃较旧的观测（7.3）；`observed_at` 是写入事件的时间。

`state.pbs` 原样保存 PBS 打印的 `exec_host` 和 `exec_vnode`：running 和 ended 观测写入事件和记录，后来缺这两个字段的观测不覆盖已有的值。PBS 历史过期以后，仍能查到作业跑在哪个节点、哪个 MIG 切片上（`exec_host` 的 `mg1112/3*18` 中的 3），诊断节点故障或数值异常时用得上。服务端不解析它们，导出（11.2）也不包含它们。

### 7.3 事件（中性格式）

只有状态变化时才产生事件。每个作业的 `seq` 单调递增，`event_id` 稳定，可以直接用作下游的去重键。

```json
{"event_id": "miyabi-pbs:1234567.opbs:4", "job_id": "1234567.opbs", "seq": 4,
 "observed_at": "2026-10-30T17:27:02+09:00", "observer": "miyabi-g1",
 "source": "qstat -H -f", "kind": "ended",
 "execution_state": "ended", "outcome": "success", "raw_state": "F Exit_status=0",
 "pbs_time": "2026-10-30T17:26:01+09:00", "exit_status": 0,
 "usage": {"node_hours": 0.1725, "basis": "measured"}}
```

`kind` 的取值：`submitted`、`queued`、`held`、`running`、`ended`、`cancel_requested`、`cancel_failed`、`unknown`、`reconciled`、`annotated`。

事件写入其 `observed_at` 所在月份的文件；已有更晚月份的文件时，写入最晚的那个。`events` 的游标是"文件 + 行数"，读者离开旧月份后不再回头，所以事件不能落进旧文件（登录节点之间的时钟偏差、跨月时等锁都会造成这种情况）。

各类事件的来源：

| kind | 何时产生 | raw_state | pbs_time | source_ref |
| --- | --- | --- | --- | --- |
| `submitted` | qsub 返回作业号后，立即用一次 `qstat -f <id>` 确认 | 确认成功时为 PBS 状态字母（Q、H 等），失败时为 `submitted` | 确认成功时为 qtime，否则省略 | `qsub <id> @<host>` |
| `queued`、`held`、`running` | 共享快照中的状态变化后，批量 `qstat -f` 确认 | 状态字母 | stime 等，没有就省略 | `qstat -f <id> @<host>` |
| `ended` | 作业从活动表消失后，批量 `qstat -H -f` | `F Exit_status=<n>` 或 `FINISH no-start` | obittime | `qstat -H -f <id> @<host>` |
| `unknown` | 活动表和历史里都找不到（只针对尚未结束的作业） | 如 `not in active table or history` | 省略 | 查询命令和主机 |
| `reconciled` | 带证据的更正 | 更正后的值 | 按证据 | 证据来源；必须带 `note` 写明证据，并用 `correction_of` 指向原事件 |

- **事件写入后不再修改。** 后来发现判断有误（例如 Exit_status 的映射）时，追加一条 `reconciled` 事件。
- **数值在生成事件时定稿。** `node_hours`、`mig_instance_hours` 保留 6 位小数（例如 617 s × 1 节点 → 0.171389），写入事件。导出、台账和通知消息都用这个存下的值，不重新计算。
- **已结束的作业不再轮询**，所以结束之后不会再产生 `unknown`，只会有带证据的 `reconciled` 更正。
- **刚提交的作业。** 提交后 `submit_grace_s`（默认 120 s）内，作业不在活动表时同样查历史，但只有 `job_state` 为 F（数组子作业为 X）的记录才记为 `ended`；两处都查不到时不开始计 missing（7.4 的 unknown），因为它也可能只是还没列进表里。
  - 11-06 联合测试 B 轮之前，这段时间里根本不查历史：几秒就结束的作业在 `job_wait` 里一直是 queued，`met` 为 false，要等宽限期过去才记为结束，而 `job_wait` 一次最多等 45 s。有回归测试。
  - Miyabi 实测（11-06，各项目台账里 480 个已结束的作业）：作业离开活动表后查历史都查到了，最早在 obittime 后 2 s；没有出现过 missing 或 unknown。结束的记录 `job_state = F`，排队时被删除、没有开始时间的也一样。
- **较旧的观测不覆盖较新的。** 每个观测按其 PBS 查询开始的时间计（`state.observed_ts`）。查询开始得比已记录状态的那次查询更早的观测，直接丢弃。两次刷新同时进行时（例如 agent 的调用和 notifyd），先查询的可能后写入，照写会让状态倒退（running → held），并记下没有发生过的变化（11-05 审查时发现，有回归测试）。
  - `submitted` 只由 qsub 的接受担保，按接受的时间计；`unknown` 还依赖更早的查询（活动表读取，可能还有 `qstat -f`），按其中最早的那次计。所以别的进程先记下了更晚的状态时，作业的第一个事件不是 `submitted`。
  - 剩下的窗口：两次查询在时间上重叠时，PBS 回答的先后不一定与开始的先后一致；正好在这段时间里（通常不到一秒）发生的状态变化仍可能按错误的顺序记下，下一次刷新会纠正。登录节点之间比较的是各自的时钟，NTP 的偏差在毫秒级，影响与此相同。
  - 带证据的 `reconciled` 更正不做这个比较。
- `<host>` 是写入事件的那台登录节点，即事件里的 `observer`。
- **`pbs_time`** 是 PBS 自己记录的事件时间（qtime、stime、obittime 等），导出时成为 `occurred_at`。

### 7.4 PBS 状态映射

| PBS 观察 | execution_state | outcome | 说明 |
| --- | --- | --- | --- |
| 作业表 `QUEUED`；`-f` 的 `job_state` 为 Q、W、T | queued | unknown | |
| `HELD`；`job_state` H | queued | unknown | `raw_state` 保留 H；记录中出现过成批 HELD 的作业 |
| `RUNNING`；`job_state` R、E（退出中）、B（数组已开始） | running | unknown | |
| `FINISH`，`Exit_status = 0` | ended | success | 只表示 PBS 层面正常退出；应用是否成功由结果导入判断 |
| `FINISH`，`Exit_status` 1–255 | ended | failed | 脚本退出码 |
| `FINISH`，`Exit_status ≥ 256` | ended | 台账中有本服务发出的取消时为 cancelled，否则为 failed | 256 + 信号号。Miyabi 实测（10-30）：qdel 运行中的作业为 271（SIGTERM），comment 为 "terminated by <user>" |
| `FINISH`，`Exit_status < 0` | ended | failed；有取消记录时为 cancelled | PBS 的执行错误码。Miyabi 实测（10-30）：walltime 超限为 −29，comment 为 "exceeded resource walltime"，日志末行为 `=>> PBS: job killed: walltime 67 exceeded limit 60` |
| `FINISH`，没有开始时间 | ended | cancelled | 排队或 HELD 时被删除，从未运行（记录中观察到过） |
| `EXPIRED` | ended | 有 Exit_status 时按上面的规则，否则 unknown | 站点状态，含义待确认 |
| 活动表和历史里都找不到 | unknown | unknown | 保留最后一次可信状态，不推断 |
| qsub 结果不确定（没拿到作业号） | — | — | 记为 `submit_uncertain`，对账之前不产生作业状态 |

- `raw_state` 保留 PBS 原值，例如 `F Exit_status=271`、`FINISH no-start`，以便日后带证据更正。
- `submitted` 事件的 `execution_state` 为 queued；`prepared` 由 smooth-research 在登记 attempt 时自己产生。

### 7.5 用量

- **G 节点。** `node_hours = resources_used.walltime × G 节点数`，节点数取 `exec_vnode` 中 `nvnodes-g` 之和（与 `nodect` 一致），basis 为 `measured`。每个节点一块 GH200，所以 node·h 就是 GPU·h。
- **MIG 队列。** `select` 计的是 MIG 实例。实例数取 `exec_vnode` 中 `nvnodes-mig` 之和，单位为 `mig_instance_hours`。实测的一个 debug-mig 作业是 `exec_vnode = (mg1098:mem=26214400kb:ncpus=18:ngpus=1:nvnodes-mig=1)`，即 18 核、25 GiB 主机内存，约为一个节点的 1/4。debug-mig、short-mig、regular-mig 都是 1 个节点上 1、2 或 4 个实例（`--rsc -x` 的 `AVAIL_MIG`）。
- **MIG 切片视作一样。** 切片由 PBS 分配，提交时不能指定（`select` 里的 `vnode` 会被 qsub 以 "Unknown Resource" 拒绝）。站点说明里 MIG#0–#2 是 32 SM、24 GB，MIG#3 是 26 SM、24 GB；11-05 对照 15 个作业，编号 0–2 是 2g.24gb，编号 3 是 1g.24gb。不同切片的算力和 BF16 数值可能不同，所以需要逐位一致的比较要放进同一个作业，规格要在作业内用 `nvidia-smi -L` 自测。计费与切片无关：批处理 MIG 每实例·小时 0.25 token，互动 MIG 不消耗 token。所以用量、预算和导出只按实例数和时间算，miyabi-mcp 不解析切片编号、不推断规格；MIG 队列的 dry-run 计划附一句固定说明（`plan.mig_slice`）。
- **TOKEN 列。** 这是四舍五入后的站点记账值，单独记为 `reported`，与 node·h 分开，`accounting_ref` 也不同（11.2）。
- **缺数据不补。** 没有 `resources_used.walltime`（从未开始，或历史已过期）就不记用量，不写 0。
- **几秒的作业可能实测为 0。** PBS 按整秒、周期性地更新 `resources_used.walltime`，只跑几秒的作业可能报 `00:00:00`。这是 PBS 给出的实测值，照记为 0（basis 仍是 measured），不按开始和结束时间去推算。11-06 联合测试中，两个约 5 s 的 debug-mig 作业分别记为 0 和 1 s（0.000278 MIG 实例·h）。
- **申请量分开记。** 申请量（nodes × walltime）记在 `submit.requested_node_hours`，与实际用量分开。

### 7.6 锁与写入

- **记录**：先写临时文件，再 `rename` 替换。Lustre 上 rename 是原子的，实验平台已经依赖这一点。
- **记录与事件的顺序**：在作业锁内先写占用新 `seq` 的记录，再追加事件；追加失败就把记录写回原样。反过来的顺序会重复事件 ID：事件已追加而记录没写成（/work 配额满时，小的追加仍可能成功，新建临时文件却失败），下一个事件就会沿用同一个 `seq`（11-05 审查时复现）。
- **绑定作业号**：在作业锁内修改已存的记录，不用调用方手里的副本整体覆盖。作业号的链接一旦存在，别的进程就可能记事件，旧副本会把 `seq` 退回去；同一提交再绑定一次不改变任何东西，也不再补 `submitted` 事件。
- **锁**：用 `fcntl.flock` 锁住锁文件。/work 是带 `flock` 挂载选项的 Lustre（11-05 查证），这种锁在各客户端节点之间一致；工作站的本地磁盘同样支持。持锁进程退出或崩溃时，系统自动释放它的锁，所以没有租期，也不需要抢占：卡住的持有者只会让其他调用方等到超时（`lock_timeout`，错误里带持有者的主机、pid 和取锁时间），不会让两方同时进入。
  - 每次取锁都重新打开锁文件。flock 属于打开的文件，所以同一进程的不同线程之间也互斥（MCP 服务在线程里执行工具）；POSIX 的 `lockf`/`fcntl` 锁做不到这一点。
  - 持有者释放时先删除锁文件再解锁，锁文件不会越积越多。等锁的调用方如果锁住的是刚被删除的文件，会发现路径已经指向另一个文件，于是重新来过。
  - 此前的做法是用 `os.link` 创建锁文件，超过租期视为失效、可以抢占。这要处理抢占与释放之间的竞态（两方同时持锁，11-05 审查时复现）、持有者续期，以及 Lustre 只保存整秒的修改时间。当时不用 flock，只是因为没有验证它跨登录节点的行为，而 /work 的挂载选项正好提供这一点（11-05 改用 flock）。
  - 升级到 flock 时要重启所有 miyabi-mcp 进程（各 agent 的 MCP 服务、notifyd）：旧进程仍用 `os.link`，新旧进程之间互不排斥。
- **验证**（11-05，g1，/work）：8 个进程各 40 轮，一成持有者在锁内停留 0.5 s。共 320 次进入临界区，没有两方同时在内，计数一致，最长等锁 18 s，没有超时，结束后不留锁文件。持锁进程被 SIGKILL 后，锁在 1 ms 内就能取得。两台登录节点之间未实测：从工作站只能经共享的 ssh 连接到达一台登录节点，g1 上的 ssh 又不认识 g2 的主机密钥。按 Lustre 文档，`flock` 挂载选项下的锁在客户端之间一致。
- **不用 SQLite**：多个服务实例可能同时在几台登录节点（g1、g2、g3）上写。实验平台规避网络文件系统上的 SQLite 写入，也是同一个原因。

### 7.7 多个项目同时进行

多个项目同时进行是常态。记录中，同一周内常有几个项目在 Miyabi 上跑作业；PBS 的项目上限（16 running / 128 accepted）和 token 预算按组计，由组内所有项目、所有用户共享。所以要把三类东西分开：

| 类别 | 内容 | 放在哪里 |
| --- | --- | --- |
| 项目的记录 | 台账、事件、脚本快照、计划、通知、日志 | 项目目录下的 `.miyabi-mcp/`（7.1，不变） |
| 跨项目的协调 | 作业 → 项目的索引、项目登记、用户级上限和各项目份额、共享轮询快照、提交锁 | 用户级目录 `/work/<group>/<user>/.miyabi-mcp/` |
| 组级事实 | 组内已接受和运行中的作业数、预算 | PBS（`--limit`、`show_token`），每次实时读取 |

并发带来的问题（份额、公平、合计预算、跨项目查找、轮询次数）都属于第二、三类，与台账放在哪里无关。把台账集中到一个用户级目录也解决不了它们。所以台账仍放在项目下，保留这样做的好处：

- 与代码、快照、运行输出放在一起；
- 随项目归档或删除；
- 项目之间互不影响；
- 幂等键天然按项目隔离。

用户级目录补充以下内容：

```
/work/<group>/<user>/.miyabi-mcp/
  config.json  policy.json  cache/active.json   （7.1）
  projects.json        已知的项目目录：名称、路径、首次和最近使用时间
  index/<YYYY-MM>.jsonl  作业索引：提交时追加一行（job_id、project_dir、submit_id、name、queue、nodes、walltime、申请 node·h、提交时间），结束时再追加一行（结果、实际用量）
  locks/submit         用户级提交锁
```

- **作业 → 项目。** `job_list`、`job_get` 和通知通过索引找到作业所属的项目，不必逐个运行 `qstat -f`。索引里没有的作业（不是经 miyabi-mcp 提交的）标为 external。
- **上限分三层**，提交时依次检查：
  1. 组：`--limit` 的实时余量，扣除保留槽位。这是 PBS 事实，已经包括组内其他用户和不经 miyabi-mcp 提交的作业。
  2. 用户：所有项目合计的活动作业数，以及时间窗内的 node·h（由索引和活动表算出）。
  3. 项目：本项目的份额（活动作业数）、每作业上限、按标签（agent）的并发。
- **提交锁。** 检查和 qsub 在用户级锁内完成（持锁约 1–2 s），避免两个项目同时提交时都通过同一个余量检查。持锁进程退出时锁即释放（7.6）。
- **索引可以重建。** 只要各项目台账还在，就能从台账和 `qstat` 重建索引。项目目录被删除后，索引里仍保留其作业的提交和用量摘要，用户级的用量统计不受影响。调查时，记录中出现过的几个项目目录已经不在 /work 顶层。
- **有活动作业时不要移动项目目录。** 作业里的 `MIYABI_JOB_RECORD` 指向原来的路径。
- **同一个研究的多个 git worktree 是同一个项目。** 记录中有一个项目由 1 个主目录和 11 个 worktree（`<repo>-wt/<branch>`）组成，由不同 worker 同时使用。按工作目录推断项目时（7.1 第 4 项），先用 `git rev-parse --git-common-dir` 解析到主仓库所在的目录，避免一个研究被拆成多个台账、各自计算份额。确实需要分开时，显式指定 `--project-dir`。

## 8. 工具接口

### 8.1 一览

所有工具都接受可选的 `project` 参数（7.1 解析顺序的第 1 项）。不传时使用服务启动时确定的项目。

| 工具 | 作用 | 注解 | CLI |
| --- | --- | --- | --- |
| `pbs_context` | 服务端主机、平面、系统；PBS 命令和版本；可用项目组；计划停机；台账位置；策略摘要 | readOnly | `context` |
| `pbs_queues` | 队列形状和状态（含 REMAIN_ELAPSE）、项目上限和已用量、资源池占用、token 预算；可只取部分，缓存 60 s | readOnly | `queues` |
| `job_list` | 当前作业（可附带有界历史），合并台账中的标签；按状态、名称前缀、标签过滤；可投影字段或只给汇总 | readOnly | `jobs` |
| `job_get` | 一个或多个作业的详情（解析 `-f`/`-H -f`）、台账记录、作业标记、可选的日志尾部 | readOnly | `job` |
| `job_submit` | 默认 dry-run 生成计划；凭 `plan_digest` 提交；可同时加入通知（`notify`） | 写（消耗预算） | `submit` |
| `interactive_plan` | 校验交互资源与策略，返回计划及 pi-env-mcp 终端启动参数；在 PTY 内执行后提交并记账 | 写（计划） | `call interactive_plan` → `interactive-start` |
| `allocation_check` | 验证当前 shell 的计算节点与目标架构、nodefile 和实时 PBS 分配，报告剩余 walltime | readOnly | 在分配内 `call allocation_check` |
| `job_cancel` | 默认 dry-run 列出目标；执行 qdel 并记录原因 | destructive | `cancel` |
| `job_wait` | 有界等待（默认最长 45 s）多作业条件；列出这些作业所属的通知和收尾方法 | readOnly | `wait --jobs …` |
| `notify_create`、`notify_update` | 为已提交的作业、队列或到期时间创建通知；加入作业、封口 | 写（台账） | `call notify_*` |
| `notify_list` | 通知状态、封口情况、概况（未完成数、已送达未确认数、各 daemon 的 state）、daemon 心跳和启动命令 | readOnly | `call notify_list` |
| `notify_cancel`、`notify_ack` | 撤销未完成的通知，或把已送达未确认的通知关成 dismissed；凭 token 确认已收到 | 写（台账） | `call notify_*` |
| `ledger_query` | 台账查询和统计（node·h 申请量与实用量、被拒和结果不确定的提交、TOKEN、策略预算，按标签）；增量导出（中性事件或 smooth-research 格式） | readOnly | `ledger` |
| `job_annotate` | 标签、备注、应用层结论；`adopt` 把外部提交的作业纳入台账 | 写（台账） | `annotate` |

### 8.2 查询类

- **`pbs_context`**：合并 `context.py` 的结果（`node_role`、`system`、架构、PBS 作业号与节点文件）和服务端信息。远端接入时，它描述的是服务端主机，不是 agent 所在的机器。
- **`pbs_queues`**：
  - 输入：`system`（G、C 或 all）；`include`（rsc、limit、rscuse、token 中的若干项，每一项是一次 0.3–0.8 s 的命令）。
  - 输出：队列树，含路由关系（`regular-g` → small、medium、large、x-large）。每个队列有 `enabled`、`started`、节点范围、`max_elapse_s`、`remain_elapse_s`、`mem_gib`、`mig_sizes`、可用项目；另有各项目的上限和已用量、资源池占用、token 预算和 `scheduled_stop`。
- **`job_list`**：历史默认 `days ≤ 3`、`limit ≤ 200`，并标出数据新鲜度（`observed_at`、`cache_age_s`）。按标签过滤只对台账内的作业有效。`fields` 只接受顶层字段名（作业表的列加 `finished`、`project`、`ledger`），未知名字和 `ledger.usage` 这样的点路径返回 `invalid_arguments`。null 有几种正常含义：排队中的作业没有开始时间、用时和 TOKEN；MIG 作业没有 nodes，节点作业没有 mig；不在本项目台账里的作业没有 ledger。
- **`job_get`**：
  - `tail_lines`（≤ 200）只读取作业自己的 `Output_Path`/`Error_Path`，从文件末尾按字节上限读取。
  - 默认不返回完整 `Variable_List`（其中有 PATH 等大段内容，也可能有敏感值），只返回 `MIYABI_*` 和提交时 `env_names` 里列出的变量。

### 8.3 `job_submit`

**输入**（`script` 与 `script_text` 二选一）：

```json
{
  "script": "/work/<group>/<user>/proj/scripts/train.pbs",
  "queue": "debug-g", "group": "<group>", "select": "1:mpiprocs=1", "walltime": "00:20:00",
  "name": "train-a",
  "workdir": "/work/<group>/<user>/proj",
  "output": "/work/<group>/<user>/proj/logs/pbs/train-a.log",
  "env": {"SR_ATTEMPT_ID": "ATT-0003-1"},
  "labels": {"project": "proj", "agent": "worker-1"},
  "client_ref": "ATT-0003-1", "request_key": "sr:ATT-0003-1",
  "expected_seconds": 600,
  "note": "smoke of writer v2; next: read results/metrics.json",
  "dry_run": true, "plan_digest": null,
  "notify": {"name": "E12-seeds", "expected": 3, "message": "…"}
}
```

`output` 按字面使用，不展开占位符；不给时默认路径里带时间戳和 submit ID。

**两阶段。** `dry_run: true`（默认）只校验并返回计划和 `plan_digest`。提交时传同样的参数，加上 `dry_run: false` 和该 `plan_digest`。服务端重新生成计划，比对摘要，并重新检查会变化的部分（上限、占用、维护）。这样保证提交的就是 agent 给用户看过的那份；在 Claude Code 的权限提示里，用户看到的也是完整参数。策略可以允许小作业省掉这一步（10.1）。

**检查项**（每项给出 pass、warn 或 fail）：

- **平面**：服务端在目标系统的登录节点上，`qsub` 可用。
- **队列**：必须是 `--rsc -x` 中的顶层队列，状态为 ENABLE、START；节点数在路由后的子队列范围内；walltime ≤ MAX_ELAPSE；walltime 超过 REMAIN_ELAPSE 时，按策略报警或拒绝（"停机前不会开始"）；`*-mig` 队列中 `select` 计的是 MIG 实例，按 `AVAIL_MIG` 检查（1、2、4 通过，3 失败），计划里附切片说明（7.5）。
- **select 的资源名**（`select.resources`）：按本站 qsub 手册列出的 chunk 资源（`mpiprocs`、`ompthreads`、`mem`）检查。`vnode` 直接失败，并说明节点和 MIG 切片由 PBS 分配、不能指定；格式不对的 chunk 失败；集合之外的名字给 warn，写明"未确认"。dry-run 不试投，也不用 `qsub -h` 判断服务器接不接受。
- **项目组**：在用户的组中，且列在该队列的 PROJECT 里；未指定时，只有在唯一一个候选组时才自动选取。
- **上限**：`--limit` 的 accepted 和 running 还有余量，并扣除保留槽位；按标签的并发；策略中的每作业上限、时间窗预算和项目总额 `budget`（10.1）。
- **通知**（`notify`）：规范化后（补上默认值）放进计划和 `plan_digest`，所以同一个摘要下不能再换通知的目标、投递方式或分组；分组的当前状态（是否已封口）在 commit 时复核。
- **脚本**：文件存在；`bash -n` 通过；没有残留的 `<...>` 占位符（尖括号内不含空格，所以 `python x.py <config.yaml >train.log`、`cat <<EOF >notes.txt` 这类重定向不算；整行注释不检查，但 `#PBS` 指令行和首行的 `#!` 照常检查（行首空白不计，与指令的识别一致），行尾注释也照常检查；失败时报出行号和匹配到的文字，`allow_placeholders` 整体放行。11-06 之前所有注释都检查：联合测试 B' 里一句注释写了 `<attempt>`，计划被拦下，研究员只好用 `allow_placeholders` 关掉整份脚本的检查）；解析 `#PBS` 指令，报告被命令行覆盖的指令（命令行优先；只列命令行实际给出的选项：`-q`、`-N`、`-l select`、`-l walltime`、`-W group_list`，批处理另有 `-o`、`-j`，带 `depend` 时还有 `-W depend`；值与命令行相同的不报，报出的写明两边的值。11-06 之前只比较选项名，脚本里写着与项目相同的 `group_list` 也每次报警）；脚本在 `/tmp` 或节点本地路径下时报警。
- **数组作业**：暂不支持。`array` 参数或脚本中的 `#PBS -J` 都使计划失败：计划、时间窗、`budget` 和预留只会按一个子作业计算（`array="1-100"` 时少算 99 倍），用量、取消和通知也没有按子作业处理（11-05 审查）。需要多个成员时分别提交，用带 `expected` 的通知分组等待全部结束。
- **工作目录**（`script.cwd`）：批处理作业从 `$HOME` 开始，`workdir` 只决定 `$PBS_O_WORKDIR`。脚本里（注释之外）没有 `cd` 或 `pushd` 时给 warn，附上示例 `cd -- "${PBS_O_WORKDIR:?}" || exit 1`（放在 `#PBS` 指令之后）。不自动往脚本里插 cd：插的位置不对会让后面的 `#PBS` 指令失效，提交的字节也不再是用户看过的脚本；也不做"cd 之前是否用了相对路径"的静态分析。
- **工作目录与输出路径**：
  - `workdir`（qsub 的工作目录，即作业里的 `$PBS_O_WORKDIR`）默认是项目目录。作业本身从 `$HOME` 开始，脚本要自己 cd。
  - 输出默认写到 `<project_dir>/.miyabi-mcp/logs/<name>-<stamp>-<id>.log`，可在 `project.json` 或每次提交时修改。
  - 输出路径必须是 /work 上的绝对路径：不能放在 `/tmp`（曾有作业从 `/tmp` 提交导致输出丢失）；放在 /home 下会报警（容量小）。
  - 父目录不存在时自动创建。
- **环境变量**：变量名合法；值中的逗号和引号按 `qsub -v` 规则转义，无法安全转义时拒绝。
- **依赖**：`depend` 允许，但会提醒（`afterok` 链曾经断过，建议改用通知的作业组）。交互分配走 `interactive_plan` + PTY 内的 `interactive-start`，不接受脚本、数组、依赖或批处理输出参数。
- **重复**（`request_key`）：键已经提交过作业时，dry-run 给 warn 并写明作业号，不再判断实时上限（commit 不会提交）；commit 直接返回原作业，计划过期也一样。只是 warn 而不是 fail：用同一个键重试是查明中断的 commit 做了什么的安全办法，fail 会诱使 agent 换新键，提交出第二个作业。结果不确定的键也给 warn：commit 先对账，找不到作业时要 `resubmit: true`。键的作业已加入通知分组并使它封口时，dry-run 里 notify 的 fail 同样降为 warn（commit 返回原作业，不再加入）。（"有同名、同标签的活动作业时报警"尚未实现。）
- **代码可变性**：PBS 只在提交时复制脚本，脚本引用的代码要到运行时才读。
  - 这次运行声明了要绑定到一个版本（带 `client_ref` 或 `labels.code_commit`），而 `workdir` 或脚本所在目录仍可写时，给出 warn，并说明补救办法：从只读快照运行（例如 `git archive` 到 `<project>/snapshots/<commit>` 后 `chmod -R a-w`），把 `workdir` 设为它。
  - 没有这种声明时（开发中的运行），只在 pass 里注明代码目录可写，避免每次都报警。
  - `labels.code_commit` 记入台账，供导出和审计使用。
- **默认作业名**：不传 `name` 时，用 `client_ref`（如 attempt id），否则用脚本文件名。

**提交步骤：**

1. 带 `request_key` 的 commit 先在用户级提交锁内查键，早于重新生成计划和核对摘要：已有作业号就直接返回原作业；记录停在 `submitting` 或 `uncertain` 就先对账（见下）。返回的作业若有 `submit.notify` 却还没加入通知（对账才找到它，或上一次调用在 qsub 之后中断），这时补上。进入提交锁后再查一次，防止同一个键的并发调用。
2. 在 `submissions/<submit_id>/` 写记录（状态 `submitting`，含计划和新生成的 `submit_id`），并把脚本快照复制进去。
3. 取得用户级提交锁，重新检查三层上限（7.7），然后以 argv 方式运行 `qsub`（不经 shell），超时 60 s。实时检查用的 `qstat -ll` 快照必须晚于该用户最近一次 qsub 被接受的时间（`cache/accepted.json`，任一项目），否则重新查询：15 s 内的缓存看不到刚提交的作业，连续两次 commit 会一起越过 `max_active_jobs`、`label_limits` 和 `user.max_active_jobs`（11-05 审查时复现）。项目台账在这一步只读一遍。
   - `-v` 中加入 `MIYABI_SUBMIT_ID`、`MIYABI_CLIENT_REF`、`MIYABI_PROJECT_DIR`、`MIYABI_JOB_RECORD`，以及调用方的 `env`。
   - 提交的是快照。PBS 本来就在提交时复制脚本，所以运行语义不变，而记录中的摘要与实际提交的内容一致。
4. 解析作业号（形如 `1234567.opbs`），创建符号链接 `jobs/<num>`，写入记录（`submit.indexed=false`），在用户级索引中追加一行，再把记录标为 `indexed=true`，最后才在幂等键文件里写入作业号；随后追加 `submitted` 事件（7.3），释放提交锁。键文件写入作业号之前中断的，用同一个键重试会补完这些步骤：用户级时间窗只读索引，漏写的作业永远不会被计入（11-05 审查时复现）。
5. 如果请求里带了 `notify`，用计划里规范化的 notify 把作业加入通知（9.2），并返回 daemon 状态和要运行的命令（8.6）。加入在提交锁内进行，对账找到的作业同样加入；notify 规范也记入记录（`submit.notify`），供重试补加入用。加入失败时作业已经存在，结果仍为 ok，但带 `notify_error` 和醒目的 `warning`：这次提交没有通知。

**对账。** qsub 超时或输出无法解析时，记录标为 `submit_uncertain`。随后用 `qstat -f` 查看同名候选作业的 `Variable_List`，找到同一个 `MIYABI_SUBMIT_ID` 就绑定。找不到时报告不确定，必须由调用方显式传 `resubmit: true` 才会再次提交。没有 `request_key` 时无从重试对账：作业之后出现的话，`job_annotate(adopt=true)` 按 `MIYABI_SUBMIT_ID` 把它绑定到这条提交记录，不另建 adopted 记录。

**输出：** `job_id`、记录路径、输出日志路径、`observed_at`、`submitted` 事件，以及下一步提示（例如休眠的命令）。

### 8.3.1 交互分配与执行平面检查

`interactive_plan(queue, walltime, request_key, …)` 复用提交计划的实时队列、组、资源、停机时间和策略检查。
队列为 `interact-g`、`interact-c` 或 `interact-mig` 的顶层名称；支持 `nodes/select`、`workdir`、`env`、标签和备注。
只准备计划，不提交作业；返回 10 分钟有效的摘要、`launch_command`、pi-env-mcp `terminal_open` 参数和
`allocation_check_command`。完整调用示例见 [README](README.md#interactive-compute-allocations)。

调用方在登录节点的持久 PTY 中执行 `exec … interactive-start <plan_digest>`，保留 terminal_id。
CLI 拒绝无 PTY 或非登录节点环境，重新检查计划与策略后以 argv 执行 `qsub -I`，用内部 PTY 转发同一个会话。
解析到 PBS 接受的作业号后立即写普通台账及用户索引，释放提交锁，再持续转发输入输出。
记录有 `submit.interactive=true`，输出路径是持久终端原始日志，不生成批处理脚本快照。
同一 request_key 重试返回原作业，不重复申请；未知结果沿用 MIYABI_SUBMIT_ID 对账机制。

`allocation_check(target_system, expected_job_id?)` 检查执行该命令的进程环境：计算节点角色、架构、有效作业号、
nodefile 主机成员，再检查实时 PBS R 状态、作业所属用户、exec_host 和项目；不满足时返回 ok=false / CLI 非零。
返回剩余 walltime（无法取得时为 null）。必须通过 `terminal_send` 在计算 shell 内调用返回的检查命令；
登录节点上的 MCP 调用不能证明远端 PTY 已获分配。检查成功后才设置项目 cwd、模块和解释器并运行任务。

等待时用 `terminal_read`；调度状态沿用 `job_get/job_wait`，长等待可注册 started 通知并维持终端连接。
普通失败检查在子 shell 中执行，避免结束分配；保存 /work 输出、停止自有后台进程后 `exit`。
qsub 退出后整个专用终端关闭，不退回登录 shell。排队和运行中的取消均沿用 `job_cancel` dry-run/commit，
然后关闭终端。终端工具超时不取消作业；失联、进程或节点故障后必须核实 PBS，清理遗留作业，不能假定已释放。
runner 对可捕获的断开信号尝试取消自己的作业；终端不支持重新附着。

### 8.4 `job_cancel`

- **输入**：`job_ids`（必须明确列出，不接受通配）、`reason`、`dry_run`（默认 true）、`allow_external`（默认 false）。也可以用 `label_selector` 按标签选择：dry-run 先解析出具体列表和摘要，执行时必须带上这个摘要。
- **dry-run**：列出每个目标的当前状态，以及将要执行的操作。
- **执行**：qdel，记录 `cancel_requested` 事件，然后对账。排队中的作业应变为"FINISH，从未开始"；运行中的作业的 Exit_status 要实测后记入 7.4。
- **qdel 失败**：qdel 拒绝的作业（错误输出里点名的；一个都没点名时算全部）记 `cancel_failed` 事件，清掉 `cancel_requested` 标记，再对账；否则它之后因 walltime 或信号结束时会被记成 cancelled。每个目标给出 `result`：`cancelled`（已离开活动表）、`cancelling`（qdel 已接受，仍在表中）、`qdel_failed`、`ended_without_qdel`（qdel 报错时作业已经结束）。没有一个取消成功时返回错误 `qdel_failed`；部分失败时仍为 ok，`failed` 列出失败的作业并带 `warning`。（此前无论 qdel 退出码如何都报告 `cancelled`，11-05 审查时复现。）
- **范围**：默认只取消本项目台账中的作业；作业是否属于本人由 qdel 保证。
- `job_hold`、`job_release`（qhold、qrls）放到 v0.2，用于"暂时把资源让给别的实验"之类的场景。

### 8.5 台账类

- **`job_annotate`**：修改标签、备注和应用层结论（`verdict`，附证据路径；先校验，校验失败不会接管）。`adopt` 把外部提交的作业纳入台账：读 `qstat -f`，记录 `origin: adopted`。作业带本项目一条结果不确定的提交的 `MIYABI_SUBMIT_ID` 时，改为绑定那条提交记录。归属检查和写入（记录、链接、索引）都在用户级提交锁内：两个项目同时接管同一个外部作业时只有一个成功，另一个得到 `owned_elsewhere`。
- **`ledger_query`**：
  - 范围：默认是当前项目的台账；用 `project` 参数查询其他项目。
  - 过滤条件：标签、`client_ref`、时间范围、状态、结果。
  - `summary`：按状态、结果、标签统计数量，以及申请和实际的 node·h 或 MIG 实例·h。申请量只算拿到作业号的；被 qsub 拒绝的（`rejected`）和结果不确定的（`uncertain`）另列。TOKEN 合计单列（`token_reported`），不混进 node·h。策略有 `budget` 时另给 `budget`：已用、预留、合计、占比、阈值和缺实测的作业数（10.1），按所选记录计算，所以用标签过滤就得到阶段用量。策略文件无效时只拦写入，summary 照常返回，另附 `budget_error`。
  - `format`：`records`、`events`（带 `cursor`，可以增量导出）、`smooth-research`（第 11 节）、`csv`。

### 8.6 等待与通知类

- **`job_wait`**：
  - 输入 `job_ids`、`when`（`all_ended`、`any_ended`、`started`）和 `timeout_s`（默认上限 45 s，可配置）。
  - 有 `progressToken` 时每 10 s 发一次进度通知。
  - 返回各作业状态和 `met`。只认 `ended`：unknown 的作业不算结束，也不算开始，`next` 里会请 agent 用 `job_get` 核对。只用于几分钟的短作业，更长的等待用通知（第 9 节）。
  - 这些作业属于通知时，结果的 `notifications` 列出每个通知的 id、状态和收尾方法（未封口、已发过提前或状态不明消息、等待超时、已送达未确认等）。job_wait 不改变通知，也不返回 ack token。
- **`job_submit` 的 `notify` 参数**：提交时把作业加入一个通知，按 `name` 创建或加入：

  ```json
  {"notify": {"name": "E12-seeds", "expected": 5, "when": "all_ended", "early_failure": true,
              "deliver": {"method": "listener"}, "message": "all seeds done: run stats/aggregate.py"}}
  ```

- **`notify_create`**：为已提交的作业（`job_ids`，立即封口）、队列状态（`queue`）或到期时间（`timeout`）创建通知。
- **`notify_update`**：加入作业（`add_job_ids`）、封口（`seal`）、修改 `message`，或改投递目标（`deliver`）。只有 open 的通知能改前三项；`deliver` 对 open、fired 和 failed 的通知都能改：待投递的消息从头计算尝试次数，failed 的通知回到 fired，最终消息重新投递（9.6）。改完再运行一次 `start_command`，它会检查目标（9.3）。
- **`notify_list`、`notify_cancel`、`notify_ack`**：`notify_ack(id, ack_token)` 中的 token 只出现在最终的通知消息里，用来证明确认来自收到通知的那个 agent。`notify_cancel` 撤销未完成的通知；对已送达未确认的通知，它把状态记为 `dismissed`，不需要 token，也不算确认。
- 以上返回值都带 `daemon` 字段：notifier 的 `state`（9.3）、心跳、配置的间隔和启动命令 `start_command`；`deliver.method` 为 `listener` 时还带 `listen_command`。`notify_create` 和带 notify 的 `job_submit` 另有 `next`，写明这次要运行哪条命令：每次登记或加入后都运行，不以"已在运行"为由跳过。

### 8.7 instructions 与 resources

- **MCP `instructions`**（约 1.5 KB，客户端会放进系统提示）：
  - 先看 `pbs_context` 确认平面；登录节点上不跑重负载。
  - FINISH 和 Exit_status=0 都不等于应用成功。
  - 先 dry-run，再提交。
  - 不在回合里轮询。长等待用通知：提交时带 notify，每次都运行返回的命令（listener 时只有 `listen_command` 能显示消息，只运行 `start_command` 收不到），然后做别的事或结束回合；一个通知一个接收方；收到通知后检查输出，再用最终消息里的 token ack。
  - 等待期间先做可以并行的工作（用户 09-30 的规则）。
- **resources（可选）**：`miyabi://rules`、`miyabi://queues`、`miyabi://jobs/{id}`。

## 9. 通知机制

**术语。** 作业或队列状态变化后告诉 agent，称为"通知"，不叫"唤醒"：agent 等作业时可能正在做别的事，也可能已经结束回合。通知在两种情况下都要送达，agent 忙时排队，空闲时开始新的回合。

### 9.1 结构

```
Miyabi 登录节点（miyabi-mcp）                         agent 所在机器（notifyd，由 agent 启动）
 项目台账：通知记录、作业事件    ◀── notify poll（默认每 180 秒）  daemon
 共享 qstat 快照、条件判定       ─── 已触发的通知 ─────────────▶  本机投递：listener / herdr / tmux /
                                ◀── notify report ─────────────          codex_queue / command / file
```

- **判定在登录节点。** daemon 每次 poll，miyabi-mcp 用共享快照刷新这个 notifier 名下所有未完成通知涉及的作业（跨项目，每个用户约一次 `qstat -ll`），判定条件，返回已触发、尚未投递的消息。
- **投递在 agent 所在机器。** daemon 在本机投递，再把结果报告回去。
- **daemon 由 agent 启动。** 每次登记或加入通知后，agent 都运行返回的命令：`start_command`（`notifyd ensure`），或 listener 的 `listen_command`（它也会 ensure）。ensure 只看本机的状态文件和 pid，已在运行就直接返回，否则在后台启动，所以 agent 不以服务端报告的"在运行"为由跳过（11-05 定，D3）。没有未完成的通知时，daemon 再 poll 两次后报告退出并自行退出，不留常驻进程（9.3）。
- **每台机器、每个用户一个 daemon。** 它的名字（notifier）默认是本机短主机名，服务端按名字分派通知。名字只能用字母、数字和 `_ . -`（不超过 64 个字符，首字符为字母或数字），因为它是两端的文件名，也出现在返回的命令里；返回的命令按 shell 规则加引号。在登录节点上 daemon 直接调用 miyabi-mcp；在工作站上经 `ssh -o BatchMode=yes <alias> <launcher> notify poll …`，复用 ControlMaster。
- **频率。** 用户级 `config.json` 的 `notify_poll_seconds`（默认 180 s）是唯一来源：服务端把它写进返回的 `start_command` 和 `listen_command`（`--poll-seconds`），daemon 在心跳里报告实际间隔，与配置不一致时 `daemon_info` 标出来。已在运行的 daemon 保持原间隔，直到退出或 `notifyd stop`；ensure 不假装已经换了配置。间隔再短也意义有限：共享 qstat 快照最长用 60 s 前的（`poll_max_age_s`），poll 失败时还会退避。不按 walltime 自适应：walltime 只是上限，作业提前结束或失败时更需要及时报告。11-05 先定为 60 s（D4），此前是 300 s，实测检测延迟中位 192 s；同日改为 180 s，减少 qstat 调用。

### 9.2 通知与作业组

- **一个通知可以绑定一组作业。** 条件满足时发一条汇总消息，列出每个作业的状态、Exit_status、用量和日志路径。例如多 seed 实验，等所有 seed 都结束后才通知。
- **加入方式**：
  - 提交时：`job_submit(notify={"name": …})`。同名的 open 通知不存在就创建，存在就加入。已 fired 的通知不再被判定，加进去的作业永远不会被报告，所以它不接收新作业，名字也让给新通知；`notify_update` 同样只改 open 的通知，按名字查找时先找 open 的，再找最近 fired 的。`notify_create` 的重名检查与创建在同一把锁内。
  - 加入在该通知的锁内复查：查名字之后它可能已被封口或触发，这时拒绝加入（`sealed`），提交结果带 `notify_error` 和 warning。同一个作业再次加入（重试）不重复计入。
  - 事后：`notify_create(job_ids=[…])`，或 `notify_update(add_job_ids=[…])`。
- **封口（sealed）。** 只有封口后的通知才会按 `all_ended` 触发，这样第一个 seed 结束时不会误报。
  - 给了 `expected` 时，成员数达到它就自动封口。
  - 用 `job_ids` 创建时立即封口。
  - 其他情况需要 `notify_update(seal=true)`。未封口的通知在 `notify_list` 中会标出来。
- **条件**：`all_ended`（默认）、`any_ended`、`started`。也可以等队列恢复（`queue`），或到期触发（`timeout`）。
- **提前失败通知。** `early_failure: true`（默认）时，任一成员以 failed 结束，就先单独发一条通知；组通知照常在最后发。
- **状态不明。** 成员在活动表和历史里都查不到满 `missing_s`（默认 300 s）就记为 unknown（7.4），PBS 查询短暂故障也可能造成这种情况。unknown 不算结束，`all_ended`、`any_ended` 和 `started` 都不认它（第 4 节"未知就记为未知"）；这时单独发一条"状态不明，请用 job_get 核对"的消息，每个作业一次，组通知继续等，靠 `timeout` 兜底。

### 9.3 daemon（`miyabi-mcp notifyd`）

| 命令 | 作用 |
| --- | --- |
| `notifyd ensure [--remote <alias>] [--notifier <名>] [--poll-seconds <s>]` | 未运行就在后台启动，并返回状态；agent 每次登记或加入通知后都运行。已在运行的 daemon 间隔不同时，结果写明它保持原间隔。随后在本机检查这个 notifier 名下所有未完成通知的投递目标，有到不了的就报 `delivery_target_unreachable` 并以非零退出（见下） |
| `notifyd run` | 前台运行（调试用） |
| `notifyd status`、`notifyd stop` | 查看、停止 |

- **单例。** 用本机状态目录里的锁和 pid 文件保证，状态文件只在持锁时写。工作站上的状态目录是 `${XDG_STATE_HOME:-~/.local/state}/miyabi-mcp/`；Miyabi 登录节点上是 `/work/<group>/<user>/.miyabi-mcp/notifyd/`，不写 `$HOME`。
- **失败处理。** poll 失败后按 2、4、8 倍间隔退避。poll 成功但某个项目读不出来时，结果带 `errors`：这个项目的未完成通知在刷新作业之前就已计入 `open_count` 和 `targets`，所以这一轮不算空闲，daemon 只把 targets 合并进已知目标、不覆盖。连续 3 轮出问题（整次失败或带 errors）时，向上次已知的各通知目标投递一条"通知通道中断"的消息，说明原因和恢复方法（例如用 `ssh -fN miyabi-g` 重新打开主连接），不静默；一轮干净的 poll 后重新计数。
- **心跳与状态。** 服务端在用户级目录的 `notifiers/<名>.json` 里记录：poll 的开始时间（`last_poll`）、最近一次没有 errors 的 poll 的时间（`last_ok`）、daemon 的实际间隔、本轮 errors，以及 daemon 报告的退出（时间、原因、实例）。`daemon_info` 据此给出 `state`：`running`（两个周期加 60 s 内有 poll，且这个实例之后没有报告退出）、`idle_exited`（空闲退出；没有未完成通知时是正常状态）、`stopped`（stop、SIGTERM 或异常退出）、`stale`（心跳超时：进程可能被 `kill -9`，或连接断了）、`never`。`daemon_running` 保留给旧调用方，只在 state 为 running 时为 true。只有这个 notifier 名下有未完成通知、而 state 不是 running 时才给出 `hint`；`notify_list` 的 `summary` 给出未完成数、已送达未确认数和各 daemon 的 state。（11-01 两个通知晚到约 20 分钟，起因是 daemon 空闲退出后，服务端按旧的"两个周期内有 poll"规则仍报告它在运行。）
- **退出与交接。** daemon 决定空闲退出时，先把本机状态写成 `exiting` 并释放锁，再用 `notify poll --exiting idle` 报告。服务端先记下退出，再照常判定。这时又有了未完成通知（或报告发不出去）时，daemon 重新取锁继续运行；取不到锁说明新 daemon 已经接手，它就退出，不再写状态文件。所以新通知恰好在 daemon 最后一次 poll 期间登记时，不会两边都以为对方在管：ensure 不把 `exiting` 当作在运行，会启动新的 daemon。stop、SIGTERM 和意外异常同样报告（`stopped`、`error`）；被 `kill -9` 时按心跳超时判为 stale。工作站上的 daemon 经 ssh 调用同一个命令，两种拓扑一致。
- **登记时检查目标。** 投递目标只有 agent 所在的机器才能检查，服务端只查格式。所以 `notifyd ensure`（agent 登记或加入通知后运行的 `start_command`）启动或确认 daemon 之后，用 `notify targets` 取回这个 notifier 名下未完成通知的目标（服务端不记心跳、不判定、不交出消息），在本机逐个检查：herdr 用 `herdr agent get`，找不到时列出现有的 agent；tmux 用 `display-message`；command 查本机配置；codex_queue 查 codex 是否安装。有到不了的目标时，命令以 `delivery_target_unreachable` 失败，写明是哪个通知、什么原因、怎么改（`notify_update(deliver=…)` 或 `notify_cancel`）；daemon 照常运行。这样登记的 agent 在自己的回合里就能知道。11-06 联合测试中，Codex 研究员把 herdr 目标写成并不存在的 `coordinator`，5 次投递失败后通知标为 failed，没有任何地方显示原因，项目空等了约 8 小时。
- **日志。** `<状态目录>/<notifier>.log` 每行一个 JSON：`started`（pid、host、间隔、启动方式 ensure、listen 或 run）、`exited`（原因、最近一次成功 poll 的时间）、`poll_errors`、`poll_failed`、`delivery`（每次投递尝试的结果，以及之后通知的状态；此前叫 `delivered`，重试和失败也记成它，容易误读）、`given_up`（最终消息放弃投递，以及提醒的结果）、`broken_notice`、`resumed`、`inbox_removed`。
- **关闭名单与收件目录。** poll 结果附上这个 notifier 最近 30 天关闭的通知（状态、关闭时间、送达时间）。daemon 先写 `closed-<notifier>.json`，再写 `known-<notifier>.json`（未完成通知的投递目标），listener 据此说明退出原因（9.4）。通知确认、取消或 dismissed 满 7 天后，daemon 删掉它的收件目录；已送达未确认的保留，消息可能还没被读。
- **本机需要一份 miyabi-mcp。** 即仓库克隆加 `uv sync`。daemon 只用标准库部分，不依赖 MCP SDK。

### 9.4 投递方式（在 agent 所在机器上执行）

| method | 机制 | 适用 | 状态 |
| --- | --- | --- | --- |
| `listener`（默认） | daemon 把消息写入本机收件目录就算送达。agent 用后台命令运行 `listen_command`（`notify listen --id N`），收到消息就退出，退出提示里就是通知内容。listen 还会确保 daemon 在运行。没人运行 `listen_command` 时消息就停在收件目录里：只运行 `start_command` 收不到。收件目录先到先得，不是广播 | Claude Code：agent 忙时提示排队，空闲时开始新回合；其他 agent 用 listener 时同样要运行 `listen_command` | 待实测。同一机制（后台命令退出）已在旧 waiter 上实测通过（10-30） |
| `herdr` | `herdr agent prompt <agent> "<msg>"`，可加 `--machine`。agent 是唯一的 agent 名，或其中有 agent 的 pane id | herdr 中的任意 TUI agent（Claude Code、Codex、pi） | 11-06 联合测试中用过（目标写错的那次见 9.3）。herdr 的错误码记入投递结果（`code`）：`agent_blocked`（停在对话框）和 `agent_not_found`（名字错了，或 agent 正在重启）都稍后重试；登记时由 `notifyd ensure` 检查 agent 是否存在 |
| `tmux` | `tmux send-keys -l` 后再按 Enter | tmux 中的 agent | 未验证，兜底 |
| `codex_queue` | `codex queue --thread … --remote unix://…`，用本机的 app-server | Codex | 旧实现实测确认 21 次，需在 0.163 上复验 |
| `command` | 运行本机 daemon 配置中具名的命令，消息 JSON 从 stdin 传入 | 自定义 | — |
| `file` | 追加到本机的一个文件 | 轮询或其他系统 | — |

`notify listen` 的退出码：0 打印了消息；3 这个通知对该 notifier 已不再未完成（判断依据是开始时间晚于 listener 启动的一次无错误 poll：在 poll 进行中登记的通知不在那次结果里，不能看结束时间），提示写明原因：已取消（时间）；已送达（时间。listener 投递的"已送达"只代表写进了收件目录，不代表 agent 看到了），本机收件目录显示它在某时被取走；或者不在这个 notifier 的名单里，请用 `notify_list` 查它属于哪个 notifier；75 daemon 无法维持。退出码不按原因细分，agent 读的是提示文字。

`codex_queue` 和 `herdr` 仍待验证，验证之前不改默认投递方式。

`codex_goal`（恢复暂停的 Goal）需要在登记通知时于本机读取 Goal 快照，推迟到 v0.2。

### 9.5 通知消息

```
[miyabi-mcp] notification N-20261030-181500-ab12 (E12-seeds): all 3 jobs ended (18:42 JST)
- 1234567.opbs seed-1  F Exit_status=0 (success)  walltime 00:41:12/01:10:00  0.69 node_hours  log …
- 1234568.opbs seed-2  F Exit_status=0 (success)  walltime 00:40:55/01:10:00  0.68 node_hours  log …
- 1234569.opbs seed-3  F Exit_status=1 (failed)   walltime 00:02:03/01:10:00  0.03 node_hours  log …
PBS exit status is not application success: check the outputs before continuing.
Your note: all seeds done: run stats/aggregate.py
Acknowledge: notify_ack(id="N-20261030-181500-ab12", ack_token="k3f9q")
```

消息控制在约 2 KB 以内，详细信息让 agent 用 `job_get` 去取。

只有最终消息带 ack 那一行。提前失败和状态不明的消息写明"组通知仍在等待，这条不需要确认"，不带 token：照着它立刻 ack 会因为通知仍未完成而失败；ack 恰好落在最终消息已触发、还没投递的时候，通知会变成 acknowledged，最终消息就再也发不出去。listener 投递时，这两种消息还提醒再运行一次 `listen_command` 继续等。

### 9.6 状态与确认

```
open ─封口─▶ sealed ─条件满足或超时─▶ fired ─投递─▶ delivered ─ack─▶ acknowledged
  └──────────── cancelled ◀───────────────┘  │  ▲          └─notify_cancel─▶ dismissed
                                             │  └─notify_update(deliver)─┐
                                             └─▶ failed（多次投递失败）──┘
```

- 投递至少一次，agent 用 ack token 去重。
- 投递失败（例如 herdr 一直 blocked）超过 5 次就标为 failed。原因要有处可查，也要有人知道：
  - `notify_list` 的每个通知带 `delivery`（最新一条消息的状态、尝试次数、最后一次结果及其 `code`）；failed 的另有 `failure`，写明尝试次数、最后一次的时间和错误。重试期间也看得到最后一次失败的原因。
  - `job_wait` 和 `job_get` 的 `notifications` 中，failed 的 `next` 写明原因和补救办法，fired 的写明已尝试几次、最后一次的结果。
  - daemon 放弃一条最终消息时，若本机有 herdr，就用 `herdr notification show` 提醒本机的人（`--sound request`），正文是失败原因和原消息。只有目标本身是对的，通知才能自动送达；目标错了，只有人或登记它的 agent 能改。
  - 改正目标：`notify_update(id, deliver=…)`。failed 的通知回到 fired，最终消息从头计算尝试次数、重新投递（同一个 ack token）。
  - 11-06 之前，原因只写在 agent 所在机器上的 daemon 日志里，`notify_list` 和 `job_wait` 只说"见 notify_list"。
- 一次投递的结果分四种：`delivered`；`uncertain`（程序已经运行但超时，可能已送达，按已送达处理，避免重复打扰）；`retry`（例如磁盘满，什么也没送出）；`failed`（找不到程序或没有权限：什么也没送出，重试也没用，通知立即标为 failed）。此前所有 OSError 都算 uncertain，投递程序不存在时最终消息被标成 delivered，不再重试（11-05 审查时复现）。
- 提前失败和状态不明的消息不改变组通知的状态。
- 已送达却没人确认的通知（例如没人运行 `listen_command`，消息从没被读），用 `notify_cancel` 关成 `dismissed`：不需要 token，也不算确认；8.6 的 token 规则不变，dismissed 之后仍可凭 token ack（11-05 定，D6）。job_wait 只列出通知和收尾方法，不返回 token。

### 9.7 各场景的推荐用法

| agent 位置 | Claude Code | Codex | pi 或其他 TUI agent |
| --- | --- | --- | --- |
| 登录节点 | 提交时带 notify，后台运行 `listen_command` | `deliver=codex_queue`，运行 `start_command` | `deliver=herdr` 或 `tmux` |
| 工作站（MCP 由 pi-env-mcp 托管） | 同上，daemon 在工作站上经 ssh poll | 同上 | 同上 |
| 编排者通知 worker | `deliver={"method": "herdr", "agent": "<worker>"}`；会话内 subagent 由编排者收到通知后用 SendMessage 转达 | | |

**一个通知一个接收方**（11-05 定，D5）。通知归提交作业的 agent：结果出来后要接着干活的是它。协调者用 `notify_list`、`job_get` 看状态，不开 listener。双方都要被唤醒时，为同一组作业建两个通知（不同 `name`，各自的投递方式），不共用一个收件目录：listener 收件目录先到先得，两个人一起听，总有一个以退出码 3 结束。

典型流程（工作站上的 Claude Code，三个 seed）：

1. 三个 seed 各提交一次：先 `job_submit(dry_run)`，再 `job_submit(…, notify={"name": "E12-seeds", "expected": 3, "message": …})`。
2. 第一次提交后，在后台运行返回的 `listen_command`；它会启动本机 daemon。后两次加入同一个通知时 listener 已在运行，按返回的 `next` 运行 `start_command`（幂等），不开第二个 listener。
3. 继续做不依赖这些作业的工作。
4. 三个 seed 都结束后（通常晚 1 分钟上下），listener 退出，会话收到汇总通知。中途有 seed 失败时，会先收到一条提前通知（不需要确认），之后再运行一次 `listen_command` 继续等组通知。
5. 用 `job_get` 读输出，用最终消息里的 token `notify_ack`，再把台账导出给 smooth-research。

## 10. 安全、权限与策略

### 10.1 `policy.json`（示例）

```json
{
  "groups": ["<group>"],
  "queues": ["debug-g", "regular-g", "debug-mig", "short-mig", "regular-mig"],
  "per_job": {"max_nodes": 16, "max_walltime": "16:00:00", "max_node_hours": 16},
  "reserve_accept_slots": 4,
  "label_limits": {"agent": {"max_active_jobs": 6}},
  "budget_windows": [{"window": "24h", "max_requested_node_hours": 200}],
  "require_plan": true,
  "auto_commit_below_unit_hours": 0.5,
  "maintenance": "refuse_if_crossing",
  "cancel_scope": "project"
}
```

策略文件不存在时按空策略处理；存在但顶层不是对象（`null`、`[]`、`0` 等）时报 `invalid_policy`，与其他类型错误一样拦住所有写入，不当作"没有限制"。

用户级目录中的 `policy.json` 有两个作用：给各项目提供默认值，并设定用户在所有项目上的合计上限（活动作业数、时间窗内的 node·h）。项目 `.miyabi-mcp/policy.json` 设定本项目的份额和每作业上限。三层上限（组、用户、项目）的检查顺序见 7.7。

数值只是示例。"每作业 16 node·h"和"每 worker 6 个"来自 3.4 中的项目约定；"保留 4 个 accepted 槽位"来自实验平台。上限只用于检查，永远不作为申请的 walltime。

**项目总额 `budget`**（11-05 加）。与 `budget_windows` 并存：时间窗在单一单位内比较一段时间的申请量；`budget` 按"已结束的实测加未结束的预留"限制整个项目，跨单位折算。

```json
{"budget": {"unit": "node_hours", "limit": 200, "warn_at": 0.8,
            "factors": {"node_hours": 1, "mig_instance_hours": 0.25},
            "source": "bypass-lora docs/constraints.md:34"}}
```

- 预计用量 = 已结束作业的实测 + 未结束作业的预留 + 本次请求，各乘其台账单位的系数。预留按请求量（单元数 × walltime）算；运行中的作业只算完整预留，不再加它的部分实测。累加历史申请量会高估（bypass-lora 申请 279.03 MIG-h，实用 152.99），只看实测又会漏掉在跑的作业。
- 被 qsub 拒绝的不算；`FINISH no-start`（从未运行）记 0；已结束但缺实测的、排队、运行、unknown 和提交结果不确定的，都按请求量预留，直到台账知道更多。接管（adopt）的作业也算。
- 预计用量超过 `limit` 时 dry-run 失败；commit 时在用户级提交锁里再算一次（先按共享快照刷新台账），所以几次并发提交不会一起越线。到 `warn_at × limit`（默认 80%）只给 warn。要继续，由用户在 policy.json 里提高 `limit`（11-05 定，D7）。
- `factors` 缺少某个台账单位时 fail closed（无法判断就拦截）。站点 TOKEN 不进 `budget`：它的系数按系统而定，互动队列也有例外。阶段额度（例如某阶段不超过 10 node·h）不进策略：作业带上阶段标签后，`ledger_query(format="summary", labels=…)` 的 `budget` 就是该阶段的用量。

### 10.2 权限提示与工具注解

- 工具都带 MCP 注解：查询类为 `readOnlyHint`；`job_cancel` 为 `destructiveHint`；`job_submit` 不是 destructive，但会消耗预算。客户端可以据此决定是否询问用户。
- 托管时，pi-env-mcp 把工具的描述、schema 和注解原样转给 agent（见它的 README）。
- Claude Code 的示例配置：查询类直接放行，提交和取消要求确认。工具名按 B 的注册（pi-env-mcp 注册为 `miyabi-g`，服务名 `miyabi-pbs`，5.3）；A 直接注册时把前缀 `mcp__miyabi-g__miyabi_pbs__` 换成 `mcp__miyabi-pbs__`。

```json
{"permissions": {
  "allow": ["mcp__miyabi-g__miyabi_pbs__pbs_context", "mcp__miyabi-g__miyabi_pbs__pbs_queues",
            "mcp__miyabi-g__miyabi_pbs__job_list", "mcp__miyabi-g__miyabi_pbs__job_get",
            "mcp__miyabi-g__miyabi_pbs__job_wait", "mcp__miyabi-g__miyabi_pbs__notify_list",
            "mcp__miyabi-g__miyabi_pbs__ledger_query"],
  "ask": ["mcp__miyabi-g__miyabi_pbs__job_submit", "mcp__miyabi-g__miyabi_pbs__job_cancel"]}}
```

### 10.3 其他护栏

- 服务端只运行固定命令：qstat、qsub、qdel、qhold、qrls、tracejob、show_token、`bash -n`，全部以 argv 方式调用。投递用的 `command` 只能引用 `config.json` 中的具名条目。
- 日志读取只限作业自己的输出和错误路径，并有字节上限。
- `Variable_List` 按 8.2 过滤；台账中只记录环境变量名，不记录值，调用方声明可以记录的除外。
- 每个写操作的事件都记录 `actor`：MCP `initialize` 中的 clientInfo、可选的 `actor` 参数，以及标签。
- dry-run 计划 10 分钟后过期；提交时重新校验。
- 服务端自身遵守登录节点边界：不导入项目代码，不扫描大目录，所有命令都有超时。

## 11. 与 smooth-research、pi-env-mcp 的接口

### 11.1 职责

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| pi-env-mcp | 通用传输：exec、文件、终端、后台进程、上传下载 | PBS 语义、研究语义 |
| miyabi-mcp | PBS 语义：平面判定、提交（项目组、队列、walltime、上限）、查询（含 `-H -f`）、取消、等待与通知、用量、PBS 状态 → `execution_state`/`outcome` 的映射、作业台账 | 研究记录、文件传输、任意执行 |
| smooth-research | 研究语义：RunSpec、attempt、绑定、观测排序与去重、结果、证据 | 调度、SSH、轮询 |

代码快照由 smooth-research 的 skill 配合 pi-env-mcp 完成，步骤如下。miyabi-mcp 在这里只负责在提交检查中提示可写的代码目录（8.3）。

1. 本地 `git archive <commit>` 打包；
2. 用 pi-env-mcp 的 `upload` 上传到 `<remote_root>/snapshots/<commit>.tar`；
3. 在远端解压为只读的 `snapshots/<commit>/`；
4. 作业从快照运行，输出写到 `<remote_root>/runs/<attempt>/`。

建议 `<remote_root>` 就取项目目录（7.1），这样快照、运行输出和 `.miyabi-mcp/` 台账都在同一处，也都在 /work 上。smooth-research 的 skill 已按此编写（commit 6ec70cb），并约定：

- 一个研究项目对应一个远端项目目录，不共用。因为 `request_key`（`sr:<ATT>`）只在项目内唯一，共用会撞键。
- `.miyabi-mcp/` 由 miyabi-mcp 管理，agent 不直接修改。
- PBS 日志用默认位置 `.miyabi-mcp/logs/`，日志尾部通过 `job_get` 查看。

### 11.2 Observation 和 Usage 的映射

本节内容已与 smooth-research 会话商定（2026-10-30），仍待用户审。`ledger_query(format="smooth-research")` 按下表输出 JSONL，agent 不需要理解 PBS，原样交给 `rl run record` 即可。

| Observation 字段 | 来源 |
| --- | --- |
| `record_id` | `event_id`，即 `miyabi-pbs:<job_id>:<seq>` |
| `attempt_id` | 记录中的 `client_ref`。没有时省略，由 `rl run record --attempt <ATT>` 补上 |
| `source` | `"miyabi-mcp"` |
| `observed_at` | 事件的 `observed_at`（服务端观测时间，ISO 8601，`+09:00`） |
| `occurred_at` | 事件的 `pbs_time`（PBS 自己记录的 qtime、stime、obittime 等），没有就省略。只用于存证和计算时长，不参与排序 |
| `source_sequence` | 每个作业单调递增的 `seq` |
| `execution_state`、`outcome` | 7.4 的映射。`submitted` 事件为 queued；只有 ended 时 outcome 才可能不是 unknown |
| `raw_state` | PBS 原值，如 `"Q"`、`"H"`、`"R"`、`"F Exit_status=0"`、`"F Exit_status=271"`、`"FINISH no-start"` |
| `source_ref` | 只用事件自身的字段拼成，格式见 7.3 的表，如 `"qsub 1234567.opbs @miyabi-g2"`、`"qstat -f 1234567.opbs @miyabi-g1"`、`"qstat -H -f 1234567.opbs @miyabi-g1"` |
| `execution_ref` | `{kind:"hpc_job", executor_namespace:"miyabi-pbs", opaque_id:"1234567.opbs", locator:"<project_dir>/.miyabi-mcp/jobs/1234567/record.json"}`（经符号链接指向 `submissions/<submit_id>/`，7.1） |
| `correction_of`、`note` | 更正事件（`reconciled`）用 `correction_of` 指向被更正的 `event_id`，并在 `note` 里写明证据（必填），如 `"qstat -H -f re-read: Exit_status=271 after miyabi-mcp cancel"`。按 `seq` 顺序导出，被更正的事件总是先送达 |
| `usage` | 见下 |

- **只导出状态事件。** 导出的是 `submitted`、`queued`、`held`、`running`、`ended`、`unknown`、`reconciled`。`cancel_requested` 和 `annotated` 不导出：取消请求不是状态，结束以 PBS 的事实为准。
- **导出是已存事件的纯函数。** 同一个 `event_id` 每次导出都逐字节相同。只用事件里存下的字段，不掺入导出时的主机名或时间；序列化固定为 UTF-8、键排序、紧凑分隔符（`sort_keys=True, separators=(",", ":"), ensure_ascii=False`），一行一条。smooth-research 按 `record_id` 去重，内容不同会被当作冲突拒收。
- **命名空间固定为 `miyabi-pbs`。** 一经使用不再改动，因为绑定与去重都依赖 namespace + opaque_id。系统（G 或 C）另记在记录里。
- **用量**：只在 ended 且有 `resources_used.walltime` 时给出，缺数据不记、不写 0。smooth-research 按 `(resource_type, unit, hardware, basis)` 分别累计。

  | 类别 | accounting_ref | unit | resource_type | hardware | basis |
  | --- | --- | --- | --- | --- | --- |
  | G 节点 | `miyabi-pbs:<job_id>` | `node_hours` | `gpu_node` | `GH200` | `measured` |
  | MIG 实例 | `miyabi-pbs:<job_id>` | `mig_instance_hours` | `gpu_mig_instance` | `GH200 MIG`（`mig_hardware`） | `measured` |
  | TOKEN（只在调用方要求时导出） | `miyabi-pbs:<job_id>:token` | `miyabi_token` | `site_allocation` | — | `reported` |

  - `source_ref` 写计算依据，格式固定为 `"resources_used.walltime=00:10:17 x nvnodes-g=1"`（MIG 为 `nvnodes-mig=<n>`）。`quantity` 用事件里存下的 6 位小数值（7.3）。smooth-research 按规范化 JSON 比较内容，值必须完全相同。
  - smooth-research 遇到同一个 `accounting_ref` 出现不同数值会报 CONFLICT，所以 TOKEN 必须用不同的 ref。
  - MIG 的 `hardware` 字符串一改，累计就会分裂，所以不轻易改。它写在用户级 `config.json` 的 `mig_hardware` 里（默认值 `"GH200 MIG"`；此前没有默认值，config 里不写时标签为 null），在作业被观测到结束时读取，每个作业只有这一次带 usage 的事件，所以改配置只影响改动之后结束的作业，同一个 `accounting_ref` 不会出现两种内容。10-30 的实测作业里 `nvidia-smi -L` 显示 "NVIDIA GH200 120GB" 上的 `MIG 2g.24gb`，当时定为 `"GH200 MIG 2g.24gb"`。后来查明切片由 PBS 分配，1g.24gb 也会出现，而计费与切片无关（7.5），这个标签会让读账本的人以为所有作业都在 2g 上，所以 11-05 改为 `"GH200 MIG"`，不写规格（D2）。代价是跨越改动的项目在 smooth-research 里多一行 MIG 累计，两行只有 hardware 不同，可以直接相加。以后实现 reconciled 更正时，要沿用原事件的标签。MCP 服务只在启动时读一次配置，已在运行的服务要重启后才用新标签；登录节点上的 notifyd 每次 poll 都重新读取。
- **摘要格式。** 用 `blake2b:<hex>`，`digest_size=32`，与 smooth-research 一致；项目规则不允许 SHA-256。
- **契约样例。** smooth-research 仓库的 `tests/fixtures/miyabi_mcp_export.jsonl`（commit 9398c63，已按 7.3 对齐）按本节，用一个真实作业的 `qstat -H -f` 生成，是 M2 导出的对照基准。字段有出入时以本设计为准，并通知对方修改样例。

### 11.3 关联与幂等

- `request_key` 由调用方给出，例如 `sr:ATT-0003-1`，在项目台账内唯一。用同一个键重复调用会返回同一个作业，所以 agent 重试不会重复提交。重新提交就是新的 attempt，要用新的键，这与 smooth-research 的"一个 attempt 只绑定一个作业"一致。
- `client_ref`（如 attempt id）写入记录，并作为 `MIYABI_CLIENT_REF` 传给作业。调用方也可以通过 `env` 另传 `SR_ATTEMPT_ID`、`SR_SPEC_DIGEST`，作业产出的清单就能带上 attempt id 和 spec 摘要。
- 作业名不强制带 attempt id；建议由 attempt id 生成（`qstat -ll` 显示 64 个字符）。
- **`export_command`。** `job_submit`、`ledger_query` 的返回值和通知消息里都带一条可在 agent 所在机器上执行的命令，把本项目的 smooth-research JSONL 原样写到 stdout（远端时为 `ssh -T -o BatchMode=yes <alias> <launcher> --project-dir <dir> ledger --format smooth-research --jsonl`）。agent 用 `<export_command> > observations.jsonl` 再交给 `rl run record`，数据不经过模型转抄（smooth-research 一侧的要求，避免抄错的一份先入库后，正确的重发被判为冲突）。

### 11.4 端到端测试

1. **离线（不需要新作业）。** 对一个已经结束的作业跑 `qstat -H -f`，按 7.3、7.4、11.2 生成事件，导出 Observation 和 Usage，交给 `rl run record` 和 `rl run usage`，检查去重和排序。smooth-research 一侧正在做这一步，会把匿名化样例放进它的契约测试，供 M2 的导出逐字节对照。
2. **在线。** 测试要求经过 miyabi-mcp，所以要等 v0.1 的 M1 和 M2 可用（提交、台账、smooth-research 导出）；没有 M3 时先用 `job_wait` 代替通知。
   - smooth-research 一侧转述的用户授权上限：`debug-mig`、`select=1`、walltime 10 分钟、最多 6 个作业、合计不超过 0.5 node·h；日志用 `-o` 指到 `/work/<group>/<user>/tmp/` 下的新目录。
   - 第一个 MIG 作业里运行 `nvidia-smi -L`，用来确定 11.2 中的 `hardware` 字符串（后来改为不写规格，见 11.2）。
   - 查询时 debug-mig 占用 12%，debug-g 占用 94%。
3. **代码。** 从只读快照运行（11.1），提交时检查可写目录（8.3）。

## 12. 可选：任务打包与预先占位排队（pilot pool，v0.3）

### 12.1 为什么要做

实验平台 skill 的实测：11 节点作业中位运行 16 分钟，却要中位排队 3.6 小时；项目只能同时运行 16 个作业，单节点作业最多同时占用 16 块 GPU。改为晚绑定 pilot 并把 walltime 设为实测的 1.5 倍后，11 节点 regular-g 的排队中位降到 0.85 小时。"预先占位"就是在任务就绪之前先排上 pilot；"打包"就是让一个多节点 pilot 并行跑多个单 GPU 任务，或让一个 pilot 依次跑多个短任务。

### 12.2 从平台 skill 取什么、不取什么

- **取**（通用部分）：
  - pilot 启动时才读取队列文件，用 `os.link` 认领任务；
  - 认领规则：预计时间 × 1.2 ≤ 剩余时间 − 90 s；
  - walltime = 预计时间 × 1.5，向上取整到 10 分钟，并受上限约束；
  - walltime ≤ 30 分钟且不超过 16 节点的路由到 debug-g；
  - 永远不提交放不下任何任务的 pilot；
  - 按 `--limit` 控制提交，扣除保留槽位；
  - 坏节点闸门：重新提交一次，以退出码 79 结束；
  - 空闲 60 s 退出；多节点 pilot 先等最多 10 分钟；
  - 多节点 pilot 用 `mpirun -np 1 --host <h> /usr/bin/env K=V …` 在每个节点上跑单节点任务（实验平台已验证）；
  - walltime 被杀的尝试单独分类为 `lost`。
- **不取**（实验专用）：spec 展开、science version、canary、选择与 TPE、统计、artifact 登记。

### 12.3 对象与工具

- **Pool**：名称、系统、形状（节点数、每节点 lane 数）、路由规则、`max_pilots`、`max_queued`、walltime 策略、环境准备脚本（模块、venv，由用户审过的文件）、坏节点列表、状态（active 或 held）。
- **Task**：id、argv、cwd、env、所需节点数（v0.3 只支持 1）、`expected_seconds`、优先级、依赖、标签、输出目录。
- **Attempt**：认领信息（作业、主机、lane、时间）、`started`、退出（退出码、是否被停止）、耗时。
- **工具**：`pool_create`、`pool_update`、`pool_hold`、`pool_resume`、`task_enqueue`、`task_list`、`task_cancel`、`pool_status`。通知增加任务条件：某个 pool 中某些标签的任务全部完成。

### 12.4 运行方式

- **登录节点侧的 `pool reconcile`**（幂等，加锁；由 notify poll 或工具调用顺带执行，也可以用 `pool loop` 作为后台进程）：导入认领和标记，对尝试分类，按规则重排队（`failed_infra` 最多 2 次，`lost` 1 次并加长时间），写出 `queue.json`，决定要提交哪些 pilot（走 `job_submit` 同一条路径，带 `pool` 标签，台账可查）。
- **计算节点侧的 `miyabi-mcp pilot run --pool <name>`**（标准库 Python，在作业内运行）：上下文检查 → 坏节点闸门 → 读取队列 → 为每个 lane 认领任务 → 运行 → 写标记 → 空闲退出。
- **占位策略**：`min_queued` 让 pool 在还有任务时保持 k 个 pilot 排队；统计"空启动率"（pilot 没认领到任务就退出的比例），超过 20% 时自动减半。实验平台中这一条是未验证的设计。

### 12.5 风险与验收

- **风险**：空启动浪费节点时间；多个 lane 争用 GPU 显存；`mpirun` 不转发调用者环境（已知陷阱）；远端 rank 写出的文件权限是 600（umask）；`/local` 需要清理。
- **验收**（沿用实验平台的验收项）：
  - 双 pilot 集成测试：3 个 sleep 任务加 1 个放不下的任务，每个短任务恰好运行一次，长任务从不被认领，空闲 pilot 在 60 s 内退出；
  - 用假节点文件和桩 `qsub` 演练坏节点闸门；
  - 并发认领演练；
  - walltime 被杀时归类为 `lost`。

## 13. 实施计划

### 13.1 里程碑

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| v0.1 M1 协议与只读 | stdio MCP 层；CLI 骨架；runner；解析器（作业表、带续行的 `-f`、`--rsc -x`、`--limit`、`--rscuse`、`show_token`、停机横幅）；`pbs_context`、`pbs_queues`、`job_list`、`job_get`；在 g1、g2 采集输出并匿名化成测试样例；跨节点锁的实测 | 与 `qstat_json.py` 对 live 输出结果一致；A、B 两种接入方式在 Claude Code、Codex、pi 中都能工作；双节点锁演练通过 |
| v0.1 M2 台账与写操作 | 台账（记录、事件、幂等键）；用户级索引、项目登记和提交锁（7.7）；策略（三层上限）；`job_submit`（计划、提交、对账）；`job_cancel`；`job_annotate`、adopt；`ledger_query` 及 smooth-research 导出 | 用假 qsub/qdel 覆盖成功、失败、超时、输出无法解析、重复键；在线：提交 → 结束、取消排队作业、取消运行作业、walltime 超限演练（记下 Miyabi 上的 Exit_status） |
| v0.1 M3 通知 | `job_submit(notify=…)` 与 `notify_*`；作业组与封口；提前失败通知；`notify poll/report` 与 notifier 心跳；agent 所在机器上的 `notifyd`（ensure、run、status、stop；单例；空闲退出；通道中断通知）和 listener；投递 herdr、tmux、codex_queue、command、file；`job_wait` | 离线：组通知只在封口且全部结束后触发；提前失败；经假 ssh 的远端模式；空闲退出；通道中断。在线：工作站上的 Claude Code 经本机 daemon 收到真实作业的组通知，包括 agent 正忙时 |
| v0.2 | codex_goal；herdr、tmux 的在线实测；channels（实验，需开关）；作业内标记 hook 和模板更新；`usage_report`；hold/release；台账压缩 | 各适配器的在线演练 |
| v0.3 | pilot pool（第 12 节） | 12.5 |

### 13.2 测试

- **离线测试**：用假 PBS 可执行文件和匿名化的真实输出。在登录节点上用 venv 的 Python 运行，不提交作业。
- **协议测试**：用一个最小的 MCP 客户端驱动 `serve`，覆盖版本协商、取消、进度通知、结构化输出。
- **在线实测**：每项都需要用户授权，只用 1 个 MIG 实例或 1 个节点、walltime ≤ 10 分钟。M2 和 M3 合计约 8–10 个作业；按单节点、每个最长 10 分钟估算，申请上限约 1.7 node·h，实际用量会更少（用 MIG 时更少）。

### 13.3 skill 的迁移

11-05 定：miyabi-mcp 是 agent 操作 PBS 的标准方式，不另设 skill（用法写在服务器 instructions 里，前 512 字符自成一体）。

- `miyabi-pbs-cmd`、`miyabi-pbs-goal-wakeup` 标为过时：description 和 `agents/openai.yaml` 指向 miyabi-mcp，正文前加过时说明（cmd 附命令对照表），正文留作背景。Codex 的 skills 链接已删除；仓库 README 的安装只链接仍在用的 skill。
- `miyabi-pbs-shell-template`、`miyabi-ml-experiment-platform` 中的查询、提交和等待改指向 miyabi-mcp。
- `context.py` 仍是作业内的计算节点检查：模板和 `pilot.pbs` 经 checkout 中的路径（`MIYABI_PBS_CMD_ROOT`）调用，不经 skills 链接。模板改为通过 `MIYABI_MCP` 的路径调用检查（可选加载 job hook）之前，它留在 `miyabi-pbs-cmd`。
- `qstat_json.py`、`job_wakeup.py` 随 skill 保留，不再推荐；已注册的 wake-up 运行到触发为止。
- 恢复暂停的 Codex Goal 尚无替代，待 v0.2 的 `codex_goal` 投递。

## 14. 待决问题

1. **存储位置**：已定，见 7.1。产出放在项目目录（默认 `/work/<group>/<user>/<project>`，可指定）下的 `.miyabi-mcp/`，`$HOME` 下什么都不写。还待定的是：PBS 日志默认放在 `.miyabi-mcp/logs/`（隐藏，自动忽略），还是项目的 `logs/pbs/`（显眼，但需要项目自己在 git 中忽略）？本稿倾向前者，smooth-research 的 skill 也已按前者编写（11.1）。
2. **默认策略**：是否沿用"每作业 16 node·h、每个 agent 标签最多 6 个活动作业、保留 4 个 accepted 槽位"？小作业是否允许省掉 dry-run？
3. **作业内标记**：是否默认启用？建议 v0.1 不包装脚本、只靠 qstat；v0.2 通过模板 hook 让用户选择启用（包装脚本需要转移 `#PBS` 指令，风险较大）。
4. **注册名**：用 `miyabi-pbs`？
5. **channels**：research preview 阶段是否值得做实验性支持？
6. **Miyabi-C**：支持到什么程度？解析逻辑共用，但 C 侧还没有实测。
7. **MIG 的 hardware 字符串**：已定为 `GH200 MIG`（11-05）。10-30 实测的 `GH200 MIG 2g.24gb` 只是那一个作业分到的切片；切片由 PBS 分配，计费相同，标签不写规格（7.5、11.2）。命名空间已与 smooth-research 商定为 `miyabi-pbs`（11.2）。
8. **公开发布**：仓库是否公开、用什么许可证，以及文档中保留多少带日期的站点实测数字。
9. **pilot pool**：是否列入路线图，还是只在实验平台 skill 中保留方法论？
