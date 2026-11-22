# 豆包视频生成 — API 调用开发说明

本服务是豆包网页版视频生成的管理台，对外提供**拉取式**接口：调用方提交任务 → 轮询状态 → 下载视频。默认只监听本机 `127.0.0.1:8765`。

- Swagger 文档：`http://127.0.0.1:8765/docs`
- 完整项目说明：见 `README.md`

账号创建接口 `POST /api/accounts` 默认使用 `follow_doubao_name=true`。账号浏览器打开并登录后，服务每 5 秒读取豆包头像旁的用户名并保存为账号名称；未登录或读取失败时保留原名。旧账号升级后也默认启用，登录前仍使用原名称。

`GET /api/accounts` 返回 `follow_doubao_name` 字段。若需手动命名，可通过 `PATCH /api/accounts/{id}` 提交 `{"priority":0,"follow_doubao_name":false,"name":"自定义名称"}`；优先级请传入账号当前值。旧客户端不传新增字段时，保留原有跟随设置。

---

## 一、接口总览（只需三个）

| 步骤 | 方法 | 路径 | 作用 |
| --- | --- | --- | --- |
| ① 提交 | `POST` | `/api/v1/videos` | 创建任务并执行（图片直接作为文件字段上传） |
| ② 轮询 | `GET` | `/api/v1/videos/{job_id}` | 查询任务状态 |
| ③ 下载 | `GET` | `/api/v1/videos/{job_id}/video` | 拉回生成的 MP4 视频 |

**调用方式（拉取式）：**

```
提交(返回 job_id)  →  轮询状态直到 downloaded  →  下载视频
```

---

## 二、认证

默认无需认证。只有当服务端设置了环境变量 `DOUBAO_LOCAL_API_KEY` 时，以上三个接口才要求请求头：

```
X-API-Key: <你的密钥>
```

可并发提交多个任务。不同账号和同一账号的任务都可以并行执行；同一账号的每个任务使用独立标签页。需要限制浏览器资源占用时，可将 `DOUBAO_MAX_CONCURRENT_ACCOUNTS` 设置为正整数。

确认语支持预设管理：`GET/POST /api/v1/videos/confirmation-presets`、`PUT/DELETE /api/v1/videos/confirmation-presets/{preset_id}`。自动执行遇到豆包确认步骤时读取 `preset_default` 的当前内容，不使用代码中的固定文本。人工向指定任务发送时调用：

```http
POST /api/v1/videos/generations/{job_id}/send-confirmation?confirm_send=true
Content-Type: application/json

{"preset_id":"preset_default"}
```

该入口不限制任务当前状态；只要任务已有豆包会话页即可发送。重复发送可能再次触发生成或消耗额度，调用方必须让操作者明确确认。

对 `needs_review` 或 `failed` 的未完成任务，可明确确认后重新生成：

```http
POST /api/v1/videos/generations/{job_id}/retry?confirm_retry=true
```

按服务器最初提交的参数复制为新任务（保留原回推地址和令牌；自动任务立即执行，人工任务保持待执行）：

```http
POST /api/v1/videos/generations/{job_id}/copy?confirm_copy=true
```

两个入口都会复制原任务的提示词、模式和素材，并重新选择账号：只从可用的空闲账号中选择当日剩余额度最大的账号，额度相同时按档案创建顺序选择，不判断本地额度是否足够。`retry` 会标记重试关系并自动开始，若已有未失败的重试任务则拒绝重复提交；`copy` 中的自动任务会立即执行，人工任务仍保持待执行，由操作者启动人工流程。

对未完成原任务就地重新执行时，可调用：

```http
POST /api/v1/videos/generations/{job_id}/restart-auto
```

该操作保留原任务 ID，先结算原账号的额度占用，再清空原账号并进入自动分配流程，获取处理名额后选择当日剩余额度最大的可用账号并自动执行，不判断本地额度是否足够。自动执行方式会保存，排队重派或服务重启后仍然生效；暂无可用账号时保持排队并自动重试。

