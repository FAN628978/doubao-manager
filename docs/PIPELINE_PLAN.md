# 任务流水线重构规划（只规划，不改代码）

> 目标：把现在“待处理 / 正在处理 / 已归档”这套混在一起的判断，拆成一条可解释、可恢复、可测试的流水线。
>
> 本文件只做检查、优化建议和方案规划，不修改任何运行代码。

---

## 0. 结论摘要

现在的三列不是一套状态机，而是**三个前端分组**，底下压着 13 个字符串状态、4 套资源占用（处理名额 / 浏览器 / 账号 / 额度）和 5 个状态字段（`jobs.status`、`video_web_jobs.phase`、`quota_state`、`recall_state`、`delivery_state`）。

最关键的三个问题：

1. **终态不终**：`needs_review` 被算进“正在处理”且继续占用处理名额，而它实际上已经是终态；默认上限 3 个，只要 3 个自动任务异常，新任务就永久卡在 `queued`。见 `src/doubao_manager/web/api.py:102`、`:429`、`:715`，以及 `tests/test_concurrency.py:432` 对“占槽直到任务结束”的断言。
2. **三列口径不一致**：`needs_review` 在前端“正在处理”，在后端 `TERMINAL`，在 H3 那里映射成 `processing`；`ready` 在“新任务队列”但没有任何 worker 会认领；`已归档` 只是 `downloaded` 的别名，而且可以被“重新下载”打回 `generating`。
3. **状态与动作脱节**：很多动作直接写 `jobs.status`，绕过统一的 `emit()`，导致 `phase`、事件、额度、名额四者不同步；批量按钮的“适用数量”也是前端自己估的，和接口实际能不能执行不一致。

---

## 1. 现状盘点

### 1.1 三个视图到底怎么来的

| 界面 | 来源 | 实际规则 | 问题 |
| --- | --- | --- | --- |
| 概览“待处理任务” | `GET /api/stats` 的 `queued` | `COUNT(status='queued')`，见 `src/app.py:555` | 文案说“等待人工准备”，实际也包含自动任务；`awaiting_manual_send` 反而不算 |
| 概览“已归档视频” | `stats.downloaded` | `COUNT(status='downloaded')`，见 `src/app.py:557` | 全历史累计，不区分今天 / 未回传 / 文件丢失 |
| 任务页“新任务队列” | 前端 `jobGroupDefs` | `queued` + `ready`，见 `src/static/app.js:39` | `ready` 是网页任务永远到不了、worker 也不认领的死状态 |
| 任务页“正在处理” | 前端 `jobGroupDefs` | `opening/configuring/uploading/submitting/confirming/generating/downloading/awaiting_manual_send/needs_review` | 机器在跑、等人点发送、已终止等人工复查，三种语义混在一起 |
| 任务页“处理完” | 前端 `jobGroupDefs` | `downloaded` + `failed` | 成功和失败混在一起；失败没有独立出口 |
| 状态胶囊“已归档” | `statusLabels` | `downloaded`，见 `src/static/app.js:3` | 和“账号归档”共用一个词；而且不是不可变终态 |

### 1.2 实际在用的状态字段

| 字段 | 位置 | 现在的含义 | 问题 |
| --- | --- | --- | --- |
| `jobs.status` | `src/app.py:119` | 13 个字符串，业务和阶段混用 | 没有约束、没有 CAS、可被多处直接 UPDATE |
| `video_web_jobs.phase` | `src/doubao_manager/web/schema.py:51` | 复制 `jobs.status` | 双写不同步；`app.py:1079` 等路径只改 status 不改 phase |
| `quota_state` | `reserved/consumed/released/unknown` | 额度生命周期 | 与状态耦合：`unknown` 既是额度状态，也是“禁止重发”的隐藏语义 |
| `recall_state` | `recalling/...` | 召回租约 | 与 `status=generating` 混在一起 |
| `delivery_state` | `not_requested/pending/delivered/failed` | H3 回传 | 不参与任务分组，用户看不到“已归档但没回传” |
| `submit_mode` | `auto/manual` | 执行方式 | 被当成状态使用，导致 `queued` 一词多义 |

