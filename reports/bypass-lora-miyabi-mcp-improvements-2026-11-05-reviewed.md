# bypass-lora 复盘：miyabi-mcp 改进设计（审核修订稿）

2026-11-05 修订。底稿是 bypass-lora 协调者 S-0001 在 11-03 写的 `bypass-lora-miyabi-mcp-improvements-2026-11-03.md`，原样保留。Claude（claude-opus-5-5）和 Codex 各自独立审核了底稿，Codex 的完整意见在同目录的 `bypass-lora-miyabi-mcp-improvements-2026-11-03.codex-review.md`。本稿合并两份审核：事实按账本、PBS 历史、站点说明和代码重新核对，建议改写成可以直接实施的设计。两边意见不同或需要用户取舍的地方放在第 6 节，每项都留了选项和理由。时间都是 JST，行号指 miyabi-mcp 的 3da7876，11-05 时仍是最新提交。同日稍后用户提出把各 MIG 切片视作一样，本稿采纳，第 1 节随之简化，D1 取消；其余待决项按本稿的推荐定下，用户级配置里的 MIG 标签也已改好。

## 审核后的主要变化

- MIG 各切片视作一样。1g 和 2g 按同样的 MIG-h 计费，TOKEN 和站点说明都支持这一点，底稿要向站点确认的计费问题可以结案。底稿想按 vnode 后缀推出规格，PBS 的 exec_vnode 里其实没有 `_migN`；现在也不必推了，miyabi-mcp 只改掉写死的 2g.24gb 标签和文档说法。这一节的优先级从 P0 调为第一批的 P1。
- 11-01 有两个通知晚到 20 分钟，直接原因找到了：daemon 空闲退出后，服务端在最多 11 分钟内仍报告它在运行。11-02、11-03 两次看到的 `daemon_running: false` 没有造成可见延迟，原因查不到。底稿"dry run 因 daemon 未运行而失败"的建议不采用。
- `notify_poll_seconds` 实际不控制 daemon 的轮询间隔，按底稿在 README 里写"可以调它"会误导，要先把配置接上。
- 导出时的 Extra data 有了能复现的机制：stdout 和 stderr 合并到同一个文件时，cursor 会和某条观测挤在同一行。
- 预算检查要按"已结束的实测加未结束的预留"来算，不能累加历史请求量。
- 新发现七个问题：单个项目 poll 出错会让 daemon 以为没事可做而退出；unknown 状态被当成已结束；早期失败消息带着整组通知的 ack token；合法的 2、4 实例 MIG 请求被拒；计划摘要不含 notify；非 Claude agent 的通知接收方式没有说清；job_list 对不存在的字段名返回 null。

## 使用概况

- 459 条提交记录，其中 458 个拿到 PBS 作业号：415 个成功、42 个失败、1 个取消，这里的成功只是 PBS 层面正常退出，不等于研究结果有效；另有 1 条被 qsub 拒绝，是第 1 节的 vnode 尝试。
- 拿到作业号的作业共请求 279.03 MIG-h，底稿的 279.04 多算了被拒那次的 1 分钟。账本记录的实际用量是 152.99 MIG-h，按项目规则乘 0.25 为 38.25 node·h，占 200 node·h 总预算的 19.1%。站点 TOKEN 合计 35.7，比 38.25 少，因为 TOKEN 按作业四舍五入到 0.1，很短的作业记成 0.0。
- 457 条 usage 里有 1 条是 0 秒（3757929，即退出码 127 的 ATT-0001-1）。其余 456 条的用时中位数 241 秒，287 条不超过 10 分钟，313 条不超过 15 分钟，底稿的这组数字成立。
- 提交者是 codex 325 次、Claude Code 134 次，都在 miyabi-g2 上。153 个通知的 notifier 都是 miyabi-g2，投递方式都是 listener。

## 1 MIG：各切片视作一样，改掉写错的规格标签（P1，第一批）

### 现象

项目在作业里用 nvidia-smi 自测切片。比如 ATT-0415-1（PBS 3787033，mg1112）测到的是 1g.24gb，账本 usage 写的却是 `"hardware": "GH200 MIG 2g.24gb"`。这个字符串来自用户级 config.json 的 `mig_hardware`，经 `observe.py:92` 和 `states.py:40-43` 贴到每个 MIG 作业上，457 条 usage 全是它。MIG-h 的数量按 walltime 乘实例数计算，没有错，错的是硬件标签：`DESIGN.md:836` 把 10-30 的一次实测当成了所有作业的事实，`README.md:229` 也写着已验证为 2g.24gb。

PBS 的 exec_vnode 形如 `(mg1112:mem=26214400kb:ncpus=18:ngpus=1:nvnodes-mig=1)`，没有 `_mig3` 这样的后缀；切片编号在 exec_host 里，`mg1112/3*18` 中的 3 就是。底稿的 `mgXXXX_mig3` 是项目自己的叫法，所以"按 vnode 后缀推出规格"做不了。

