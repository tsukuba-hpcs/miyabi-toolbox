# miyabi-mcp 设计建议

状态：草案，供讨论（2026-10-30）。本文只含设计，不含实现。"已验证"指在 Miyabi 上有实测记录；"未验证"指只有设计或离线测试。站点数字都带日期，会过时，决策前请重新查。

## 0. 摘要

1. **定位。** 把 miyabi-toolbox 中可执行的 PBS 能力做成一个 MCP 服务（本目录 `miyabi-mcp/`），给 Claude Code、Codex、pi 等 agent 提供结构化、可审计、带护栏的 Miyabi 作业接口。四个 skill 保留为知识（作业脚本模板、实验平台方法论、站点规则），脚本迁入服务。
2. **服务端始终运行在 Miyabi 登录节点上**，因为 PBS 命令、作业日志和共享文件系统都在那里。agent 在登录节点上时直接以 stdio 启动；agent 在工作站上时用 `ssh -T miyabi-g … serve` 启动同一个服务，复用 pi-env-mcp 已在用的 ControlMaster。miyabi-mcp 不依赖 pi-env-mcp，两者并列：pi-env-mcp 管文件、终端和传输，miyabi-mcp 管 PBS 语义。
3. **运行环境。** 服务运行在 `miyabi-mcp/.venv` 中（uv 管理，Python ≥ 3.11）。在 Miyabi 上以系统自带的 `/usr/bin/python3.11` 为基础建 venv，不下载解释器，也不往 `$HOME` 写。MCP 协议层用官方 Python SDK，核心逻辑只用标准库。每个工具都有对应的 CLI 子命令，输出同一份 JSON，所以只有 shell 的 agent（例如经 pi-env-mcp 的 `bash`）也能用。
4. **v0.1 覆盖前五项需求**：查询（`pbs_context`、`pbs_queues`、`job_list`、`job_get`）；提交（`job_submit`，先 dry-run 出计划，再凭计划摘要提交）；取消（`job_cancel`）；记录与管理（作业台账、事件日志、查询与导出）；通知（`job_wait` 有界等待；作业组通知、agent 所在机器上的 daemon 投递、确认）。第六项（pilot 占位排队与任务打包）排在 v0.3。
5. **通知（原称"唤醒"）。** agent 等作业时可能正在做别的事，所以这里是"通知"而不是"唤醒"。
   - agent 提交作业后，在自己所在的机器上启动 notify daemon（已在运行就跳过）。daemon 每 5 分钟经 miyabi-mcp poll 一次；条件判定仍在登录节点完成（共享的 qstat 快照加台账），投递在本机进行：Claude Code 用后台 listener，另有 herdr、tmux、codex_queue、具名命令和文件。
   - 一个通知可以绑定一组作业（如多个 seed），全部结束后发一条汇总通知；可选在任一成员失败时提前通知。
   - 通知通道中断（ssh 断开等）本身也会通知 agent，不会静默。
6. **台账。** 每个作业一份 JSON 记录，另有按月追加的事件日志。台账、脚本快照、PBS 日志等必须留在 Miyabi 上的产出，都放在项目目录下的 `.miyabi-mcp/` 中。项目目录默认是 `/work/<group>/<user>/<project>`，用户也可以另行指定。写入用原子 rename 和跨节点安全的锁文件。不用 SQLite，因为两台登录节点上可能同时有多个写者。事件可以 1:1 导出为 smooth-research 的 Observation/Usage。
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
 工作站                                          Miyabi 登录节点（g1 或 g2）
┌───────────────────────────────┐            ┌────────────────────────────────────────────┐
│ Claude Code / Codex / pi      │            │                                            │
│                               │  B: ssh -T │                                            │
│  MCP miyabi-pbs ──────────────┼───────────▶│ miyabi-mcp serve ──▶ qstat / qsub / qdel   │
│                               │            │        │                                   │
│  MCP miyabi-g (pi-env-mcp) ───┼─ C ───────▶│ pi-env daemon ──▶ bash: miyabi-mcp <cmd>   │
│                               │            │        │                                   │
│  notifyd: ssh … notify poll ──┼───────────▶│ miyabi-mcp notify poll（每 5 分钟）         │
└───────────────────────────────┘ 同一个      │        ▼                                   │
                                 ControlMaster│ /work 上的共享状态：                        │
 登录节点上的 agent ── A: stdio ──────────────▶│ <project>/.miyabi-mcp（台账、通知、日志）   │
                                              └────────────────────────────────────────────┘