---

## 2. 检查发现

### P0：会导致队列停摆或状态错误

#### P0-1 `needs_review` 永久占用处理名额，队列会被 3 个异常任务堵死

- `processing_statuses = ACTIVE | {MANUAL_WAIT, "needs_review"}`，见 `src/doubao_manager/web/api.py:102`。
- `emit()` 只在 `downloaded` / `failed` 时 `schedule_processing_release()`，见 `:429`。
- `browser_slot()` 启动时会把数据库里所有匹配状态重新加回名额集合，见 `:208-221`；所以重启也不会自动释放。
- 默认上限是 3（`schema.py:9`）。3 个自动任务进入 `needs_review` 后，第 4 个任务的 worker 会永远等 `len(processing_job_ids) < 3`。
- 自动任务还不允许 `mark-failed`（`:1239-1240` 只允许 manual），所以操作者只能通过“重新自动执行 / 重新生成 / 删除”来解套。
- 现有测试 `test_processing_slot_is_held_until_job_is_finished`（`tests/test_concurrency.py:432`）明确把“占槽到任务结束”当成预期，说明这是设计选择，但它和“异常自动转人工”的产品目标冲突。

**影响**：异常一多，整个自动流水线停摆，但界面只显示“排队中”，没有等待原因。

#### P0-2 从 `needs_review` 回传成功恢复成 `downloaded` 时不释放名额

- `push_result()` 的恢复分支直接 `UPDATE jobs SET status='downloaded'`，见 `src/doubao_manager/web/api.py:2158-2168`，随后没有调用 `release_processing_job`。
- `sync_processing_job_ids()` 只做 `update`，从不清除过期 id，见 `:183-199`。于是内存里的 `active_processing` 会永久偏高，达到上限后新任务同样卡死。
- 外部表现：设置页里 `active_processing` 对不上实际任务；重启才能恢复。

#### P0-3 “已归档”不是终态，点一次下载就会跳回“正在处理”

- `begin_recall()` 允许 `downloaded` 进入，见 `src/doubao_manager/web/api.py:2010`；前端“下载视频”固定带 `force_redownload=true`（`src/static/app.js:124`）。
- `claim_recall()` 无条件把 `jobs.status` 改成 `generating`，见 `:1814`。
- 下载失败时又 `emit(jobid, "downloaded", ..., code=...)`，见 `:1959`，出现“成功状态 + 非空 error_code”的矛盾组合。
- 结果：一条已归档任务在“处理完 → 正在处理 → 处理完”之间来回跳；H3 轮询会看到已归档任务短暂变成 `generating`。

#### P0-4 同一套“完成/失败”有至少四份定义

| 位置 | 定义 |
| --- | --- |
| `src/doubao_manager/web/config.py:60` | `TERMINAL = downloaded / needs_review / failed` |
| `src/doubao_manager/web/api.py:102` | `needs_review` 属于“处理中” |
| `src/static/app.js:39` | `needs_review` 在“正在处理”，`finished` 只有 `downloaded/failed` |
| `src/app.py:239` | `VALID_STATUSES` 还包含 `ready` |

同一个任务，后端说它是终态，前端说它正在处理，H3 说它是 `processing`。任何一处加状态，另外三处都会漏。

### P1：结构性问题

#### P1-1 `queued` 一词三义，用户看不出在等什么

`queued` 实际包含：

- 自动任务已建单、等处理名额；
- 人工任务已建单、等人点“提交素材”；
- `auto_start=false` 的占位任务；
- 因为账号锁 / 额度 / 浏览器被占而等待的任务。

界面统一显示“排队中”，没有 `wait_reason`、`queue_position`、`blocked_reason`。这正是“待处理/正在处理逻辑不好”的直接体感来源。

#### P1-2 “新任务队列”不是队列