网页面板手动点击“下一个任务”调用 `POST /api/v1/videos/generations/{job_id}/dispatch-next`：取出最早排队的其他任务，包括已自动放行但正在等待并发名额的任务，固定改为自动执行且不受全局并发数量限制；仍保留任务原有账号选择和同一账号串行规则，不判断本地额度是否足够。自动执行设置及并发豁免会保存，排队重派或服务重启后仍生效；显式重置任务时清除并发豁免，已手动放行的排队任务不会被重复选取。

---

## 三、① 提交任务

```
POST /api/v1/videos
Content-Type: multipart/form-data
```

### 3.1 普通字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `account_id` | string | 可选 | 进入处理时分配 | 账号 ID，如 `acc_9b661e5a38`；留空则任务先进入“待处理”且不绑定账号，真正拿到处理名额、进入处理前才自动选一个**当日剩余额度最大**的可用账号，不判断本地额度是否足够（每天每账号 30 秒，可经环境变量 `DOUBAO_DAILY_VIDEO_SECONDS` 覆盖） |
| `prompt` | string | ✅ | — | 提示词，1～10000 字；`@图N` 会自动翻译成「参考图N」 |
| `mode` | string | 可选 | `text2video` | 生成模式，见下方枚举 |
| `ratio` | string | 可选 | `16:9` | 画幅，以豆包实际选项为准（常见 `16:9` / `9:16` / `1:1`） |
| `duration` | int | 可选 | `4` | 秒数，以豆包实际选项为准（常见 `4` / `10` / `15`） |
| `model` | string | 可选 | 忽略 | 兼容字段，不参与最终模型选择；最终按本机“任务设置”中的视频模型执行 |
| `project_name` | string | 可选 | 空 | 项目名称，≤120 字 |
| `episode_name` | string | 可选 | 空 | 集数名称，≤120 字 |
| `shot_name` | string | 可选 | 空 | 镜头名称，≤120 字 |
| `task_name` | string | 可选 | 空 | 调用方显示名称，≤160 字；H3 使用统一格式 `项目 / 集数 / 镜头`，不参与生成幂等指纹 |
| `idempotency_key` | string | 可选 | 空 | 幂等键，1～128 字；重复调用不会重复建单 |
| `auto_start` | bool | 可选 | 本地“自动调度”设置 | `true` 建单后直接进入调度；`false` 只进“待处理”，之后用 `/generations/{id}/dispatch` 放行（或 `/start`）。为兼容旧调用，显式同时传 `submit_mode=manual` 时仍只排队 |
| `submit_mode` | string | 可选 | 本地“自动执行”设置（默认关闭） | `auto`：进入调度后脚本自动点击发送并完成后续流程；`manual`：进入调度后只准备页面与素材，等待人工发送。`/dispatch` 只放行，不改变该值。H3 不传此字段，由本地管理台“任务设置”决定 |

自动任务下载归档后，如提交时同时提供 `result_push_url` 和 `result_push_token`，会自动把视频推送回服务器；推送失败只记录 `delivery_state=failed` 和下次建议重试时间，任务仍保持 `downloaded` 并保留本地归档视频，不会退回 `needs_review`。人工任务仍保留分步处理和手动推送流程。

### 3.2 mode 枚举

| mode | 含义 | 需要的文件字段 |
| --- | --- | --- |
| `text2video` | 文生视频 | 无 |
| `image2video` | 首帧图生视频 | `first_frame`（1 个） |
| `first_last_frame` | 首尾帧生视频 | `first_frame` + `last_frame`（各 1 个） |
| `reference_images` | 参考生视频 | `reference`（1～9 个文件） |

### 3.3 图片约束（所有文件字段通用）

- 格式：`PNG` / `JPEG` / `WEBP`
- 大小：≤ 15 MB
- 尺寸：128～8192 像素（宽和高都满足）

### 3.4 本机模型设置

| model 值 | 说明 |
| --- | --- |
| `Seedance 2.0 Fast` | 快速出片 |

> 最终模型只读取本机“任务设置”，所有账号统一使用视频模型。请求中的 `model` 参数始终忽略；系统在处理任务时写入并选择本机模型。目前仅支持 `Seedance 2.0 Fast`；旧设置中的其他模型自动回退为此模型。`GET/PUT /api/settings/video-models` 使用统一的 `model` 字段，例如 `{"model":"Seedance 2.0 Fast"}`。账号接口不再使用或返回 `is_member` 字段，旧客户端传入该字段时忽略。