```

| 方式 | agent 位置 | 启动 | 说明 |
| --- | --- | --- | --- |
| A | 登录节点（tmux 或 herdr 中） | `miyabi-mcp serve`（stdio） | notify daemon 也在这台登录节点上 |
| B | 工作站 | `ssh -T -o BatchMode=yes miyabi-g <path>/bin/miyabi-mcp serve` | 推荐的远端方式，与 pi-env-mcp 共用 ControlMaster |
| C | 工作站，只接了 pi-env-mcp | 经 pi-env-mcp 的 `bash` 调用 `miyabi-mcp <cmd> --json` | 不需要额外注册，功能相同；通知由工作站上的 notifyd 负责（9.3） |

### 5.2 为什么服务端放在登录节点

另一种做法是在工作站跑服务端，通过 ssh 或 pi-env 守护进程远程执行 PBS 命令。不选它的原因：

- PBS 命令、作业日志、作业内标记和台账都在登录节点可见的共享文件系统上。服务端放在本地，每次读文件和执行命令都要跨 ssh，等于重做一遍 pi-env-mcp。
- 放在登录节点，一套代码路径就能同时服务 A、B、C 三种方式。
- 远端的代价可以接受：每个会话一条 ssh 通道，在 ControlMaster 下建立很快；断线后由 MCP 客户端重连（Claude Code 用 `/mcp`）。
- 通知的投递在 agent 所在机器上进行：那里运行一个由 agent 启动的 notifyd（9.3），它经 ssh 向登录节点 poll。

### 5.3 注册示例

服务名建议用 `miyabi-pbs`，避免与本项目中名为 `miyabi-g` 的 pi-env-mcp 服务混淆。agent 在工作站上时，`serve` 还要加 `--notifier <工作站名> --local-launcher <工作站上的 bin/miyabi-mcp> --remote-alias miyabi-g`，这样返回的通知命令和 `export_command` 可以原样执行（9.3、11.3）。

- `<path>` 是 Miyabi 上本仓库的绝对路径；`<project>` 是 `/work/<group>/<user>/` 下的项目目录名（7.1）。
- 经 ssh 启动时，服务端无法从工作目录推断项目，所以按项目注册，并写明 `--project`。

```bash
# Claude Code，agent 在工作站（B）：在本地项目目录中按项目注册
claude mcp add --scope local miyabi-pbs -- \
  ssh -T -o BatchMode=yes miyabi-g <path>/miyabi-mcp/bin/miyabi-mcp serve --project <project>

# Claude Code，agent 在登录节点（A）：写明 --project-dir 最稳妥（服务端工作目录在项目内时也可以省略）
claude mcp add --scope local miyabi-pbs -- \
  <path>/miyabi-mcp/bin/miyabi-mcp serve --project-dir /work/<group>/<user>/<project>

# pi（-l 写入项目的 .pi/mcp.json）
pi mcp add -l miyabi-pbs -- ssh -T -o BatchMode=yes miyabi-g <path>/miyabi-mcp/bin/miyabi-mcp serve --project <project>
```

```toml
# Codex（~/.codex/config.toml 是用户级配置：这里写默认项目，其他项目在工具调用中传 project 参数）
[mcp_servers.miyabi-pbs]
command = "ssh"
args = ["-T", "-o", "BatchMode=yes", "miyabi-g", "<path>/miyabi-mcp/bin/miyabi-mcp", "serve", "--project", "<project>"]
tool_timeout_sec = 120
```

`bin/miyabi-mcp` 是一个 shell 启动器：用 `miyabi-mcp/.venv` 里的 Python 运行服务，不依赖 ssh 非登录 shell 的 PATH；venv 不存在时报错，并给出安装命令（6.1）。

### 5.4 连接与登录节点的注意事项

- **BatchMode。** 主连接（ControlMaster）不在时服务起不来，用户要先 `ssh -fN miyabi-g`（与 pi-env-mcp 的 shareConnection 相同）。ControlPersist（这里是 8 h）是从最后一个会话断开时开始计时的。
- **两台登录节点。** 同一主连接里的会话落在同一台，不同主连接可能落在另一台。台账、通知和轮询快照都在共享文件系统上；进程是否存活靠心跳文件判断，不靠 pid。
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
- 经 ssh 启动时（方式 B），服务端的工作目录是远端 home，无法推断，所以注册时要写明 `--project`（5.3）。
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
  events/<YYYY-MM>.jsonl              本项目所有作业的事件，只追加
  requests/<key-digest>.json          幂等键 → 作业（在项目内唯一）
  plans/<plan>.json                   dry-run 计划（过期删除）
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
| `projects.json`、`index/`、`locks/submit` | 跨项目的项目登记、作业索引和提交锁（7.7） |
| `notifiers/<名>.json` | 各 notifier（daemon）的最近 poll 时间（9.3） |
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
    "execution_state": "ended", "outcome": "success", "raw_state": "F Exit_status=0",
    "pbs": {"job_state": "F", "queue": "debug-g", "exit_status": 0,
            "qtime": "2026-10-30T17:12:05+09:00", "stime": "2026-10-30T17:15:40+09:00",
            "obittime": "2026-10-30T17:26:01+09:00", "exec_host": "mg0123/0*72",
            "walltime_used_s": 621, "nodect": 1},
    "observed_at": "2026-10-30T17:27:02+09:00", "seq": 4
  },
  "usage": {"node_hours": 0.1725, "basis": "measured", "token_reported": 0.2},
  "markers": {"started": null, "exit": null},
  "verdict": {"status": "unreviewed"},
  "notifications": ["N-20261030-181500-ab12"]
}
```