- 列表按 `created_at DESC` 返回（`src/app.py:818`），最新在前；
- 启动恢复按 `created_at ASC`（`src/doubao_manager/web/api.py:2350`），最早先跑；
- 名额等待用 `asyncio.Condition.notify_all()`（`:201-206`），所有等待者一起醒，谁先抢到不确定，没有 FIFO；
- 每个排队任务在创建时就 `create_task(worker)`（`:1060`），worker 先拿账号锁再等名额（`:707-710`），所以排队任务会先占住账号，形成队头阻塞；
- 没有队列序号、没有优先级、没有 ETA。

#### P1-3 `ready` 是死状态

- 前端把它放进“新任务队列”，`statusLabels` 也有“待人工提交”。
- 网页任务 worker 只认领 `status='queued'`（`:712-716`），`start_worker` 也只接受 `queued`（`:1058`）。
- 启动恢复会把网页任务的 `ready` 改成 `needs_review`（`quota.py:296-316`）。
- 只有旧的普通任务 `prepare` 会写 `ready`（`src/app.py:1029`），而网页任务走这个接口会被 409 拒绝。

结论：网页任务一旦出现 `ready`，既不会被调度，也没有对应的正常出口。

#### P1-4 状态写入没有统一入口

统一入口是 `emit()`，但下面这些路径直接改状态：

- `send_confirmation()`：`:1757-1765` 直接改 `jobs.status` 和 `phase`；
- `claim_recall()`：`:1814` 直接改 `generating`；
- `push_result()`：`:2160` 直接改 `downloaded`；
- 旧任务上传：`src/app.py:1079` 直接改 `downloaded`；
- 批量删除：`src/app.py:940-947` 直接删表后再补释放。

后果：事件、`phase`、额度结算、名额释放四件事经常只做了一部分。P0-2 就是其中一个漏点。

#### P1-5 没有 CAS / 合法迁移矩阵，并发操作可以互相覆盖

`emit()` 是无条件 `UPDATE jobs SET status=?`（`:387`）。`recall`、`send-confirmation`、`push-result`、删除、恢复脚本都可能同时改同一行。`reset_generation()` 用了 `WHERE status=?` 做 CAS（`:1151-1156`），但只覆盖了重置这一条路径。没有“从哪个状态只能到哪个状态”的矩阵。

#### P1-6 `needs_review` 混合了“可安全重试”和“可能已花钱、禁止重发”

`settle_quota()` 用 `definitely_unspent` 错误码集合和 `prior_status` 来决定 `unknown` 还是 `released`（`quota.py:109-130`）。但界面只有一个“需人工处理”标签：

- `LOGIN_REQUIRED`：明确没花钱，可以重发；
- `SUBMISSION_UNCONFIRMED` / `WAIT_TIMEOUT`：可能已花钱，禁止自动重发。

操作者只能凭经验判断，批量操作时风险很高。

### P2：口径和体验问题

#### P2-1 概览数字、标签页数字、可操作数量三套口径

- “待处理任务”只算 `queued`，不含 `ready`，也不含等人处理的 `awaiting_manual_send`；
- “正在处理”标签包含终态 `needs_review`；
- “已归档视频”是全历史累计，包含已删除账号留下的历史任务；
- 前端每次 `refreshAll()` 拉全量 `/api/jobs` 再自己分组（`src/static/app.js:17`），12 秒一次（`:147`），任务多了以后会越来越慢。

#### P2-2 批量按钮的“适用数量”和接口实际行为不一致

`bulkApplies()`（`src/static/app.js:35`）：

- `auto`：只要是网页任务就返回 true，包括 `downloaded`（后端会 409）和正在执行的任务；
- `send` / `confirm` / `download`：只要网页任务就返回 true，但后端 `send` 要求有活动页面、`send-confirmation` 只允许 `awaiting_manual_send/confirming/needs_review`（`:1652`）、`recall` 只允许部分状态（`:2010`）。

结果：按钮上显示 N 个，点下去一部分必然失败；失败提示还是“首个失败”，用户无法预判。

#### P2-3 “归档”一词被三处复用

- 视频完成：`downloaded` → 界面“已归档”；
- 账号停用：`accounts.archived` → “归档账号”（`src/app.py:803`）；
- 本地回收区：`data/trash`。