### 3.5 响应（HTTP 202）

```json
{
  "job_id": "job_xxxxx",
  "status": "queued",
  "submit_mode": "manual",
  "status_url": "/api/v1/videos/job_xxxxx",
  "video_url": "/api/v1/videos/job_xxxxx/video"
}
```

### 3.6 人工发送（半自动，推荐给 H3 一类的外部调度）

本地管理台选择“人工处理”后，“提交任务”和“按下发送键”会拆开。H3 只提交任务内容，不传 `submit_mode` 或 `auto_start`。整个流程是：

```
H3/外部系统 POST 建单（模式由本地设置）        → status=queued（只排队，不打开浏览器）
操作者在本机管理台点“1 准备豆包网页”          → POST /generations/{id}/start
脚本配置模式/比例/时长、上传图片、填入提示词   → status=awaiting_manual_send
操作者点“2 确认发送”                          → POST /generations/{id}/send?confirm_send=true
系统点击已准备页面的发送按钮                   → status=confirming
操作者点“3 发送确认语”                        → POST /generations/{id}/send-confirmation?confirm_send=true
确认语发送成功                                 → status=generating
出片后操作者点“4 下载视频”                    → POST /generations/{id}/recall
系统复用准备时的标签页，按提交前卡片序号取片归档 → status=downloaded

需要重新发送（页面丢失/参数要重来/服务重启后）      → POST /generations/{id}/resend?confirm_resend=true
同一 job_id 重新准备页面（旧会话/旧卡片/旧结果作废）→ status=awaiting_manual_send

操作者判定这个任务不再尝试                        → POST /generations/{id}/mark-failed?confirm_failed=true
→ status=failed（error_code=MANUAL_CONFIRMED_FAILED），H3 显示失败并停止等待
```

要点：

- 本地“自动调度”和“自动执行”是独立设置：前者决定是否立即放行，后者决定放行后自动发送还是等待人工发送。显式调用 `/dispatch`（“进入调度”）只放行，不会把 `manual` 任务改成 `auto`。为兼容旧 API 调用，调用方显式传 `submit_mode=manual` 时即使同时传 `auto_start=true` 仍先进入 `queued`；本地设置则支持“自动调度 + 人工发送”组合。
- 本地“异常自动换号”通过 `GET/PUT /api/settings/auto-switch-on-exception` 配置。开启后，执行任务首次遇到异常会关闭原账号窗口、解除原账号并按原参数换号自动执行；同一轮最多自动换号一次，再次异常进入人工处理，避免无限重试。
- **重新发送沿用原任务 ID**：`/resend` 不会新建 job，旧的 `web_page_url`、`result_card_index` 和结果绑定会被清空，重新准备页面。之后 `/recall` 只认这次新页面，因此“关联下载以重新发送的网页为准”。同一天内重发复用已有额度占用，不重复占用。
- **人工判定失败是最终裁决**：`/mark-failed` 把任务置为 `failed`、错误码 `MANUAL_CONFIRMED_FAILED`，并释放额度占用；H3 收到该错误码后显示失败、停止等待。判定后仍可 `/resend`，但 H3 需要手动召回才会恢复轮询。
- `manual` 任务的 `needs_review` / 普通 `failed` 在 H3 侧映射为 `processing`（最长 7 天），等待本机操作者重新发送或关联下载；只有 `MANUAL_CONFIRMED_FAILED` 才按失败处理。自动任务仍按失败处理。H3 始终轮询原 `job_id`，最终从同一个任务取回视频。
- 本地“正在处理队列上限”只统计机器正在执行的任务（`opening` 到 `downloading`）；`awaiting_manual_send` 与 `needs_review` 不占该名额，只占账号页面和额度。这样异常/人工任务积压不会阻塞后续自动任务。
- 同一账号只有一个执行通道：后续任务必须等待当前任务释放账号占用后才能进入执行；不同账号仍可在全局“正在处理队列上限”内并行。
- `/start` 完成后状态进入 `awaiting_manual_send`，`web_page_url` 可能还是首页或 `/chat/local_*`（因为还没发消息）。页面句柄保留在服务进程内，**请保持 Chrome 窗口和服务运行**；服务重启会把该状态转为 `needs_review`（错误码 `MANUAL_SEND_INTERRUPTED`）并保留额度占用。
- 出片后调用 `/recall`。它会优先复用准备时的标签页；发送后标签页 URL 会转成真实 `/chat/<id>` 并被记录，之后即使页面关闭也可按会话召回。`recall` 只下载已有结果，**绝不重新提交生成**。
- 管理台“下载视频”使用 `/recall?force_redownload=true`；即使任务已经归档，也会重新从任务页面定位并下载结果、成功后覆盖旧文件。重新下载不会改变任务状态，仍保持 `downloaded`；进度可通过 `GET /api/jobs` 的 `recall_state=recalling` 观察。重新下载失败时保留原归档文件和 `downloaded` 状态。
- 如果页面仍停在“确认生成”提示，`recall` 返回 `needs_review` / `CONFIRM_REQUIRED`；先在网页完成确认或使用“发送确认语”，再点一次“关联下载”。
- 归档成功后 `result_source=manual_web_link`；额度从占用转为已消耗。若始终取不回，可用管理台“复查归档”手工导入 MP4，`result_source=human_verified_upload`。
- 想恢复全自动：在本地管理台同时开启“自动调度”和“自动执行”。H3 保持只提交任务内容；本地服务接收后自动执行，成功归档后自动回推服务器。

