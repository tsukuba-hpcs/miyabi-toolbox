# bypass-lora 项目复盘：miyabi-mcp 改进建议

2026-11-03，bypass-lora 的协调者 S-0001（claude-opus-5-5）整理。依据是 bypass-lora 的作业账本（`.miyabi-mcp/`）、作业记录、研究记录和 miyabi-mcp 当前代码。miyabi-mcp 最后一次提交是 3da7876（10-31），项目期间（11-01 至 11-03）代码没有变化，所以下面的问题都针对当前版本，行号也指这一版。账本里的时间是 JST。

用户指出的两个问题（阶段报告读不懂、codex 容量错误没人处理）都在 smooth-research 一侧，写在 `smooth-research/reports/bypass-lora-improvements-2026-11-03.md` 里。

## 使用概况

- 459 个作业记录：458 个结束（415 成功、42 失败、1 取消），1 个提交失败（第 1 条里的 vnode 尝试）。
- 请求 279.04 MIG-h，实际用掉 152.99 MIG-h，按项目规则（MIG-h × 0.25）约 38.2 node·h。
- 有用量的 456 个作业，按用量折算的用时中位数是 4 分钟，287 个不超过 10 分钟，313 个不超过 15 分钟。
- 研究者（codex）和协调者（Claude Code）都直接用 MCP 工具提交、等待和接收通知。

## 1. MIG 规格没有记录，账本标签也不对（P0，影响结果的正确性）

**现象**

- 项目在作业里用 nvidia-smi 自测，在多个节点上看到 mgXXXX_mig3 是 1g.24gb，mig0 至 mig2 是 2g.24gb。例如 ATT-0415-1（PBS 3787033，mg1112）的 `mig-profile.json` 写的是 1g.24gb，账本却记成 `"hardware": "GH200 MIG 2g.24gb"`，2.106667 MIG-h。
- 原因：`DESIGN.md:836` 根据 10-30 的一次实测，把 `mig_hardware` 定为 “GH200 MIG 2g.24gb”，写在用户级 config.json；`states.py:40-43` 把这个字符串贴到每个 MIG 作业的 usage 上。`states.py:33` 其实读到了 exec_vnode，但只用来数 `nvnodes-mig`，没有存进作业记录（3787033 的 `record.json` 里没有），事后查不到作业用的是哪块切片。
- 切片也选不了：qsub 拒绝 `vnode=`（“Unknown Resource: vnode”），这次提交在账本里是 submit_failed，dry run 没有拦住。

**对研究的影响**

- 1g 和 2g 上 BF16 的数值结果不同，跨作业的严格一致性检查接连失败：V2-03 重载不一致、ATT-0158-1 的 2g 守卫失败、RB1-02 的 1g 参考值对不上、RV-0087 的 medium 发现，还有校准时的初始化检查因为 CUDA RNG 随切片而变而失败。项目为此记了 D-0019、D-0050、D-0060 等决定，改用同一作业内的参考值，初始化挪到 CPU 上做。
- 计时类的比较受切片影响，必须另外记下切片才能解释。
- 研究者只能自己在作业里写 mig-profile.json 记录切片。

**建议**

1. 作业结束时把 exec_vnode 和 exec_host 存进作业记录和账本。
2. 新增 mig_profile 字段：按站点表从 vnode 后缀推出（本项目观测到 mig3 为 1g.24gb，mig0 至 mig2 为 2g.24gb），可选在作业开头跑一次 `nvidia-smi -L` 核对。现有的 hardware 字符串保持不变，避免 `DESIGN.md:836` 担心的累计分裂。
3. job_get、job_list 和导出都带上 mig_profile，smooth-research 的观测可以直接引用。
4. dry run 检查资源名是否被服务器接受（例如 vnode），让这类提交在 commit 之前就失败；MIG 队列的计划里写明“切片规格不可选，可能是 1g，也可能是 2g”。
5. 1g 和 2g 是否按同样的 MIG-h 计费，本项目没有核实，需要向站点确认后写进文档。

## 2. 通知（P1）

### 2.1 守护进程停了没人知道

- 11-02 和 11-03 各有一次，notify_list 显示 `daemon_running: false`，都是协调者碰巧看到后手动 `notifyd ensure`。守护进程为什么退出，没有查到。
- 正在运行的 listener 每 60 秒会 ensure 一次（`notifyd.py:288-289`）。可是按服务器说明，Claude Code 以外的代理只运行 start_command，不开 listener；这时守护进程一旦退出，就没有人把它拉起来。
- 建议：带 notify 的 job_submit 和 notify_create 检查 daemon_running，没在运行就 ensure，或者让 dry run 的检查失败并给出 start_command；notify_list 把这个状态放在结果最前面。