建议把视频侧改成“已完成 / 已产出”，账号侧改成“停用 / 隐藏”，从词根上分开。

#### P2-4 文档与代码已经漂移

- `docs/API.md:88` 说“推送失败时任务转为 `needs_review`”，但 `finish_downloaded()`（`:606-662`）现在只记录 `delivery_state=failed`，不改状态；
- `README.md:28` 说“任务完成或判定失败后才释放队列名额”，但 `needs_review` 也一直占着；
- `docs/API.md:161` 说重新下载失败会保留 `downloaded`，但中间会先把状态改成 `generating`（P0-3）。

#### P2-5 进度条是前端猜的

`jobStep()`（`src/static/app.js:30`）把 `needs_review` / `failed` 一律算成第 3 步，哪怕它是在第 2 步“准备页面”就失败了；`ready` 落到默认值 1。进度和真实阶段不一致。

---

## 3. 优化原则

1. **一个事实只存一个字段**：执行阶段、处置结果、文件归档、H3 回传、资源占用，分开存。
2. **状态只通过事件迁移**：所有写状态的地方走同一个 `transition()`，带期望状态和合法迁移矩阵，禁止直接 UPDATE。
3. **终态不可变**：已归档 / 已失败是终态；重新下载、重新回传是“产出物操作”，不改变处置状态；重新生成是显式 `reopen` 事件，`attempt+1`。
4. **资源与状态解耦**：机器执行才占执行名额；等人不占机器名额；额度风险单独计。名额是租约，有 TTL 和心跳，进程崩了能被回收。
5. **界面不自己算规则**：后端返回 `bucket / wait_reason / next_action / can`，前端只渲染。批量数量也由后端给。
6. **对外契约不变**：H3 继续看到原来的 `status`，由兼容投影生成；内部先迁移，外部后迁移。

---

## 4. 目标流水线设计

### 4.1 四个正交维度

| 维度 | 取值 | 作用 |
| --- | --- | --- |
| `disposition` 处置 | `pending` / `running` / `waiting_human` / `archived` / `failed` / `canceled` | 决定界面列、统计口径、是否终态 |
| `execution` 执行阶段 | `queued` / `claimed` / `preparing` / `submitted` / `waiting_generation` / `downloading` / `finished` / `none` | 决定进度条和调试信息 |
| `artifact` 产出物 | `none` / `downloaded` / `delivered` / `delivery_failed` / `delivery_not_requested` | 决定能不能下载、要不要回传 |
| `hold` 占用 | `queue_slot` / `account_lane` / `browser_lane` / `quota_state` / `lease` | 决定调度和额度，不参与界面分组 |

**不变量**

- `archived`、`failed`、`canceled` 三者互斥，且 `archived` 必须有本地文件；
- `archived` 不会被自动改回 `running`；重新下载只写 `artifact` 和 `archive_version`；
- 一个 `running` 任务恰好持有一个有效租约；一个租约只对应一个 `running` 任务；
- `waiting_human` 不持有 `queue_slot`，可以持有 `quota_state=unknown`；
- 任何状态迁移都写一条事件，且满足迁移矩阵。

### 4.2 处置桶（界面列）

建议从 3 列改成 4 列 + 失败筛选，而不是继续硬塞：

| 列 | 包含 | 用户要做什么 |
| --- | --- | --- |
| **待处理** | 已建单、还没开始执行；等名额、等账号、等额度、等操作者点“开始” | 通常不用管，等调度；显示排队位置和等待原因 |
| **正在处理** | 机器正在准备 / 提交 / 等生成 / 下载 | 不用管；显示当前阶段和耗时 |
| **待人工** | 人工发送、待确认、需复查、页面丢失、回传失败 | 这是唯一需要人动手的列，按风险和停留时长排序 |
| **已归档** | 本地文件已校验；回传状态用徽标显示（已回传/回传失败/未要求） | 可下载、可重推；重新下载不改列 |
| 失败（筛选/第五列） | 明确失败、没有产出物 | 可重新生成；不能伪装成功 |

如果产品上必须保留三列，则定义必须是：