有 15 个作业既写了作业内的 mig-profile.json，又还查得到 PBS 历史。按 exec_host 的编号对照，编号 0、1、2 都是 2g.24gb（分别 4、5、2 个作业），编号 3 都是 1g.24gb（4 个）。Codex 在 11-05 核对了站点的 [MIG 说明](https://www.cc.u-tokyo.ac.jp/supercomputer/miyabi/service/mig.php)：MIG#0 至 #2 是 32 SM、24 GB，MIG#3 是 26 SM、24 GB，提交选项避不开 MIG#3。站点没有写 NVIDIA 的 profile 名和 PBS 编号怎么对应，"编号 3 是 1g.24gb"目前是 15 个样本加站点说明得出的推断。

计费不随切片变化。这 15 个作业的 TOKEN 都等于 MIG-h × 0.25 四舍五入到 0.1，其中 4 个是 1g：3787033（1g，2.106667 MIG-h）的 TOKEN 是 0.5，3787043（1g，1.045 MIG-h）是 0.3。按 GPU 份额计费的话，1g 和 2g 的 TOKEN 会不同。站点的 [token 规则](https://www.cc.u-tokyo.ac.jp/supercomputer/miyabi/service/token.php)写的批处理 MIG 系数也统一是每实例 0.25（Codex 核对），互动 MIG 不消耗 token。

账本不保存 exec_host 和 exec_vnode。record 的 state 只投影 `STATE_KEYS`（`ledger.py:20-21`、`:140`），DESIGN 7.2 示例里的 `state.pbs.exec_host` 没有实现。job_get 的 detail 会显示这两个字段（`states.py:105`），但只在 PBS 历史还在的时候有用。

切片也选不了。`select=1:mpiprocs=1:vnode=mg1112_mig3` 被 qsub 拒绝，报 "qsub: Unknown Resource: vnode"，dry run 没有拦住：`submit.py:47` 的 `select_units` 只数 chunk，不看资源名。

### 对研究的影响

1g 和 2g 上的 BF16 数值不同，跨作业的严格一致性比较因此失败过：V2-03 重载不一致（`research/notes/T-0016-results.md`），RB1-02 的 1g 参考值对不上（`T-0027-results.md`），ATT-0158-1 是项目自己的 2g 守卫主动退出（exit 3）。Codex 核对后指出底稿有两处说宽了：RV-0087 的 medium 是对跨作业门禁的审核意见，不是又一次失败；挪到 CPU 上的只是 E-0029 up-only 初始化，不是整个项目的初始化。数值差异来自站点的切片本身。切片选不了，记下规格也保证不了跨作业逐位一致，miyabi-mcp 区分规格解决不了这个问题。把严格比较放进同一个作业、需要规格时在作业里自测，都由研究设计负责，bypass-lora 的 mig-profile.json 和 2g 守卫就是这么做的。miyabi-mcp 做错的只是把标签写成了 2g.24gb。

### 设计

用户 11-05 提出把各切片视作一样，本稿采纳：用量、预算和导出只按实例数和时间算，miyabi-mcp 不解析切片编号，不推断规格，结果里也不显示规格。审核稿原来的编号解析、规格推断（D1）、规格显示和模板探针都不做了。

1. hardware 标签不再写规格（D2）。用户级 config.json 的 `mig_hardware` 已在 11-05 从 "GH200 MIG 2g.24gb" 改成 "GH200 MIG"，不用改代码。标签在作业被观测到结束时读取（`observe.py:92`），每个作业只有这一次带 usage 的事件；`ledger.py:106-111` 留了 reconciled 更正事件的入口，但没有任何代码生成它。所以改配置只影响改动之后结束的作业，同一个 accounting_ref 不会出现两种内容。MCP 服务只在启动时读一次配置（`cli.py:130`），已在运行的服务要重启后才用新标签；notifyd 每次 poll 都重新读取（`notifyd.py:101`、`:211`）。
2. MIG 队列的 dry run 计划加一句固定说明：切片由 PBS 分配，不能指定，不同切片的算力和 BF16 数值可能不同，需要逐位一致的比较放进同一个作业。
3. 改掉 `DESIGN.md:836` 和 `README.md:229` 的 2g.24gb 说法。README 写明 MIG 按实例计费、系数 0.25、与切片无关，并写上第 2 点的说明；`DESIGN.md:836` 的"标签一旦使用就不能再改"补上这次改动的原因。
4. 账本保存 exec_host、exec_vnode 原文（P2）。这和区分切片无关，是每个作业都有的调度事实：PBS 历史过期以后，还能查到作业跑在哪个节点、哪个切片上，诊断节点故障或数值异常时用得上。只在现有的 running 和 ended 观测路径上（`observe.py` 读 detail 的地方）写进事件和 `record.state.pbs`，原样保存，不解析；后来的观测缺这两个字段时不覆盖。导出只取固定字段，smooth-research 导出逐字节不变。不做也可以，那就删掉 DESIGN 7.2 示例里的 `state.pbs.exec_host`。
5. smooth-research 导出不加规格字段。资源名检查见 4.3。

## 2 通知（P1）

### 2.1 daemon 空闲退出后，服务端仍说它在运行

11-01 那次，最后一个未完成通知在 11:57:42 送达，之后两次空闲 poll（约 12:02:42、12:07:42），daemon 按设计退出（`notifyd.py:221-226`）。研究者在 12:08 和 12:10 提交作业，各建了一个通知（N-20261101-120844-1c52、N-20261101-121026-4aaf）。这时离最后一次心跳只有 1 到 3 分钟，而 `daemon_info` 的判定是"最近 2 × 300 + 60 秒内有 poll"（`notify.py:73-88`），所以返回的 `daemon_running` 仍是 true。12:08 到 12:30 之间没有人运行 ensure，否则新 daemon 的第一次 poll 就会触发这两个通知。直到 12:30:11 又一次提交之后 daemon 才重新启动，两个通知在 12:31:00 触发，比作业结束晚了 21.8 和 20.2 分钟。研究者是因为看到 true 才没运行 start_command，这一点是推断，当时的工具返回没有留下来。

底稿说的 11-02、11-03 两次 `daemon_running: false` 没有造成可见延迟，这两天通知比作业结束最多晚 393 秒和 408 秒。没有未完成通知时，空闲退出后显示 false 本来是正常的，可现在的提示不分这两种情况，只要 daemon 不在就提示运行 start_command。是不是协调者及时 ensure 才避免了延迟，已经查不到：notifyd 只往日志里写 delivered、poll_failed 和 broken_notice 三种事件（`notifyd.py:188`、`:218`、`:231`），不记启动和退出，状态文件又会被下一次启动覆盖。Codex 也指出，研究笔记 `T-0033-progress.md:524` 只能证实有过一次重启。

还有一条静态确定的失败路径，由 Codex 发现、Claude 核对：`notify.poll` 把单个项目的 MiyabiError 收进 `errors`，结果仍是 ok，这个项目的未完成通知也不计入 open_count 和 targets（`notify.py:342-365`）。daemon 不看 errors，照样清零失败计数，用本轮 targets 覆盖 known，按 open_count 为 0 记一次空闲（`notifyd.py:212-224`）。如果有未完成通知的那个项目连续两轮出错，daemon 会以 exited_idle 退出，也不发通道中断消息。没有证据表明本项目发生过；现有测试只覆盖整次 poll 失败（`tests/test_notify.py:167`）。

1. 把心跳拆开报告。现在的心跳在 poll 开始时写入（`notify.py:336`），说明不了这一轮成功了。`notifiers/<name>.json` 改为记 poll 开始时间、最后一次成功时间、实际间隔、本轮 errors，以及 daemon 退出时报告的退出时间和原因。daemon_info 据此给出 `state`：running、idle_exited、stopped、stale（心跳超时，进程可能被强杀）或 never。旧字段 `daemon_running` 保留，含义写进文档。
2. daemon 退出时向服务端报告，并且不和新通知冲突。daemon 决定退出时，先把本机状态写成 exiting 并释放锁，再用一次 `notify poll --exiting <reason>` 报告。服务端先写退出时间，再照常判定；如果这时又有了未完成通知，daemon 重新取锁继续运行，取不到锁说明已经有新 daemon 接手，它就退出。这样新通知恰好在 daemon 最后一次 poll 期间登记时，不会两边都以为对方在管。stop 和 SIGTERM 同样报告，被 kill -9 时仍按心跳超时判为 stale。工作站上的 daemon 经 ssh 调用同一个命令，两种拓扑一致，进程是否存活仍然靠心跳文件判断（`DESIGN.md:222`）。
3. 提示只在需要时出现：这个 notifier 名下有未完成通知、而 state 不是 running 时，job_submit、notify_create 和 notify_list 才提示运行返回的命令；没有未完成通知时 idle_exited 是正常状态，不提示。notify_list 增加概况字段：未完成数、已送达未确认数、daemon state。底稿想把状态"放在结果最前面"，JSON 的键顺序靠不住，用概况字段代替。
4. notifyd 日志增加 started（pid、host、间隔、启动方式）和 exited（原因、最后一次成功 poll 的时间）两种事件，poll 有 errors 时记 poll_errors。
5. 部分失败不算空闲。poll 先数未完成通知再刷新作业，刷新失败不影响 open_count 和 targets；有 errors 的一轮不计入空闲，连续 3 轮有 errors 时按现有的通道中断消息处理（`notifyd.py:176-188`）。
6. daemon 仍由 agent 启动（D3）：每次登记或加入通知后都运行返回的命令，不以"已在运行"为理由跳过，服务端 instructions 和 job_submit、notify_create 的返回值照此写明。ensure 本身是幂等的，它看本机状态文件和 pid（`notifyd.py:122-144`），不依赖服务端的心跳判断。
7. 说清非 Claude agent 怎么接收，这是 Codex 的发现。服务端 instructions 只说"其他 agent 运行 start_command"（`server.py:44`），而默认的 listener 投递只把消息写进本机收件目录就算送达（`deliver.py:48-50`），没人运行 listen_command 时消息就停在那里。instructions 和返回值要写明：deliver 为 listener 时必须运行 listen_command，只运行 start_command 收不到。codex_queue 和 herdr 在 DESIGN 9.4 里仍标为待验证，验证之前不改默认投递方式。
8. 底稿"dry run 因 daemon 未运行而失败"的建议不采用。通知是先登记、再由 agent 启动 daemon，dry run 时 daemon 不在运行是常态，它也和作业能不能提交无关。

### 2.2 轮询间隔的配置没有接到 daemon 上

daemon 的间隔只来自命令行的 `--poll-seconds`（`cli.py:42` 默认 300，`notifyd.py:236`），服务端生成的 start_command 和 listen_command 都不传这个参数（`notify.py:83`、`:85`）。`notify_poll_seconds` 只用于心跳判断和显示（`notify.py:75`、`:367`），daemon 不读 poll 结果里的 `poll_seconds`，已在运行的 daemon 被 ensure 时也不会换成新配置。`config.py:32` 的 `poll_seconds: 60` 没有任何代码读取。按底稿在 README 里写"可以调 notify_poll_seconds"，用户调了也不会生效，反而让心跳判断和实际间隔对不上。

实测检测延迟用通知触发时间减去组内最后一个作业的 obittime 来算：152 个通知的中位数 192 秒，p90 307 秒，147 个在 360 秒以内，超过 11 分钟的只有 11-01 那两个。本项目作业用时的中位数是 4 分钟。延迟也不只取决于间隔：共享 qstat 快照最长会用 60 秒前的（`poll_max_age_s`），作业提交后 120 秒内不去查历史（`observe.py:112-113`），poll 失败时还会退避。底稿的例子 3789152 要分开看（Codex 核对）：PBS 结束 19:29:42，账本观测 19:34:10，通知送达 19:35:16，ack 19:35:50。从结束到送达 334 秒，之后的 34 秒是 agent 自己的处理时间。

1. 让 `notify_poll_seconds` 成为唯一来源：服务端把它写进 start_command 和 listen_command；daemon 报告实际间隔，和配置不一致时在 daemon_info 里标出来，ensure 不假装已经换了配置。命令行默认值跟配置默认值一致。删掉 `config.py:32` 的 `poll_seconds` 和 `tests/base.py` 里对应的设置。
2. 默认值定为 60 秒（D4），再短没有意义，因为快照本身最长 60 秒。上线后第一周记录 qstat 调用次数和检测延迟，调用量有问题就改成 120 秒。
3. 不按 walltime 自适应，两份审核意见一致：walltime 只是上限，作业提前结束或失败时更需要及时报告；每个通知单独轮询会放大 qstat 调用。确有需要时，由一个 daemon 统一调度，设全局最低间隔。

### 2.3 listener 退出码 3 说不清原因（P2）

listener 在一次成功的 poll 之后，发现通知 id 不在 known 里、收件目录也没有待取消息，就以退出码 3 结束（`notifyd.py:281-287`）。known 只含未完成的通知，所以已取消、已送达且被另一个 listener 取走（它把 json 改名为 `.shown`，`notifyd.py:271-277`）、notifier 不对、id 写错，都是同一个 3。底稿记录本项目出现过 6 次，5 次是研究者自己的 listener 先收到，1 次是通知被取消；账本能证实那次取消（N-20261101-180134-9305），其余来自协调者的记录，通知文件里恢复不出来。退出码 75（daemon 无法维持）已经实现（`notifyd.py:258-264`、`:290`），`cli.py:4` 却只写了 0、1、2。

这里采用 Codex 的意见：保留 3 的含义，在提示里写明具体原因，不再为每种原因分配退出码。agent 读的是提示文字，暂时没有调用方需要区分退出码。

1. poll 结果附上这个 notifier 最近关闭的通知及其状态和时间，daemon 把它和 known 写在一起。
2. listener 退出前据此说明原因：已取消（时间）；已送达（时间），本机收件目录显示它在某时被取走；或者不在 notifier X 的名单里，请用 notify_list 查这个通知属于哪个 notifier。listener 投递的"已送达"只代表写进了收件目录，不代表 agent 看到了，提示里不能说成已读。
3. `cli.py` 的说明和 README 写全 listener 的退出码 0、3、75。

### 2.4 已送达的通知没人收尾（P2）

153 个通知里 132 个已确认、20 个已送达未确认、1 个已取消。20 个未确认的里面，16 个的消息还留在收件目录的 json 里，从没被读过；4 个已经显示过，只是没 ack。收件目录现有 152 个子目录，从不清理。ack 需要消息里的 token（`notify.py:240`），notify_cancel 只接受未完成的通知（`notify.py:227-228`），所以通知送达之后，没看到消息的人就没法把它关掉。底稿说"一次留下 6 个、都是 job_wait 造成的"，账本里没有 job_wait 的调用记录，证实不了；那 16 个从未被读的消息，也可能是 2.1 第 7 点说的没人运行 listen_command。

已送达的通知不再被轮询，积压只影响查看和清理，不影响运行。job_wait 只刷新作业、判断条件（`notify.py:439-461`），不碰通知；它可能只等了一组作业中的几个，知道作业结束也不等于收到了整条通知消息。

1. job_wait 的结果列出这些作业所属通知的 id、状态和收尾方法，覆盖 early_failure、timeout、未封口和一个作业属于多个通知的情形。
2. notify_cancel 也能关闭已送达未确认的通知（D6），状态记为 dismissed，不需要 token，也不算确认；ack 的 token 规则保持 DESIGN 8.6 的约定。
3. notify_list 单列待确认的数目，概况字段见 2.1 第 3 点；`notify_list(status=["delivered"])` 的过滤已经有了。
4. 通知确认、取消或关闭满 7 天后，daemon 根据 2.3 的关闭名单删掉它的收件目录。
5. 一个通知只有一个接收方（D5）：通知归提交作业的 agent，协调者用 notify_list、job_get 看状态，不开 listener；双方都要被唤醒时，为同一组作业建两个通知。listener 收件目录是先到先得，不是广播，底稿记录的 5 次退出码 3 都是协调者和研究者在抢同一个收件目录。README 和服务端 instructions 写明这条约定。

### 2.5 另外三处通知状态问题

前两处是 Codex 静态发现、Claude 核对的，第三处是 Claude 发现的，都没有证据表明在本项目发生过。

**unknown 被当成已结束（P1）**，`notify.py:19` 的 `SETTLED` 含 unknown，所以组里有作业处于 unknown 时，通知会说 "all N jobs ended"，job_wait 也返回 met，`started` 条件同样接受它。作业在活动表和历史里都查不到满 300 秒（`missing_s`）就会变成 unknown（`observe.py:136-142`），PBS 查询短暂故障也可能造成这种情况。这违反了 DESIGN 第 4 节"未知就记为未知，不推断"的原则（`DESIGN.md:148`）。改为：`all_ended` 和 job_wait 只认 ended；作业变成 unknown 时，像早期失败那样单独发一条"状态不明，请用 job_get 核对"的消息，组通知继续等，靠 timeout 兜底。

**早期失败消息带着整组的 ack token（P1）**，`render` 对 early 和 final 消息都附上 `notify_ack(... ack_token=...)`（`notify.py:284`）。agent 照着早期消息立刻 ack，会因为通知还是 open 而得到 invalid_state（`notify.py:242`）；如果 ack 恰好落在 final 已触发、还没投递的那段时间，通知会变成 acknowledged，poll 只看 open 和 fired，final 消息就再也发不出去。Codex 统计本项目有 4 个通知发过早期消息。改为：早期消息不带 ack 那一行，写明"组通知仍在等待，这条不需要确认"。

**加入通知失败时只多一个字段（P2）**，作业已经提交时，job_submit 把加入失败放进 `notify_error`，ok 仍为 true（`submit.py:533-537`）。作业确实存在，ok 为 true 没错，但 agent 容易漏看，结果里要醒目地写明"这次提交没有通知"。

## 3 账本、预算与导出（P1）

### 3.1 预算按实测加在途预留来算

项目规则在 bypass-lora 的 `docs/constraints.md:34`：总算力不超过 200 node·h，MIG 作业的 node·h 等于账本的 mig_instance_hours × 0.25，到 80% 时协调者先报告，超出前必须由用户同意；同一文件的预算核算条目要求 node·h 只按实测换算。bypass-lora 和用户级目录都没有 policy.json，`budget_windows` 没有启用。它本来也只在单一单位内比较时间窗里的请求量，不跨单位折算，不看实测（`policy.py:139-157`），是另一种预算口径，算法本身没有错。每到阶段结束，协调者都要手算一次 node·h。

累加历史请求量会高估：本项目请求 279.03 MIG-h，实际只用了 152.99。只看累计实测又会漏掉正在跑的作业。

1. policy.json 增加项目总额 `budget`，和 `budget_windows` 并存，不替换它：

   ```json
   {"budget": {"unit": "node_hours", "limit": 200, "warn_at": 0.8,
               "factors": {"node_hours": 1, "mig_instance_hours": 0.25},
               "source": "bypass-lora docs/constraints.md:34"}}
   ```

2. 预计用量 = 已结束作业的实测 + 未结束作业的预留 + 本次请求，都乘各自单位的系数。预留按请求量（单元数 × walltime）算，运行中的作业只算完整预留，不再加它的部分实测；被 qsub 拒绝的不算；确认没运行过的记 0；已结束但缺实测的、提交结果不确定的，按请求量预留到对账为止。commit 时在现有的提交锁里再算一次，防止几次并发提交一起越线。这几条口径来自 Codex 的审核。
3. summary 在原生单位的 requested、used 之外增加 `budget`：已用、预留、合计、占比、阈值和缺实测的作业数。TOKEN 合计单列，不混进 node·h，因为站点 token 的系数按系统而定，互动队列也有例外。阶段用量（比如 V2 不超过 10 node·h）先不进 policy：作业带上阶段标签后，用 ledger_query 的 labels 过滤 summary 就能得到。
4. summary 的 requested 不再算被拒的提交。现在 summary 对所有记录累加请求量（`jobs.py:373`），预算窗口却排除 submit_failed（`policy.py:126`）。默认只算拿到作业号的，被拒的另列一项。
5. 超限时拦截（D7）：提交后的预计用量超过上限时，dry run 和 commit 都失败，要继续由用户在 policy.json 里提高上限；到 80% 只给 warn。拦截依赖第 2 点的预留口径，口径算错会误拦，所以和那几条测试一起交付。

### 3.2 导出的 Extra data 来自 stdout 和 stderr 合并

`ledger --jsonl` 先把观测逐行写到 stdout，最后把 `{"next_cursor":…}` 写到 stderr（`cli.py:164-167`）。stdout 重定向到文件时是块缓冲，stderr 不是，用 `> f 2>&1` 合并时，cursor 会插在 stdout 已经刷出的内容后面，常常紧跟在某条观测的 `}` 之后，形成 `{…}{"next_cursor":…}` 这样的一行。用项目 venv 的 Python 3.11 合成测试 160 次，158 次复现了 "Extra data"；系统自带的 Python 3.9 没有复现，只重定向 stdout 时也不会出现。

smooth-research 的 `load_json_records` 从 10-30（smooth-research 的 3760c82）起就接受 JSON Lines（`util.py:132-145`），所以 rl 报 Extra data，说明文件里有一行装了两个 JSON 值，不是多行文件交给了只收单个文档的解析器。Codex 认为没有原始文件就不能认定导出有问题，这一点成立：11-01 的原始文件和命令都没有保留，合并输出是最可能的原因，但不算已证实。底稿说"当前导出的 1,350 条没有复现"，现在账本有 1,358 条事件，其中 1,353 条属于导出种类，逐行都能解析（Codex 核对）。

1. stdout 写完先 flush，再写 stderr。
2. 只有调用方传了 `--cursor`（空串表示从头开始）时才输出 next_cursor。smooth-research 不用 cursor，DESIGN 11.3 的 export_command 用法也只重定向 stdout。
3. 补 CLI 输出契约测试：至少两条观测，含非 ASCII 字符和字符串里的换行，每行一个对象，末尾有换行；另跑一次把 stderr 合并进 stdout 的情形，确认每行都能单独解析。不在 smooth-research 的导入路径里加 raw_decode 之类的容错。

### 3.3 README 补导出格式（P2）

README 现在只有工具表和策略示例。补一节导出格式，给一条完整的 Observation 例子，说明原生记录的 `usage.quantity` 和 `usage.unit`、Observation 里的 `usage.accounting_ref`、什么时候没有 usage、实测为 0 的情形，并区分 MCP 返回的 `observations` 数组和 CLI `--jsonl` 的逐行对象。

## 4 提交检查与作业脚本（P2，4.3 的 MIG 单元数为 P1）

### 4.1 工作目录：说清楚，不改写脚本

ATT-0001-1（3757929）的脚本第 3 行用相对路径运行，退出码 127。ATT-0054-1（3761331）的脚本没有 cd，作业目录是 `/home/x10041`，`PBS_O_WORKDIR` 却正确指向快照（Codex 核对日志）。qsub 的 cwd 生效了，是脚本自己没有切换目录。workdir 本来就定义为 qsub 的工作目录、即作业里的 `$PBS_O_WORKDIR`（`DESIGN.md:541`），三个模板也都已经有 `cd "$PROJECT_ROOT"`。

两份审核都不赞成提交时自动往脚本里插 cd：PBS 只解析第一条可执行语句之前的 `#PBS` 指令，插的位置不对，后面的指令就会失效；依赖从 $HOME 启动的脚本会变；提交的字节也不再是用户看过的脚本。按"cd 之前是否用了相对路径"做静态分析，碰到函数、变量、source、heredoc 和子 shell 都不可靠，也不做。

1. 工具说明（`tools.py:99` 现在只写 "qsub working directory"）和 README 写明：批处理作业从 $HOME 开始，workdir 只决定 `$PBS_O_WORKDIR`，脚本要自己 cd。示例放在 `#PBS` 指令之后：`cd -- "${PBS_O_WORKDIR:?}" || exit 1`。
2. dry run 增加 `script.cwd` 检查：脚本里没有 cd 或 pushd 时给 warn，附上这行示例。

### 4.2 占位符检查：缩小匹配，报出行号

`PLACEHOLDER` 正则 `<[A-Za-z][A-Za-z0-9_:./ -]*>`（`submit.py:26`）对整个脚本运行（`:165`），注释 `# run <label> <sha> <pytest target>` 因此失败。允许空格的写法还会误伤正常的 shell 重定向：`python x.py <config.yaml >train.log` 里的 `<config.yaml >`，`cat <<EOF >notes.txt` 里的 `<EOF >`，`sort <in.txt >out.txt` 都会被当成占位符。`allow_placeholders` 可以整体放行（`:166-169`），但那样整个脚本就不查了。

模板的占位符都不含空格，有的在 `#PBS` 行（`<eligible_batch_queue>`、`<HH:MM:SS>`），有的在命令行（`<entrypoint.py>`）。Codex 指出注释不能简单跳过：`#PBS` 在 shell 看来是注释，却有提交语义；heredoc 里以 # 开头的行是数据。

1. 正则不再允许空格，上面三种重定向就不会命中，模板的占位符照样能查到。`'<html>'` 这类字符串仍会命中，用 `allow_placeholders` 放行。
2. 失败时报出行号和匹配到的文字，提示改写注释或用 `allow_placeholders`。
3. 注释照常检查。底稿那条注释里的 `<label>`、`<sha>` 仍会失败，有了行号，改写一下注释就行。

### 4.3 MIG 的 select：合法请求被拒，非法资源没拦

**2、4 实例的 MIG 请求被拒（P1）**，`qstat --rsc -x` 显示 debug-mig、short-mig、regular-mig 都是 `MIN_NODE=MAX_NODE=1`、`AVAIL_MIG=1,2,4`。`parse.py:177` 解析了 mig_sizes，但 `_route`（`submit.py:97-126`）拿 select 的单元数和节点数上下限比，没用 mig_sizes，于是 `select=2` 和 `select=4` 在 shape 检查就失败了。本项目 459 次提交都是 1 个实例，没有碰到。改为 MIG 队列按 mig_sizes 检查，测试 1、2、4 通过、3 失败，离线 fixture `tests/fixtures/rsc_x.txt` 已有这组数据。

**资源名不检查（P2）**，按本站 qsub 手册列出的 chunk 资源检查 select：已知不支持的 vnode 直接失败，并说明 MIG 切片不能指定；集合之外的名字给 warn，写明"未确认"。dry run 不能也不该通过试投或 `qsub -h` 来判断服务器接不接受。

### 4.4 计划摘要不含 notify（P2）

dry run 的摘要字段 `STABLE`（`submit.py:34-35`）和计划（`:284`）都不含 notify，commit 时却用调用时传入的 notify 创建或加入通知（`:535`）。同一个 plan_digest 下可以换掉通知的目标、投递方式或分组，两步提交锁定的并不是整个请求。改为把规范化后的 notify 放进计划和摘要，分组的当前状态仍在 commit 时复核。这条是 Codex 静态发现的，本项目没有发生过。

## 5 其他

### 5.1 job_list 的 null

job_list 的 `fields` 投影对不存在的字段名直接返回 null（`jobs.py:143-144`），`tools.py:78` 也把 `fields` 声明成任意字符串，写错字段名或写成 `ledger.usage` 这样的点路径都会得到 null。还有几种 null 是正常的：排队中的作业没有开始时间和用量，MIG 作业没有 nodes、G 作业没有 mig，其他项目的作业没有 ledger。11-01 当时是哪一种，已经无法复现。

1. `fields` 在 schema 里列出可选的顶层字段名，未知的名字返回 invalid_arguments 并给出可选列表；不做嵌套投影。
2. 工具说明写明常见的 null 及其含义，不把 null 改成 0 或空串。

### 5.2 事实更正

- 3da7876 的提交时间是 2026-10-31T16:35:42Z，即 JST 11-01 01:35，底稿写的 10-31 是 UTC 日期。项目第一个作业在这之后，"项目期间代码没有变化"仍然成立。
- DESIGN 的摘要、5.4 和 7.6 都按两台登录节点（g1、g2）来写，这次审核就运行在第三台 miyabi-g3 上。
- DESIGN 7.2 示例里的 `state.pbs.exec_host` 没有实现，第 1 节第 4 点把它列为 P2。

### 5.3 值得保留的做法（修正措辞）

- dry run 加 plan_digest 的两步提交，保留。458 个被接受、1 个被 qsub 拒绝只是提交记录的统计，不含 dry run 拦下的次数，说明不了护栏是否完备；摘要也还没覆盖 notify（4.4）。
- 一个通知覆盖一组作业，保留，前提是已封口、投递方式能到达 agent、接收步骤已经就绪。
- 导出带 attempt_id 只对有 client_ref 的作业成立：本项目 458 个作业里有 438 个，1,353 条可导出事件里有 1,295 条带 attempt_id。
- 代码可变性检查的提示保留，但它只检查 workdir 和脚本所在目录是否可写并给 warning，不创建快照，也不验证源码。459 条记录里 436 条的 workdir 在 `/snapshots/` 下，没有一条带 `labels.code_commit`，所以"作业都跑在快照上、结果都能追到提交"说得过满。

## 6 决定事项

11-05 全部定下：D1 取消，其余按本稿的推荐。每项保留当时的选项和理由，两份审核意见不同的，写明对方的看法。

### D1 MIG 规格从哪里来（取消）

用户 11-05 提出把各切片视作一样，本稿采纳，这一项不再需要。原来的两个选项是用编号表推断规格（Claude 推荐）和只存原文、规格靠作业内探针（Codex 倾向）；现在两种都不做，需要规格的研究在作业里自测。

### D2 MIG usage 的 hardware 标签（定为 B，配置已改）

两个选项都不用改代码，区别只在用户级 config.json 的 `mig_hardware` 改不改。

- A：保持 "GH200 MIG 2g.24gb"，文档注明它只是记账用的名字，不代表切片规格。累计不分裂，但字面上仍是错的。
- B：改成 "GH200 MIG"。改动之后结束的作业用新标签，已有事件不变。

选 B。计费与切片无关，标签就不该写规格；15 个样本里有 4 个在 1g 上，标签却写着 2g.24gb，读账本的人会以为所有作业都在 2g 上。代价是跨越改动的项目在 smooth-research 里多一行 MIG 累计，两行只有 hardware 不同，可以直接相加，按 hardware 分桶本来就支持这种情况。现在不会出现 CONFLICT，以后实现 reconciled 更正时，要沿用原事件的标签。Codex 主张先 A，与下游约定后再 B；不区分规格之后，B 只是改一个配置值，smooth-research 一侧不用改。用户级 config.json 已在 11-05 改好，原因和生效范围见第 1 节第 1 点。

### D3 daemon 由谁启动（定为 A）

- A：保持 agent 启动。登记或加入通知后，agent 每次都运行返回的命令（Claude Code 运行 listen_command，其他 agent 按 2.1 第 7 点的投递方式运行），不以"已在运行"为理由跳过；配合 2.1 的准确状态。
- B：notifier 和服务端在同一台机器上时，服务端在 notify_create 和带 notify 的 job_submit 里自己运行 ensure，并在返回里注明。
- C：在 agent 所在机器上常驻一个 supervisor。

选 A。11-01 的失败来自错误的"在运行"信号，ensure 又是幂等的，每次都运行代价很小。A 保持一种生命周期模型，登录节点和工作站两种拓扑一样（DESIGN 9.1）。B 更不依赖 agent，但只适用于本机拓扑，DESIGN 9.1 要改，Codex 反对 B。Claude 初审倾向 B，看到 Codex 关于拓扑的论证后改推 A；修好状态之后如果 agent 仍然漏运行，再加 B。C 恢复能力最强，但多一个要维护的常驻进程，和"不留常驻进程"的设计相反。

### D4 轮询间隔的默认值（定为 A）

- A：60 秒。
- B：120 秒。
- C：保持 300 秒。

选 A。现在的中位延迟 192 秒，已经接近本项目作业用时的中位数 4 分钟；间隔改成 60 秒后，估计延迟能降到 1 分钟上下，再短也没用，因为快照本身最长 60 秒。代价是有未完成通知时每分钟一次 `qstat -ll`，DESIGN 测得一次 0.30 秒，加上状态有变化的作业各一次 `qstat -f`。站点对 qstat 频率有没有限制，两份审核都没有查到；Codex 建议先在 60 到 120 秒之间实测延迟和调用量再定。按 A 实施，第一周记录调用次数，有问题就退到 B。

### D5 一个通知由谁接收（定为 A，需要时加 C）

- A：通知归提交作业的 agent。研究者接收自己作业的通知，协调者用 notify_list、job_get 看状态，不开 listener。
- B：协调者接收所有通知，再转告研究者。
- C：双方都要被唤醒时，为同一组作业建两个通知（不同 name，各自的投递方式），不共用一个收件目录。可以和 A 搭配。

选 A，需要时加 C。结果出来后要接着干活的是研究者；listener 收件目录先到先得，两个人一起听，总有一个以退出码 3 结束，底稿记录的 5 次都是这样。B 让协调者在每个作业组上都被打断，研究者还要等转告。这一条主要是 smooth-research 一侧的工作流约定，miyabi-mcp 只需要在文档里写明"一个通知一个接收方"。Codex 列出了同样的选项，没有表态。

### D6 已送达未确认的通知怎么收尾（定为 A）

- A：notify_cancel 也能关闭已送达未确认的通知，状态记为 dismissed，不需要 token，也不算确认；job_wait 列出相关通知和收尾方法。
- B：job_wait 发现通知已送达时，直接在结果里返回 ack token。
- C：只在 job_wait 和 notify_list 里展示信息，不加新的关闭方式。

选 A。积压只是清理成本，不影响运行；A 给了不需要 token 的清理办法，又不冒充"收到了消息"。B 最省事，但违反 `DESIGN.md:624` 的约定：token 只出现在通知消息里，用来证明确认来自收到通知的 agent。Claude 初审提的是 B，Codex 反对，本稿改推 A。C 是 Codex 的方案，最保守，代价是积压还会出现，协调者仍要靠 `notify listen` 取 token，而消息被另一个 listener 取走之后，再 listen 也取不到。

### D7 预算超限时只提醒还是拦截（定为 B）

- A：只报告。summary 和 dry run 给出折算后的已用、预留和占比，到 80% 给 warn，不拦提交。
- B：在 A 的基础上，提交后的预计用量超过上限时 dry run 失败；要继续，由用户在 policy.json 里提高上限。

选 B。项目规则是超出前必须由用户同意，而提交由 agent 自主完成，本项目有 459 次。只做 A 时，"征得同意"这一步要靠 agent 自己记得停下来；B 把它变成机器检查，离上限远的时候没有任何代价。B 的前提是 3.1 的预留口径实现正确，否则会误拦，所以要和那几条测试一起交付。Codex 推荐先 A，用户明确要求机器强制执行时再做 B。

## 7 实施顺序与验收

D2 只改配置，11-05 已经改好，下面的批次不再包含它。

第一批是较小的 P1 修复：第 1 节的 dry run 说明；2.1 的状态拆分、退出报告、提示条件、生命周期日志和部分失败处理，以及 D3 对应的 instructions 和返回值措辞；2.2 的配置接线和 60 秒默认值；2.5 的 unknown 与早期消息 token；4.3 的 MIG 单元数；3.2 的导出修复和测试；文档更正，包括 `DESIGN.md:836`、`README.md:229`、listener 的退出码、轮询配置、非 Claude agent 的接收方式和 D5 的"一个通知一个接收方"。

第二批是 3.1 的预算和 D7 的超限拦截，连同预留口径的测试一起交付。

第三批是 P2 的整理：第 1 节的 exec_host、exec_vnode 原文，2.3 的退出原因，2.4 的 job_wait 提示、D6 的 dismissed 和收件目录清理，2.5 的 notify_error 提示，3.3 的 README 导出格式，4.1 的工作目录检查，4.2 的占位符，4.3 的资源名，4.4 的摘要，5.1 的 fields。

验收用 fakepbs 离线测试，至少覆盖下面几种情形：

- exec_host、exec_vnode 原文写进事件和 record，后来缺这两个字段的观测不覆盖；旧事件导出逐字节不变，沿用现有的导出稳定性测试。
- daemon 空闲退出后再登记通知，服务端报告 idle_exited 并提示；登记恰好发生在 daemon 最后一次 poll 期间，通知也不会丢；有未完成通知的项目 poll 出错时，daemon 不会以空闲退出。
- start_command 和 listen_command 带上 notify_poll_seconds，默认 60 秒；daemon 报告的实际间隔与配置一致。
- 组里有 unknown 成员时 all_ended 不成立，并发一次"状态不明"消息；早期消息里没有 ack 那一行。
- 已送达未确认的通知能用 notify_cancel 关成 dismissed，不需要 token，也不算确认。
- debug-mig 上 `select=1`、`2`、`4` 通过，`3` 失败；含 vnode 的 select 失败；MIG 队列的计划里有切片说明。
- 导出把 stderr 合并进 stdout 时，每行都能单独解析。
- 预算：并发提交不会一起越线；被拒的不算；没运行过的记 0；缺实测的按请求量预留；预计超过上限时 dry run 和 commit 都失败，到 80% 只给 warn。

在线验证可以不花算力：建一个只带 timeout 的通知，在 daemon 空闲退出后观察状态和提示。这一步会在登录节点上启动 notifyd，做之前要征得用户同意。

## 附 核对记录

### 依据

- 账本统计：读 bypass-lora `.miyabi-mcp/` 下的 submissions、jobs、events 和 notifications，两份审核分别统计，数字一致。Codex 的命令和输出见其报告的 Q2。
- MIG 对照：Claude 把 15 个作业的 mig-profile.json 与分批的 `qstat -H -f` 逐个对照，得到编号与规格的对应和 TOKEN 拟合；Codex 查了 3787033、3766228 两个作业（其报告的 Q3）和站点的 MIG、token 说明页。
- 通知时间线：Claude 用每个通知的触发时间和成员作业的 obittime 算延迟，找到 11-01 的两次长延迟；Codex 交叉了 3789152 的事件、通知 history 和 daemon 日志（其报告的 Q5）。
- 导出：Claude 用项目 venv 的 Python 3.11 合成复现了 160 次，并核对了 smooth-research 的 JSON Lines 加载逻辑和引入它的提交；Codex 逐行解析了 1,358 条事件。
- 两份审核都只做了只读查询，没有提交或取消作业，没有启动、停止或 ensure notifyd，也没有运行测试套件。第 7 节列出的测试都还没有写。审核之后按用户要求改了用户级 config.json 的 `mig_hardware`（D2），这是唯一的改动。

### 两份审核的分歧和取舍

| 议题 | Claude | Codex | 本稿 |
| --- | --- | --- | --- |
| 第 1 节优先级 | 同意下调 | 第一批的 P1，证据不支持整节 P0 | 第一批的 P1 |
| MIG 规格来源 | 编号表推断，注明依据 | 以作业内测量为准，编号对应待站点确认 | 采纳用户意见，不区分规格，D1 取消 |
| hardware 标签 | 新作业改用中性标签 | 先保留旧标签并注明，再与下游约定 | D2 定为 "GH200 MIG"，配置已改 |
| daemon 启动 | 初审倾向服务端代启本机 notifier | 客户端负责，服务端不代启 | D3 定为 agent 每次运行 |
| 轮询默认值 | 60 秒 | 先在 60 到 120 秒之间实测 | D4 定为 60 秒，第一周记录调用量 |
| listener 退出码 | 初审提议按原因分码 | 保留 3，加原因说明 | 采用 Codex |
| job_wait 与 ack | 初审提议返回 token | 只展示信息，不暴露 token | D6 定为不需要 token 的 dismissed |
| 预算 | 超限拦截 | 先只报告 | D7 定为超限拦截 |
| Extra data | 合并输出的机制已复现，rl 自 10-30 起接受 JSON Lines | 没有原始字节，不能认定 | 写成最可能的原因，修复和测试都做 |
| 占位符 | 初审提议跳过 `#PBS` 以外的注释并去掉空格 | 注释不能简单跳过 | 注释照查，只去掉空格，加行号 |

只有一方发现、另一方核对后采纳的：11-01 的长延迟时间线、15 个作业的编号对照、1g 与 2g 的 TOKEN 拟合、重定向写法的占位符误伤、Extra data 的复现、未确认通知中 16 个从未被读、加入通知失败的提示（Claude）；poll 部分失败、非 Claude agent 的接收方式、unknown 当成已结束、早期消息的 token、计划摘要不含 notify、summary 的请求量口径、站点的 MIG 与 token 说明、3789152 的时间拆分、研究影响和"值得保留"几处措辞的更正（Codex）。