---

## 四、② 轮询状态

```
GET /api/v1/videos/{job_id}
```

需要恢复已提交任务时调用：

```
POST /api/v1/videos/generations/{job_id}/recall
```

该接口只复用原会话和原 `job_id`，按提交前保存的视频卡片序号召回；不会重新提交生成。重复召回由数据库锁保护。

响应示例（字段以实际为准）：

```json
{
  "job_id": "job_xxxxx",
  "status": "downloaded",
  "mode": "reference_images",
  "ratio": "16:9",
  "duration": 15,
  "submit_mode": "manual",
  "error_code": "",
  "error_message": "",
  "result_source": "manual_web_link",
  "quota": {
    "account_id": "acc_9b661e5a38",
    "day": "2026-10-28",
    "daily_limit_seconds": 30,
    "consumed_seconds": 15,
    "reserved_seconds": 0,
    "unknown_seconds": 0,
    "queued_reserved_seconds": 0,
    "remaining_seconds": 15
  },
  "video_url": "/api/v1/videos/job_xxxxx/video",
  "web_video_url": "https://v11-vdl.doubao.com/..."
}
```

`quota` 为该任务所属账号当日的额度：`consumed_seconds` 已出片消耗，`reserved_seconds` 排队/执行中占用，`remaining_seconds = daily_limit_seconds - consumed - reserved`。账号列表 `GET /api/accounts` 与 `GET /api/accounts/{account_id}/quota` 也返回同样的额度信息。

任务详情中的 `web.dispatched_at` 非空表示任务已经交给调度器，正在等待处理名额或可用账号；为空表示仍需人工点击“进入调度”。这两种情况的任务状态都可能是 `queued`。

已放行但尚未开始处理的任务，可调用 `POST /api/v1/videos/generations/{job_id}/undispatch` 取消放行。接口停止排队协程、释放预占额度并清空 `web.dispatched_at`，保留任务与自动/人工执行方式；后台重派和服务重启不会自动恢复放行，可再次调用 `/dispatch`（或由每日定时调度重新放行）。已经开始处理时返回 409，需要停止执行请使用取消任务接口。管理台同时提供单条和批量“取消放行”。

待处理任务默认不指定账号，拿到处理名额且存在空闲账号时，才自动绑定账号并预占额度。正在处理、待人工发送或异常待复查的账号不参与自动分配；暂无空闲账号时保持未分配并后台重试。服务恢复或重派时会释放旧版排队任务提前自动分配的账号及预占额度，保留操作者明确选定的账号。操作者可调用 `POST /api/jobs/{id}/account` 并传入 `{"account_id":"acc_xxx"}` 选定账号，或传入 `{"account_id":null}` 恢复自动分配。管理台任务列表中的账号下拉框使用同一接口。