- **待处理** = 尚未开始执行（`pending`）；
- **正在处理** = 正在占用机器执行（`running`）；
- **已归档** = 已有校验通过的本地文件（`artifact=downloaded/delivered`）；
- “待人工”和“失败”不能藏进这三列，必须用独立入口（顶部警示条 / 失败筛选）露出，否则会重现“needs_review 混在正在处理”的问题。

### 4.3 状态机与事件

事件而不是“直接改状态”：

```
create → dispatch → prepare_ok → submit_ok → result_ready → archive_ok → deliver_ok
                         ↘ blocked_pre_spend → waiting_human
                         ↘ submit_uncertain  → waiting_human(risk=uncertain)
                         ↘ generation_failed → failed
                         ↘ lease_expired     → waiting_human(risk=uncertain)
archived → redownload_requested → artifact_updated（disposition 不变）
archived → redeliver_requested → delivered / delivery_failed（disposition 不变）
waiting_human → resume → pending
failed/archived → reopen → pending（attempt+1，需显式确认）
```

**迁移矩阵（节选）**

| 当前处置 | 事件 | 目标处置 | 名额 | 额度 |
| --- | --- | --- | --- | --- |
| — | create | pending | 无 | reserved |
| pending | dispatch | running | 获得 queue_slot + account_lane + browser_lane | reserved |
| running | prepare_ok | running | 保持 queue_slot，释放 browser_lane | reserved |
| running | manual_ready | waiting_human | 释放 queue_slot | reserved |
| running | blocked_pre_spend | waiting_human | 释放 queue_slot | released |
| running | submit_uncertain | waiting_human | 释放 queue_slot | unknown |
| running | archive_ok | archived | 释放全部 | consumed |
| running | generation_failed | failed | 释放全部 | released |
| waiting_human | resume | pending | 无 | 重新校验后 reserved |
| archived | redownload | archived | 只占 browser_lane（短租约） | 不变 |
| archived | reopen | pending | 无 | 新的 attempt 重新占用 |

### 4.4 资源泳道

| 泳道 | 上限 | 谁持有 | 释放时机 |
| --- | --- | --- | --- |
| `queue_slot` | `max_running_jobs`（现 `processing_limit`） | 只有 `running` 任务 | archived / failed / waiting_human / canceled / 租约过期 |
| `account_lane` | 每账号 1 | running 或人工操作的短租约 | 同上 / 操作结束 |
| `browser_lane` | 全局 1 | 真正在操作浏览器的任务 | 进入等生成 / 下载完成 / 操作结束；租约 TTL |
| `quota` | 每账号每日秒数 | 任务 | reserved → consumed / released / unknown |
| `attention` | 不设硬上限，可设告警阈值 | waiting_human | 人处理完 / 取消 |

关键改动：**`needs_review` / `awaiting_manual_send` 不再占 `queue_slot`**。如果担心“未知额度任务过多”，单独加提交门禁（例如 `max_unknown_quota_jobs`），不要拿执行名额当风控。

### 4.5 阶段表

| 阶段 | 进入条件 | 退出 | 持有资源 | 建议超时 | 失败去向 |
| --- | --- | --- | --- | --- | --- |
| intake | 参数、素材、账号、额度校验通过 | 落库 pending | quota reserved | — | 422/409，不落库 |
| queued | pending 且 eligible | dispatcher 认领 | quota reserved | 无（可告警） | 取消 |
| preparing | 拿到三把锁 | 页面填好并提交 | queue+account+browser | 5 分钟 | waiting_human(pre_spend) |
| submitted | 发送成功，等待豆包 | 出现生成中/结果卡片 | queue+account | 15 分钟 | waiting_human(uncertain) |
| waiting_generation | 已确认生成开始 | 检测到结果 | queue（+quota unknown 可选） | 15–30 分钟 | waiting_human(uncertain) |
| downloading | 发现结果卡片 | 文件校验通过 | queue+account+browser | 10 分钟 | waiting_human(保留旧归档) |
| archived | 文件存在且 header/时长校验通过 | 终态 | 无 | — | 回传失败只改 artifact |
| delivery | 归档后且有回传配置 | H3 2xx | outbox 租约 | 600 秒 | delivery_failed，可重试 |
| failed | 明确失败或人工判定 | 终态 | 无 | — | 可 reopen |
| waiting_human | 需要人判断 | 人操作 / 取消 | 无 queue slot，可能有 quota hold | S L A 告警 | resume / fail |