`verdict` 是 agent 或人对应用层结果的结论（`unreviewed`、`verified_success`、`verified_failure`），与 PBS 的 `outcome` 分开。

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

`kind` 的取值：`submitted`、`queued`、`held`、`running`、`ended`、`cancel_requested`、`unknown`、`reconciled`、`annotated`。

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
- **MIG 队列。** `select` 计的是 MIG 实例。实例数取 `exec_vnode` 中 `nvnodes-mig` 之和，单位为 `mig_instance_hours`。实测的一个 debug-mig 作业是 `exec_vnode = (mg1098:mem=26214400kb:ncpus=18:ngpus=1:nvnodes-mig=1)`，即 18 核、25 GiB 主机内存，约为一个节点的 1/4。GPU 的 MIG 规格不在 PBS 记录里，要在作业内用 `nvidia-smi -L` 确认（11.2）。
- **TOKEN 列。** 这是四舍五入后的站点记账值，单独记为 `reported`，与 node·h 分开，`accounting_ref` 也不同（11.2）。
- **缺数据不补。** 没有 `resources_used.walltime`（从未开始，或历史已过期）就不记用量，不写 0。
- **申请量分开记。** 申请量（nodes × walltime）记在 `submit.requested_node_hours`，与实际用量分开。

### 7.6 锁与写入

- **记录**：先写临时文件，再 `rename` 替换。Lustre 上 rename 是原子的，实验平台已经依赖这一点。
- **锁**：用 `os.link` 创建锁文件，目标已存在就失败（实验平台在 /work 上跨计算节点验证过这种"不存在才创建"的语义）。锁文件里写主机、pid 和时间，超过租期视为失效，可以抢占。`fcntl.flock` 在 /work 上用过（goal-wakeup、平台的 controller.lock），但跨两台登录节点的行为没有验证，所以不依赖它。
- **待验证**：状态都在 /work（Lustre）上。`os.link` 和 rename 的语义实验平台已在计算节点之间验证过，但还没在两台登录节点之间验证。M1 中实测：g1 和 g2 上各起一个进程，竞争同一把锁。
- **不用 SQLite**：多个服务实例可能同时在 g1 和 g2 上写。实验平台规避网络文件系统上的 SQLite 写入，也是同一个原因。

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
- **提交锁。** 检查和 qsub 在用户级锁内完成（持锁约 1–2 s），避免两个项目同时提交时都通过同一个余量检查。锁过期视为失效（7.6）。
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
| `job_wait` | 有界等待（默认最长 45 s）多作业条件 | readOnly | `wait --jobs …` |
| `notify_create`、`notify_update` | 为已提交的作业、队列或到期时间创建通知；加入作业、封口 | 写（台账） | `call notify_*` |
| `notify_list` | 通知状态、封口情况、daemon 最近 poll 时间、已投递未确认的通知 | readOnly | `call notify_list` |
| `notify_cancel`、`notify_ack` | 撤销通知；确认已收到 | 写（台账） | `call notify_*` |
| `ledger_query` | 台账查询和统计（node·h 申请量与实用量、按标签）；增量导出（中性事件或 smooth-research 格式） | readOnly | `ledger` |
| `job_annotate` | 标签、备注、应用层结论；`adopt` 把外部提交的作业纳入台账 | 写（台账） | `annotate` |