任务列表的“打开网页 / 关闭网页”分别调用 `POST /api/v1/videos/generations/{id}/open-page` 和 `/close-page`，只操作任务绑定的标签页，不会关闭同账号其他任务的网页或整个浏览器窗口。

自动下载、关联下载或复查下载成功后，若同一账号仍有其他任务，服务只关闭本任务绑定的网页；若没有其他任务，则关闭该账号的整个浏览器窗口。

### Chrome 扩展接口

扩展只通过 Service Worker 请求本地服务。设置 `DOUBAO_LOCAL_API_KEY` 时同样必须发送 `X-API-Key`，密钥保存在扩展私有存储，不注入豆包页面。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/extension/tasks/resolve?binding_token=...` | 用标签页中的随机绑定令牌解析唯一任务，返回任务信息、最近事件、五个按钮状态及人工接管状态；未知令牌返回 404，不按 URL/标题猜测 |
| POST | `/api/v1/videos/generations/{id}/takeover` | 人工接管当前任务；取消该任务的自动网页协程并保留页面和进度 |
| POST | `/api/v1/videos/generations/{id}/resume-takeover` | 结束人工接管并统一恢复为 `generating`；复用原绑定页面等待原结果，不重新提交，完成后继续自动下载归档和回推 |

五个流程按钮继续调用既有接口：`/dispatch` 或 `/prepare-manual`、`/send?confirm_send=true`、`/send-confirmation?confirm_send=true`、`/recall?force_redownload=true`、`/push-result`。扩展不维护第二套任务状态机。

### 队列状态

```
GET /api/v1/videos/queue/status
```

```json
{
  "pending": 2,
  "pending_unassigned": 1,
  "pending_dispatched": 1,
  "pending_undispatched": 1,
  "processing": 1,
  "processing_limit": 3
}
```

- `pending`：全部待处理任务数，保留该字段是为了兼容旧调用方。
- `pending_undispatched`：尚未人工放行、仍在等待“进入调度”的任务数。
- `pending_dispatched`：已经放行、但仍在等待处理名额或可用账号的任务数。
- `pending_unassigned`：尚未分配账号的待处理任务数。

### 状态机

```
queued → (auto) opening → configuring → uploading → submitting → generating → downloading → downloaded
queued → (manual) /start → opening → configuring → uploading → awaiting_manual_send
       → 人工在豆包网页发送并出片 → 操作者点“关联下载”(recall) → downloaded
       → 需要重来：/resend（同一 job_id，旧页面作废）→ opening → … → awaiting_manual_send
       → 操作者判定失败：/mark-failed（MANUAL_CONFIRMED_FAILED）→ failed（H3 显示失败）
```

| 状态 | 含义 |
| --- | --- |
| `awaiting_manual_send` | 页面已填好提示词和素材，等待操作者在豆包网页亲自点击发送 |
| `downloaded` | 成功，视频已归档，可以下载 |
| `needs_review` | 需人工处理（页面无法确认、额度/验证码/登录等），**不要自动重发** |
| `failed` | 失败 |

`queued` 同时承载“尚未人工放行”和“已放行、正在等名额/账号”两种等待原因；使用 `web.dispatched_at` 或队列状态接口区分。

轮询建议间隔 3～5 秒，超时时间建议 15 分钟（4 秒视频约 2～3 分钟，15 秒约 3～5 分钟）。

---

## 五、③ 下载视频

```
GET /api/v1/videos/{job_id}/video
```

- `status=downloaded` 时：返回 `200` + MP4 字节流（`Content-Type: video/mp4`）。
- 未生成完成时：返回 `404`。

---

## 六、@图N 自然语言机制

图片和提示词的关联方式是：**图片作为附件卡片 + 提示词纯文本，同一条消息发出**。豆包后台自动把图片当参考画面、文字当动作/运镜描述。

调用方可以在提示词里用 `@图N` 标记（N 是第几张参考图，1 起），提交时系统会自动翻译成自然语言「参考图N」：

| 输入 | 翻译后 |
| --- | --- |
| `@图1` | `参考图1` |
| `@图2` | `参考图2` |

示例：

```
输入提示词：@图1 的红色纸船作为主体，@图2 的蓝色方块作为湖面背景，缓缓漂动
翻译后    ：参考图1 的红色纸船作为主体，参考图2 的蓝色方块作为湖面背景，缓缓漂动
```

图片按 `reference_asset_ids` 的顺序作为附件上传，所以 `@图1` 对应第一张、`@图2` 对应第二张，以此类推。

---

## 七、完整调用示例

### 7.1 curl

```bash
# ① 提交：参考生视频，2 张图，15 秒（模型由本机任务设置决定）
curl -X POST http://127.0.0.1:8765/api/v1/videos \
  -F "account_id=acc_9b661e5a38" \
  -F "prompt=@图1 的红色纸船作为主体，@图2 的蓝色方块作为湖面背景，缓缓漂动" \
  -F "mode=reference_images" \
  -F "ratio=16:9" -F "duration=15" \
  -F "project_name=大妖也得为我打工" -F "episode_name=第一集" -F "shot_name=苏小棠抬头" \
  -F "reference=@D:/images/角色.png" \
  -F "reference=@D:/images/场景.png"