### 4.6 调度器

把“每个任务一个 `create_task` + 自己等锁”改成“一个 dispatcher + 带租约的 worker”：

1. `pending` 行按 `queue_seq`（自增）排序，FIFO；可加优先级和老化（等待超过 N 分钟提权）。
2. dispatcher 先算出“三把锁都可用”的候选，再在事务里认领（`BEGIN IMMEDIATE` + `execution=claimed` + 写租约），避免 worker 拿着账号锁等名额。
3. 没有可用资源时，任务留在 `pending`，写 `wait_reason` 和 `queue_position`，界面直接展示。
4. worker 定期心跳续租；watchdog 每 15 秒扫描过期租约，转 `waiting_human(INTERRUPTED)`，释放资源，绝不自动重发。
5. 名额释放靠 `finally` + 租约过期双保险；启动时按数据库对账，不依赖内存计数。
6. 人工操作走单独的短租约：只占 `account_lane` + `browser_lane`，不占 `queue_slot`，因此人工积压不会阻塞自动队列。

### 4.7 交付 outbox

- 表：`job_deliveries(job_id, state, attempts, next_attempt_at, last_error, lease_until)`；
- 状态：`not_requested / pending / in_flight / delivered / failed`；
- 归档成功只负责入队；推送成功/失败只改 artifact 和 delivery，不改 disposition；
- 指数退避 + 手工重推；界面在“已归档”行显示徽标；
- 明确文档：`needs_review` 不再因为推送失败而产生。

### 4.8 重启对账

启动时只做一次，按“产出物优先”分类：

1. 有有效归档文件 → `archived`，补 delivery；
2. 停在 `preparing` 之前的阶段 → `pending`，`safe_to_retry=true`；
3. 停在 `submitted` 之后且没有归档 → `waiting_human(INTERRUPTED)`，`quota_state=unknown`，`safe_to_retry=false`；
4. 清空所有租约，重建计数；
5. 不从数据库“恢复” worker，只有明确 safe 的任务才重新入队。

---

## 5. 数据与 API 规划

### 5.1 表结构

`jobs` 新增：

| 字段 | 说明 |
| --- | --- |
| `disposition` | pending/running/waiting_human/archived/failed/canceled |
| `execution` | queued/claimed/preparing/submitted/waiting_generation/downloading/finished/none |
| `wait_reason` | `slot_full/account_busy/quota/paused/operator_start/browser_busy` |
| `wait_since` | 开始等待的时间 |
| `queue_seq` | 自增队列序号 |
| `attempt` | 第几次生成尝试 |
| `attention_reason` | `MANUAL_SEND/CONFIRM/REVIEW/PAGE_LOST/DELIVERY_FAILED/...` |
| `risk` | `safe_to_retry` / `uncertain` |
| `archived_at` | 产出物校验通过时间 |
| `archive_version` | 重新下载的版本号 |

新增：

- `job_events(id, job_id, event, from_disposition, to_disposition, actor, message, created_at)`；
- `job_leases(job_id PK, lease_id, lane, acquired_at, heartbeat_at, expires_at)`；
- `job_deliveries(...)`；
- `jobs.status` 保留为**兼容投影**，由 `transition()` 统一维护；或者建一个视图 `job_status_compat` 给旧接口。

### 5.2 兼容状态投影

| 新模型 | 旧 `status` |
| --- | --- |
| pending（未开始） | `queued` |
| running + preparing | `opening/configuring/uploading` |
| running + submitted | `submitting/confirming` |
| running + waiting_generation | `generating` |
| running + downloading | `downloading` |
| waiting_human + MANUAL_SEND | `awaiting_manual_send` |
| waiting_human + REVIEW | `needs_review` |
| archived | `downloaded` |
| failed | `failed` |