### 8.2 查询类

- **`pbs_context`**：合并 `context.py` 的结果（`node_role`、`system`、架构、PBS 作业号与节点文件）和服务端信息。远端接入时，它描述的是服务端主机，不是 agent 所在的机器。
- **`pbs_queues`**：
  - 输入：`system`（G、C 或 all）；`include`（rsc、limit、rscuse、token 中的若干项，每一项是一次 0.3–0.8 s 的命令）。
  - 输出：队列树，含路由关系（`regular-g` → small、medium、large、x-large）。每个队列有 `enabled`、`started`、节点范围、`max_elapse_s`、`remain_elapse_s`、`mem_gib`、`mig_sizes`、可用项目；另有各项目的上限和已用量、资源池占用、token 预算和 `scheduled_stop`。
- **`job_list`**：历史默认 `days ≤ 3`、`limit ≤ 200`，并标出数据新鲜度（`observed_at`、`cache_age_s`）。按标签过滤只对台账内的作业有效。
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
  "output": "/work/<group>/<user>/proj/logs/pbs/train-a-{stamp}.log",
  "env": {"SR_ATTEMPT_ID": "ATT-0003-1"},
  "labels": {"project": "proj", "agent": "worker-1"},
  "client_ref": "ATT-0003-1", "request_key": "sr:ATT-0003-1",
  "expected_seconds": 600,
  "note": "smoke of writer v2; next: read results/metrics.json",
  "dry_run": true, "plan_digest": null,
  "notify": {"name": "E12-seeds", "expected": 3, "message": "…"}
}
```

**两阶段。** `dry_run: true`（默认）只校验并返回计划和 `plan_digest`。提交时传同样的参数，加上 `dry_run: false` 和该 `plan_digest`。服务端重新生成计划，比对摘要，并重新检查会变化的部分（上限、占用、维护）。这样保证提交的就是 agent 给用户看过的那份；在 Claude Code 的权限提示里，用户看到的也是完整参数。策略可以允许小作业省掉这一步（10.1）。

**检查项**（每项给出 pass、warn 或 fail）：

- **平面**：服务端在目标系统的登录节点上，`qsub` 可用。
- **队列**：必须是 `--rsc -x` 中的顶层队列，状态为 ENABLE、START；节点数在路由后的子队列范围内；walltime ≤ MAX_ELAPSE；walltime 超过 REMAIN_ELAPSE 时，按策略报警或拒绝（"停机前不会开始"）；`*-mig` 队列中 `select` 计的是 MIG 实例。
- **项目组**：在用户的组中，且列在该队列的 PROJECT 里；未指定时，只有在唯一一个候选组时才自动选取。
- **上限**：`--limit` 的 accepted 和 running 还有余量，并扣除保留槽位；按标签的并发；策略中的每作业上限和时间窗预算。
- **脚本**：文件存在；`bash -n` 通过；没有残留的 `<...>` 占位符（与模板的检查相同）；解析 `#PBS` 指令，报告与命令行选项的冲突（命令行优先）；脚本在 `/tmp` 或节点本地路径下时报警。
- **工作目录与输出路径**：
  - `workdir`（qsub 的工作目录，即作业里的 `$PBS_O_WORKDIR`）默认是项目目录。
  - 输出默认写到 `<project_dir>/.miyabi-mcp/logs/<name>-<stamp>-<id>.log`，可在 `project.json` 或每次提交时修改。
  - 输出路径必须是 /work 上的绝对路径：不能放在 `/tmp`（曾有作业从 `/tmp` 提交导致输出丢失）；放在 /home 下会报警（容量小）。
  - 父目录不存在时自动创建。