### 2.2 发现作业结束最多要等 5 分钟

- `notify_poll_seconds` 默认 300（`notify.py:75`、`notifyd.py:26`）。例如 3789152 在 19:29:42 结束，19:34:10 才被观测到，19:35:50 被确认。本项目作业用时的中位数只有 4 分钟，轮询间隔比作业本身还长。
- 建议：根据 walltime 或提交时的预计用时，在作业快结束时加快轮询；或者允许单个通知指定更短的间隔；README 写明可以调 `notify_poll_seconds`。

### 2.3 listener 退出码 3 的含义太宽

- 退出码 3 的提示是 “no longer open (cancelled, already delivered or unknown to this notifier)”（`notifyd.py:283-287`），三种情况共用一个码，而 CLI 文档只写了 0、1、2（`cli.py:4`）。本项目出现 6 次：5 次是研究者自己的 listener 先收到了，1 次是通知被取消。协调者每次都要查 notify_list 才能确认没事。
- 建议：区分“已被别的 listener 收到（附时间和接收方）”“已取消”“未知”，各用一个退出码，并写进文档。

### 2.4 用 job_wait 等完之后，通知留着没确认

- 研究者用 job_wait 等到作业结束，对应的通知仍是已送达、未确认，一次留下 6 个；协调者只好用 `notify listen --id` 取 token，逐个 ack。
- 建议：job_wait 发现某个通知分组的作业全部结束时，在结果里给出该通知的 id 和确认方法；notify_list 单列“已送达未确认”，附上取 token 的命令。

## 3. 账本、预算与导出（P1）

- 项目预算是 200 node·h，MIG 作业按 MIG-h × 0.25 折算，用到 80% 要先报告（bypass-lora 的 `docs/constraints.md`）。账本只给 MIG-h；budget_windows（`policy.py:139-157`）只在单一单位内比较时间窗里的请求量，不做跨单位折算，也不看实际用量的累计。每到阶段结束，协调者都要手算一次 node·h。
- 建议：项目级预算支持一个统一单位和各单位的折算系数；提交时检查请求量，summary 给出实际用量、占比和阈值提醒。
- usage 嵌在每条记录的 `usage` 里（quantity、unit），协调者第一次解析时没找到。README 的导出格式部分给一个完整例子就够了。
- 11-01 有一次导出把两条观测写在同一行，`rl run record --observation @file` 报 “Extra data”，当时改用 `raw_decode` 逐条解析。当前导出的 1,350 条没有复现，10-31 之后代码也没改，原因没有查明。建议给导出加“一行一个 JSON 对象”的回归测试。

## 4. 作业脚本（P2）

- PBS 作业从 $HOME 启动，job_submit 只是在 workdir 下调用 qsub（`submit.py:510-511`），脚本里用相对路径就会失败：ATT-0001-1 退出码 127，ATT-0054-1 的工作目录不是快照目录。两次都是研究者在脚本里补了 cd 才解决。
- 建议：提交时在脚本前面加一段 `cd "$PBS_O_WORKDIR"`（或 workdir）的前导；或者 dry run 发现脚本在 cd 之前用了相对路径时给警告。miyabi-pbs-shell-template 可以提供这段前导，连同 allocation_check 和 `nvidia-smi -L`。
- 占位符检查（`submit.py:26`、`submit.py:165-167`）对整个脚本跑正则，注释也算在内：注释 `# run <label> <sha> <pytest target>` 让检查失败。建议跳过注释行，并在结果里给出行号。

## 5. 其他（P2）

- 11-01 用 job_list 查作业时，部分字段返回 null，只能逐个 job_get。当时没有记下是哪些字段，需要复现后再定。

## 值得保留的做法

- dry run 加 plan_digest 的两步提交，让每个作业在提交前都过一遍检查；459 个作业里只有 1 个提交失败。
- 一个通知覆盖一组作业（例如全部 seed），研究者可以结束回合等通知，不用轮询。
- smooth-research 格式的导出带 attempt_id，观测可以直接记进研究记录。
- 代码可变性检查和只读快照的提示（`submit.py:273-278`）让作业都跑在 git archive 出来的快照上，结果都能追到提交。