H3 的映射保持不变：`needs_review → processing`，`MANUAL_CONFIRMED_FAILED` 仍按失败处理；新增字段作为可选返回，老客户端不受影响。

### 5.3 接口

- `GET /api/jobs?bucket=pending|running|waiting_human|archived|failed&cursor=...`：返回 `bucket`、`wait_reason`、`queue_position`、`risk`、`next_action`、`can` 映射；
- `GET /api/jobs/buckets`：一次返回各桶数量，供标签页和概览使用；
- `GET /api/stats`：与桶口径对齐，拆成 `pending/running/waiting_human/archived/failed`；
- 所有动作接口统一返回迁移后的任务快照；不满足条件时返回 `409 + code + retryable + safe_to_retry`；
- 批量动作返回逐项结果，前端不再自己估算适用数量。

---

## 6. UI 规划

1. 列：**待处理 / 正在处理 / 待人工 / 已归档（+ 失败筛选）**；数量全部来自 `/api/jobs/buckets`。
2. 行内信息分三层：
   - 第一层：任务名、账号、模式、时长；
   - 第二层：执行阶段 + 等待原因（“等名额，前面 2 个” / “等人点发送” / “等确认语”）；
   - 第三层：主操作（`next_action`）+ 更多菜单。
3. 风险徽标：`可安全重试` / `可能已消耗额度，禁止重发`，直接来自 `risk`。
4. 已归档行：显示“本地已归档 · 已回传/回传失败/未要求”三个徽标；重新下载显示为临时操作徽标，不移动列。
5. 批量：勾选后只显示当前可执行的动作，数量由后端 `can` 计算；执行结果逐项展示。
6. 刷新：先改成 `GET /api/jobs/buckets` 轻量轮询 + 当前列的增量拉取；后续再考虑 SSE。
7. 进度条：由 `execution` 驱动，失败保留失败时的阶段，不再一律显示第 3 步。

---

## 7. 迁移路线（建议分四步）

### Phase 0：先止血（0.5–1 天，不改数据模型）

1. `needs_review` 不再计入 `processing_statuses`；或者进入 `needs_review` 时立即释放名额（二选一，推荐后者，语义更清楚）。
2. `push_result` 从 `needs_review` 恢复为 `downloaded` 时补 `release_processing_job`。
3. `sync_processing_job_ids()` 改成以数据库为准重建集合，而不是只增不减。
4. `force_redownload` 不再把已归档任务改成 `generating`；下载过程只写 `recall_state` / 临时 `artifact_op`。
5. 前端把 `needs_review` 移到“待人工”；概览数字改用同一口径；批量按钮按真实可执行条件过滤。
6. 修 `docs/API.md:88`、`README.md:28` 的过期描述。

### Phase 1：模型落地（3–5 天）

1. 加 `disposition/execution/wait_reason/queue_seq/attempt/attention_reason/risk/archived_at/archive_version`。
2. 实现 `transition(job_id, event, actor, expected_disposition)`：CAS + 迁移矩阵 + 事件 + 兼容 `status` 投影 + 资源动作。
3. 把所有直接 `UPDATE jobs SET status` 的路径改成走 `transition()`。
4. 写回填脚本：把现有 13 个状态映射到新维度（见 5.2）。
5. 加不变量测试：终态不可逆、租约守恒、名额不泄漏、事件与状态一致。

### Phase 2：调度器（约 1 周）

1. 用 dispatcher 替代“每任务一个 worker 自己等锁”。
2. 加 `queue_seq`、FIFO、等待原因、排队位置。
3. 加租约、心跳、watchdog、启动对账。
4. 人工操作走独立短租约，不占执行名额。
5. 压测：100 个排队任务、3 个异常、上限 3，验证不堵死且顺序稳定。

### Phase 3：产出物与交付（约 1 周）

1. artifact / delivery 拆分，重新下载不改 disposition。
2. outbox 重试与界面徽标。
3. UI 四列 + 风险徽标 + 批量结果。
4. 文档、Swagger、迁移说明、验收脚本。

---

## 8. 验收清单（不变量测试）