- **环境变量**：变量名合法；值中的逗号和引号按 `qsub -v` 规则转义，无法安全转义时拒绝。
- **依赖**：`depend` 允许，但会提醒（`afterok` 链曾经断过，建议改用通知的作业组）。交互分配走 `interactive_plan` + PTY 内的 `interactive-start`，不接受脚本、数组、依赖或批处理输出参数。
- **重复**：`request_key` 已经使用过时直接返回原作业；有同名、同标签的活动作业时报警。
- **代码可变性**：PBS 只在提交时复制脚本，脚本引用的代码要到运行时才读。
  - 这次运行声明了要绑定到一个版本（带 `client_ref` 或 `labels.code_commit`），而 `workdir` 或脚本所在目录仍可写时，给出 warn，并说明补救办法：从只读快照运行（例如 `git archive` 到 `<project>/snapshots/<commit>` 后 `chmod -R a-w`），把 `workdir` 设为它。
  - 没有这种声明时（开发中的运行），只在 pass 里注明代码目录可写，避免每次都报警。
  - `labels.code_commit` 记入台账，供导出和审计使用。
- **默认作业名**：不传 `name` 时，用 `client_ref`（如 attempt id），否则用脚本文件名。

**提交步骤：**

1. 取得 `request_key` 的锁。若已有记录：有作业号就直接返回；记录停在 `submitting` 就先对账（见下）。
2. 在 `submissions/<submit_id>/` 写记录（状态 `submitting`，含计划和新生成的 `submit_id`），并把脚本快照复制进去。
3. 取得用户级提交锁，重新检查三层上限（7.7），然后以 argv 方式运行 `qsub`（不经 shell），超时 60 s。
   - `-v` 中加入 `MIYABI_SUBMIT_ID`、`MIYABI_CLIENT_REF`、`MIYABI_PROJECT_DIR`、`MIYABI_JOB_RECORD`，以及调用方的 `env`。
   - 提交的是快照。PBS 本来就在提交时复制脚本，所以运行语义不变，而记录中的摘要与实际提交的内容一致。
4. 解析作业号（形如 `1234567.opbs`），写入记录，创建符号链接 `jobs/<num>`，在用户级索引中追加一行，再追加 `submitted` 事件（7.3），然后释放提交锁。
5. 如果请求里带了 `notify`，把作业加入通知（9.2），并返回 daemon 状态和要运行的命令（8.6）。

**对账。** qsub 超时或输出无法解析时，记录标为 `submit_uncertain`。随后用 `qstat -f` 查看同名候选作业的 `Variable_List`，找到同一个 `MIYABI_SUBMIT_ID` 就绑定。找不到时报告不确定，必须由调用方显式传 `resubmit: true` 才会再次提交。

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
- **范围**：默认只取消本项目台账中的作业；作业是否属于本人由 qdel 保证。
- `job_hold`、`job_release`（qhold、qrls）放到 v0.2，用于"暂时把资源让给别的实验"之类的场景。

### 8.5 台账类

- **`job_annotate`**：修改标签、备注和应用层结论（`verdict`，附证据路径）。`adopt` 把外部提交的作业纳入台账：读 `qstat -f`，记录 `origin: adopted`。
- **`ledger_query`**：
  - 范围：默认是当前项目的台账；用 `project` 参数查询其他项目。
  - 过滤条件：标签、`client_ref`、时间范围、状态、结果。
  - `summary`：按状态、结果、标签统计数量，以及申请和实际的 node·h。
  - `format`：`records`、`events`（带 `cursor`，可以增量导出）、`smooth-research`（第 11 节）、`csv`。

### 8.6 等待与通知类

- **`job_wait`**：
  - 输入 `job_ids`、`when`（`all_ended`、`any_ended`、`started`）和 `timeout_s`（默认上限 45 s，可配置）。
  - 有 `progressToken` 时每 10 s 发一次进度通知。
  - 返回各作业状态和 `met`。只用于几分钟的短作业，更长的等待用通知（第 9 节）。
- **`job_submit` 的 `notify` 参数**：提交时把作业加入一个通知，按 `name` 创建或加入：

  ```json
  {"notify": {"name": "E12-seeds", "expected": 5, "when": "all_ended", "early_failure": true,
              "deliver": {"method": "listener"}, "message": "all seeds done: run stats/aggregate.py"}}
  ```

- **`notify_create`**：为已提交的作业（`job_ids`，立即封口）、队列状态（`queue`）或到期时间（`timeout`）创建通知。
- **`notify_update`**：加入作业（`add_job_ids`）、封口（`seal`）、修改 `message`。
- **`notify_list`、`notify_cancel`、`notify_ack`**：`notify_ack(id, ack_token)` 中的 token 只出现在通知消息里，用来证明确认来自收到通知的那个 agent。
- 以上返回值都带 `daemon` 字段：本机 notifier 的最近 poll 时间和启动命令 `start_command`。`deliver.method` 为 `listener` 时，还带 `listen_command`，供 Claude Code 用后台命令运行。