# 返回 { "job_id": "job_xxxxx", ... }

# ② 轮询直到 status=downloaded
curl http://127.0.0.1:8765/api/v1/videos/job_xxxxx

# ③ 下载视频
curl -o out.mp4 http://127.0.0.1:8765/api/v1/videos/job_xxxxx/video
```

### 7.2 Python（requests）

```python
import time
import requests

BASE = "http://127.0.0.1:8765"
HEADERS = {}  # 若设了 DOUBAO_LOCAL_API_KEY，加 {"X-API-Key": "..."}

def submit_reference_video(reference_paths, prompt, account_id=None,
                           duration=4, ratio="16:9", model=None,
                           project="", episode="", shot=""):
    files = [("reference", (p.split("/")[-1], open(p, "rb"), "image/png"))
             for p in reference_paths]
    data = {
        "prompt": prompt,
        "mode": "reference_images",
        "ratio": ratio,
        "duration": duration,
        "project_name": project,
        "episode_name": episode,
        "shot_name": shot,
        "auto_start": "true",
    }
    if account_id:
        data["account_id"] = account_id  # 留空则后端自动选账号
    if model:
        data["model"] = model
    r = requests.post(f"{BASE}/api/v1/videos", data=data, files=files, headers=HEADERS)
    r.raise_for_status()
    return r.json()["job_id"]

def wait_and_download(job_id, timeout=900, poll=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = requests.get(f"{BASE}/api/v1/videos/{job_id}", headers=HEADERS).json()
        status = job["status"]
        if status == "downloaded":
            r = requests.get(f"{BASE}/api/v1/videos/{job_id}/video", headers=HEADERS)
            r.raise_for_status()
            out = f"{job_id}.mp4"
            with open(out, "wb") as f:
                f.write(r.content)
            return {"job_id": job_id, "video_path": out, "bytes": len(r.content)}
        if status in ("needs_review", "failed"):
            raise RuntimeError(f"生成失败 {status}: {job.get('error_code')} {job.get('error_message')}")
        time.sleep(poll)
    raise TimeoutError("等待超时")

# 用法
job_id = submit_reference_video(
    ["D:/images/角色.png", "D:/images/场景.png"],
    prompt="@图1 的红色纸船作为主体，@图2 的蓝色方块作为湖面背景，缓缓漂动",
    account_id="acc_9b661e5a38",
    duration=15, ratio="16:9", model="Seedance 2.0 Fast",
    project="大妖也得为我打工", episode="EP01", shot="SHOT03",
)
result = wait_and_download(job_id)
print(result)
```

---

## 八、错误与失败处理

- `needs_review` / `failed`：**不要自动重发**（可能已经消耗额度）。先查日志/事件，人工核对豆包网页后再处理。
- 常见 `error_code`：`LOGIN_REQUIRED`（需登录）、`VERIFICATION_REQUIRED`（需验证码）、`ACCOUNT_LIMIT`（额度不足）、`SUBMISSION_UNCONFIRMED`（无法证明已提交）、`MODEL_NOT_FOUND`（模型不存在）、`WAIT_TIMEOUT`（生成超时）。
- 幂等：相同的 `idempotency_key` 重复调用会返回已有任务，不会重复建单。