1. `needs_review` 不阻塞：3 个异常 + 1 个新任务，上限 3，新任务必须能执行。
2. `awaiting_manual_send` 不阻塞自动队列。
3. 已归档不可变：连续 3 次 force redownload，`disposition` 始终 archived，H3 轮询不出现 `generating`。
4. 名额守恒：随机迁移 1000 次后，`running` 数 == 有效租约数 == `active_processing`。
5. 崩溃恢复：在 preparing / submitted / waiting_generation / downloading 四个点杀进程，重启后分别得到 pending / waiting_human(uncertain) / waiting_human(uncertain) / archived（有文件时）。
6. 顺序：一次入队 10 个，调度顺序等于 `queue_seq`；等待原因和位置正确。
7. 回传失败：任务仍在 archived，只多一个 `delivery_failed` 徽标；重推成功变 delivered。
8. 批量：按钮数量 == 接口实际接受数量；不允许的动作不出现在批量栏。
9. 兼容：H3 用旧字段轮询，状态序列与旧版一致（除已修复的来回跳）。
10. 文档：README、API.md、Swagger 示例与新状态表一致。

---

## 9. 风险与取舍

| 风险 | 应对 |
| --- | --- |
| 一次改太多，回归面大 | Phase 0 先修 4 个 P0；Phase 1 保留 `status` 兼容投影，新旧并行一段时间 |
| `needs_review` 释放名额后，未知额度任务变多 | 单独加提交门禁 `max_unknown_quota_jobs`，而不是占执行名额 |
| 待人工列越积越多 | 按风险 + 停留时长排序，超过 SLA 高亮；不设硬上限，避免再次堵死 |
| 多一套字段，前端要改 | 后端一次返回 `bucket/next_action/can`，前端只删逻辑不加逻辑 |
| H3 契约变化 | 旧 `status` 由投影生成；新增字段可选；先内部迁移再考虑对外 |
| 人工操作也需要浏览器，仍可能和自动任务抢 | 统一 `browser_lane` 短租约，人工优先或按配置抢占；不再用“处理名额”间接控制 |

---

## 附录 A：现状状态 → 目标映射

| 现状态 | disposition | execution | attention | risk | artifact |
| --- | --- | --- | --- | --- | --- |
| `queued`（自动） | pending | queued | — | — | none |
| `queued`（人工） | pending | queued | operator_start | — | none |
| `ready` | pending | queued | operator_start | — | none（建议删除该状态） |
| `opening/configuring/uploading` | running | preparing | — | — | none |
| `submitting/confirming` | running | submitted | — | — | none |
| `generating` | running | waiting_generation | — | — | none |
| `downloading` | running | downloading | — | — | none |
| `awaiting_manual_send` | waiting_human | none | MANUAL_SEND | safe | none |
| `needs_review`（未花钱） | waiting_human | none | REVIEW | safe | none |
| `needs_review`（不确定） | waiting_human | none | REVIEW | uncertain | none |
| `downloaded` | archived | finished | — | — | downloaded/delivered |
| `downloaded` + 回传失败 | archived | finished | — | — | delivery_failed |
| `failed` | failed | none | — | — | none |

## 附录 B：关键代码位置

- 名额与终态：`src/doubao_manager/web/api.py:96-108`、`:183-227`、`:429-431`
- worker 认领：`src/doubao_manager/web/api.py:699-733`
- 人工等待与复查：`src/doubao_manager/web/api.py:771-802`
- 召回：`src/doubao_manager/web/api.py:1800-1823`、`:2003-2012`
- 回传恢复：`src/doubao_manager/web/api.py:2158-2168`
- 状态定义：`src/doubao_manager/web/config.py:8-11`、`:60`
- 额度结算：`src/doubao_manager/web/quota.py:76-134`
- 重启恢复：`src/doubao_manager/web/quota.py:165-316`
- 列分组与批量：`src/static/app.js:3`、`:30-32`、`:35`、`:39-40`
- 概览数字：`src/static/index.html:29-30`、`src/app.py:549-565`
- 旧任务写状态：`src/app.py:1029`、`:1079`