### 8.7 instructions 与 resources

- **MCP `instructions`**（约 1.5 KB，客户端会放进系统提示）：
  - 先看 `pbs_context` 确认平面；登录节点上不跑重负载。
  - FINISH 和 Exit_status=0 都不等于应用成功。
  - 先 dry-run，再提交。
  - 不在回合里轮询。长等待用通知：提交时带 notify，运行返回的命令，然后做别的事或结束回合；收到通知后先 ack，再检查输出。
  - 等待期间先做可以并行的工作（用户 09-30 的规则）。
- **resources（可选）**：`miyabi://rules`、`miyabi://queues`、`miyabi://jobs/{id}`。

## 9. 通知机制

**术语。** 作业或队列状态变化后告诉 agent，称为"通知"，不叫"唤醒"：agent 等作业时可能正在做别的事，也可能已经结束回合。通知在两种情况下都要送达，agent 忙时排队，空闲时开始新的回合。

### 9.1 结构

```
Miyabi 登录节点（miyabi-mcp）                         agent 所在机器（notifyd，由 agent 启动）
 项目台账：通知记录、作业事件    ◀── notify poll（每 5 分钟）───  daemon
 共享 qstat 快照、条件判定       ─── 已触发的通知 ─────────────▶  本机投递：listener / herdr / tmux /
                                ◀── notify report ─────────────          codex_queue / command / file
```

- **判定在登录节点。** daemon 每次 poll，miyabi-mcp 用共享快照刷新这个 notifier 名下所有未完成通知涉及的作业（跨项目，每个用户约一次 `qstat -ll`），判定条件，返回已触发、尚未投递的消息。
- **投递在 agent 所在机器。** daemon 在本机投递，再把结果报告回去。
- **daemon 由 agent 启动。** 提交作业并登记通知后，agent 运行返回的 `start_command`（`notifyd ensure`）：已在运行就直接返回，否则在后台启动。没有未完成的通知时，daemon 再 poll 两次后自行退出，不留常驻进程。
- **每台机器、每个用户一个 daemon。** 它的名字（notifier）默认是本机短主机名，服务端按名字分派通知。在登录节点上 daemon 直接调用 miyabi-mcp；在工作站上经 `ssh -o BatchMode=yes <alias> <launcher> notify poll …`，复用 ControlMaster。
- **频率。** 默认每 300 s poll 一次。从作业结束到投递，最多约 5 分钟加一次 poll 的耗时。

### 9.2 通知与作业组

- **一个通知可以绑定一组作业。** 条件满足时发一条汇总消息，列出每个作业的状态、Exit_status、用量和日志路径。例如多 seed 实验，等所有 seed 都结束后才通知。
- **加入方式**：
  - 提交时：`job_submit(notify={"name": …})`。同名通知不存在就创建，存在就加入。
  - 事后：`notify_create(job_ids=[…])`，或 `notify_update(add_job_ids=[…])`。
- **封口（sealed）。** 只有封口后的通知才会按 `all_ended` 触发，这样第一个 seed 结束时不会误报。
  - 给了 `expected` 时，成员数达到它就自动封口。
  - 用 `job_ids` 创建时立即封口。
  - 其他情况需要 `notify_update(seal=true)`。未封口的通知在 `notify_list` 中会标出来。
- **条件**：`all_ended`（默认）、`any_ended`、`started`。也可以等队列恢复（`queue`），或到期触发（`timeout`）。
- **提前失败通知。** `early_failure: true`（默认）时，任一成员以 failed 结束，就先单独发一条通知；组通知照常在最后发。

### 9.3 daemon（`miyabi-mcp notifyd`）

| 命令 | 作用 |
| --- | --- |
| `notifyd ensure [--remote <alias>] [--notifier <名>]` | 未运行就在后台启动，并返回状态；agent 在登记通知后运行 |
| `notifyd run` | 前台运行（调试用） |
| `notifyd status`、`notifyd stop` | 查看、停止 |

- **单例。** 用本机状态目录里的锁和 pid 文件保证。工作站上的状态目录是 `${XDG_STATE_HOME:-~/.local/state}/miyabi-mcp/`；Miyabi 登录节点上是 `/work/<group>/<user>/.miyabi-mcp/notifyd/`，不写 `$HOME`。
- **失败处理。** poll 失败后按 2、4、8 倍间隔退避。连续 3 次失败时，向上次已知的各通知目标投递一条"通知通道中断"的消息，说明原因和恢复方法（例如用 `ssh -fN miyabi-g` 重新打开主连接），不静默。
- **心跳。** 服务端在用户级目录的 `notifiers/<名>.json` 里记录每个 notifier 的最近 poll 时间。`notify_create` 和 `notify_list` 返回 `daemon.last_poll`；超过两个周期没有 poll 时，提示运行 `start_command`。
- **本机需要一份 miyabi-mcp。** 即仓库克隆加 `uv sync`。daemon 只用标准库部分，不依赖 MCP SDK。

### 9.4 投递方式（在 agent 所在机器上执行）

| method | 机制 | 适用 | 状态 |
| --- | --- | --- | --- |
| `listener`（默认） | daemon 把消息写入本机收件目录。agent 先用后台命令运行 `listen_command`（`notify listen --id N`），收到消息就退出，退出提示里就是通知内容。listen 还会确保 daemon 在运行 | Claude Code：agent 忙时提示排队，空闲时开始新回合 | 待实测。同一机制（后台命令退出）已在旧 waiter 上实测通过（10-30） |
| `herdr` | `herdr agent prompt <agent> "<msg>"`，可加 `--machine` | herdr 中的任意 TUI agent（Claude Code、Codex、pi） | 未验证。agent 停在对话框时返回 agent_blocked，稍后重试 |
| `tmux` | `tmux send-keys -l` 后再按 Enter | tmux 中的 agent | 未验证，兜底 |
| `codex_queue` | `codex queue --thread … --remote unix://…`，用本机的 app-server | Codex | 旧实现实测确认 21 次，需在 0.163 上复验 |
| `command` | 运行本机 daemon 配置中具名的命令，消息 JSON 从 stdin 传入 | 自定义 | — |
| `file` | 追加到本机的一个文件 | 轮询或其他系统 | — |

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

### 9.6 状态与确认

```
open ─封口─▶ sealed ─条件满足或超时─▶ fired ─投递─▶ delivered ─ack─▶ acknowledged
  └──────────── cancelled ◀───────────────┘           └─▶ failed（多次投递失败，原因可见）
```

- 投递至少一次，agent 用 ack token 去重。
- 投递失败（例如 herdr 一直 blocked）超过 5 次就标为 failed，并在 `notify_list` 中显示原因。
- 提前失败通知不改变组通知的状态。

### 9.7 各场景的推荐用法

| agent 位置 | Claude Code | Codex | pi 或其他 TUI agent |
| --- | --- | --- | --- |
| 登录节点 | 提交时带 notify，后台运行 `listen_command` | `deliver=codex_queue`，运行 `start_command` | `deliver=herdr` 或 `tmux` |
| 工作站（MCP 经 ssh） | 同上，daemon 在工作站上经 ssh poll | 同上 | 同上 |
| 编排者通知 worker | `deliver={"method": "herdr", "agent": "<worker>"}`；会话内 subagent 由编排者收到通知后用 SendMessage 转达 | | |

典型流程（工作站上的 Claude Code，三个 seed）：

1. 三个 seed 各提交一次：先 `job_submit(dry_run)`，再 `job_submit(…, notify={"name": "E12-seeds", "expected": 3, "message": …})`。
2. 第一次提交后，在后台运行返回的 `listen_command`；它会启动本机 daemon。
3. 继续做不依赖这些作业的工作。
4. 三个 seed 都结束后（最多晚约 5 分钟），listener 退出，会话收到汇总通知。中途有 seed 失败时，会先收到一条提前通知，之后再运行一次 `listen_command` 继续等组通知。
5. 用 `job_get` 读输出，`notify_ack`，再把台账导出给 smooth-research。

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
  "auto_commit_below_node_hours": 0.5,
  "maintenance": "refuse_if_crossing",
  "cancel_scope": "project"
}
```

用户级目录中的 `policy.json` 有两个作用：给各项目提供默认值，并设定用户在所有项目上的合计上限（活动作业数、时间窗内的 node·h）。项目 `.miyabi-mcp/policy.json` 设定本项目的份额和每作业上限。三层上限（组、用户、项目）的检查顺序见 7.7。

数值只是示例。"每作业 16 node·h"和"每 worker 6 个"来自 3.4 中的项目约定；"保留 4 个 accepted 槽位"来自实验平台。上限只用于检查，永远不作为申请的 walltime。

### 10.2 权限提示与工具注解

- 工具都带 MCP 注解：查询类为 `readOnlyHint`；`job_cancel` 为 `destructiveHint`；`job_submit` 不是 destructive，但会消耗预算。客户端可以据此决定是否询问用户。
- Claude Code 的示例配置：查询类直接放行，提交和取消要求确认。

```json
{"permissions": {
  "allow": ["mcp__miyabi-pbs__pbs_context", "mcp__miyabi-pbs__pbs_queues", "mcp__miyabi-pbs__job_list",
            "mcp__miyabi-pbs__job_get", "mcp__miyabi-pbs__job_wait", "mcp__miyabi-pbs__notify_list",
            "mcp__miyabi-pbs__ledger_query"],
  "ask": ["mcp__miyabi-pbs__job_submit", "mcp__miyabi-pbs__job_cancel"]}}
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
  | MIG 实例 | `miyabi-pbs:<job_id>` | `mig_instance_hours` | `gpu_mig_instance` | `GH200 MIG 2g.24gb` | `measured` |
  | TOKEN（只在调用方要求时导出） | `miyabi-pbs:<job_id>:token` | `miyabi_token` | `site_allocation` | — | `reported` |

  - `source_ref` 写计算依据，格式固定为 `"resources_used.walltime=00:10:17 x nvnodes-g=1"`（MIG 为 `nvnodes-mig=<n>`）。`quantity` 用事件里存下的 6 位小数值（7.3）。smooth-research 按规范化 JSON 比较内容，值必须完全相同。
  - smooth-research 遇到同一个 `accounting_ref` 出现不同数值会报 CONFLICT，所以 TOKEN 必须用不同的 ref。
  - MIG 的 `hardware` 字符串一旦使用就不能再改，否则累计会分裂。PBS 记录里只有 `nvnodes-mig=1`、18 核、25 GiB 主机内存，没有 GPU 的 MIG 规格；10-30 的实测作业里 `nvidia-smi -L` 显示 "NVIDIA GH200 120GB" 上的 `MIG 2g.24gb`，所以定为 `"GH200 MIG 2g.24gb"`，写在用户级 `config.json` 的 `mig_hardware` 里。
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
   - 第一个 MIG 作业里运行 `nvidia-smi -L`，用来确定 11.2 中的 `hardware` 字符串。
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

- v0.1 发布后，四个 SKILL.md 改为"有 MCP 工具时优先用工具，否则用 CLI"，保留知识部分。
- `qstat_json.py`、`context.py`、`job_wakeup.py` 保留一个版本，作为调用新包的兼容层，之后删除；`qstat_json.py` 不再有两份副本。
- 作业脚本模板改为通过 `MIYABI_MCP` 的路径调用 context 检查，并可选加载 job hook。
- 更新仓库 README 的组件表和安装说明。

## 14. 待决问题

1. **存储位置**：已定，见 7.1。产出放在项目目录（默认 `/work/<group>/<user>/<project>`，可指定）下的 `.miyabi-mcp/`，`$HOME` 下什么都不写。还待定的是：PBS 日志默认放在 `.miyabi-mcp/logs/`（隐藏，自动忽略），还是项目的 `logs/pbs/`（显眼，但需要项目自己在 git 中忽略）？本稿倾向前者，smooth-research 的 skill 也已按前者编写（11.1）。
2. **默认策略**：是否沿用"每作业 16 node·h、每个 agent 标签最多 6 个活动作业、保留 4 个 accepted 槽位"？小作业是否允许省掉 dry-run？
3. **作业内标记**：是否默认启用？建议 v0.1 不包装脚本、只靠 qstat；v0.2 通过模板 hook 让用户选择启用（包装脚本需要转移 `#PBS` 指令，风险较大）。
4. **注册名**：用 `miyabi-pbs`？
5. **channels**：research preview 阶段是否值得做实验性支持？
6. **Miyabi-C**：支持到什么程度？解析逻辑共用，但 C 侧还没有实测。
7. **MIG 的 hardware 字符串**：已定，10-30 实测为 `GH200 MIG 2g.24gb`。命名空间也已与 smooth-research 商定为 `miyabi-pbs`（11.2）。
8. **公开发布**：仓库是否公开、用什么许可证，以及文档中保留多少带日期的站点实测数字。
9. **pilot pool**：是否列入路线图，还是只在实验平台 skill 中保留方法论？
