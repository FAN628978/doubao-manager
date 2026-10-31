# 豆包网页版视频生成管理台 V3（Windows / 本地独立部署）

**当前验收结论（请先读）：** V3 已通过本地离线集成测试与豆包官网实机验收。参考生视频（reference_images）的完整链路「提交 → 确认生成 → 生成 → 自动下载 → 校验」已跑通，4 秒与 15 秒均 `LIVE_AUTO_PASS`。时长、画幅、模型均以豆包网页实际提供的选项为准，不再硬编码。外部系统推荐使用下方「单入口对外接口」做拉取式集成。

这个项目作为漫剧系统的独立生成服务，不修改你现有前端或后端。默认仅监听 `127.0.0.1:8765`。

## 七项能力与验收状态

| 能力 | V3 实现 | 本地测试 | 豆包官网实机 |
| --- | --- | --- | --- |
| 文生视频网页提交 | Playwright 从豆包首页激活视频生成并提交 | Chromium 模拟页通过 | 提交控件已适配，出片待验收 |
| 生成状态、下载识别 | 区分完成标志/新视频源，校验 MP4/WEBM、原子归档 | Chromium + 本地实际视频字节通过 | **已实测（参考生视频 LIVE_AUTO_PASS）** |
| 精确比例与时长 | 点击/选择明确网页控件并回读验证，不靠提示词猜测；以豆包为准 | 16:9 / 9:16、4s / 10s / 15s 验证通过 | **16:9 / 4s / 15s 已实测** |
| 首帧图生视频 | 首帧上传槽、上传完成标记核对 | Chromium 模拟页通过 | 待验收 |
| 首尾帧生视频 | 首/尾独立上传槽，要求分别确认 | Chromium 模拟页通过 | 待验收 |
| 人物场景多参考图 | 1～9 张图片输入、数量一致性确认 | Chromium 模拟页通过 | **单张已出片实测（4s/15s），多张上传已实测** |
| 模型选择 | 下拉菜单按模型名精确匹配选中 | — | **Seedance 2.0 Fast 选择已实测** |
| 防重复与安全恢复 | 数据库幂等键、原子认领、同账号单任务槽、重启转人工 | 本地 API/断点测试通过 | 待验收 |

**关键限制：** 当前页面控件已按 2026-10-28 的豆包 DOM 适配；页面改版后仍可能需要调整 `web_api.py` 的选项、上传、输入框和提交按钮选择器。文生视频、首帧/首尾帧、多张参考图的出片仍待实机验收。

**账号每日视频额度：** 每个账号每天 30 秒（按本地自然日归零，可用环境变量 `DOUBAO_DAILY_VIDEO_SECONDS` 覆盖）。任务创建时按 `duration` 占用额度，出片归档后记为已消耗；`needs_review`/`failed` 释放占用。提交任务时 `account_id` 留空则自动选择当日剩余额度足够的账号（优先空闲账号），额度不足会返回 409 而非硬凑。额度信息在「账号管理」列表、`GET /api/accounts/{id}/quota`、任务详情 `quota` 字段中可见。

**账号会员与优先级：** 账号管理中可标记会员账号并设置 `0～1000` 的优先级，数字越大越优先。自动选择账号时依次考虑重试避让、账号是否空闲、账号优先级和剩余额度。会员账号创建或启动视频任务时固定使用 `Seedance 2.0`，调用方传入的 `model` 参数会被忽略。

**暂停账号：** 账号管理中的“暂停使用”会让该账号退出新任务的自动选择，并拒绝用它新建、启动或人工发送生成任务；已经运行的任务不会被中断。暂停期间仍可打开浏览器维护登录状态或查看历史结果，点击“恢复使用”即可重新启用。

**并发执行：** 不同账号使用隔离的 Chrome 档案并行执行，同一账号内的任务保持串行，避免页面互相干扰。默认不限制同时运行的账号数；如需限制浏览器资源占用，可将环境变量 `DOUBAO_MAX_CONCURRENT_ACCOUNTS` 设置为正整数。

## 安装与启动（Windows 10/11）

要求：Python 3.10+、Chrome、可访问豆包的网络环境。运行 Chrome 需要有 Windows 桌面会话。

1. 解压 ZIP 到固定位置，例如 `D:\DoubaoAccountDeskV3`，**不要覆盖 V2 文件夹**。
2. 双击 `setup.bat`，安装 Python 依赖及 Playwright Chrome。
3. 双击 `start.bat`，浏览器访问 `http://127.0.0.1:8765`。
4. 进入“账号管理”创建账号档案（名称留空则自动按 `doubao01`、`doubao02`… 命名），点击“打开豆包”，在独立 Chrome 窗口中**自行完成登录**。误建的档案可用“删除”移除，登录资料会先移动到 `data/trash/`。
5. 进入“分镜任务” → “多模式网页生成”，配置模式、比例、时长，按需上传图片并创建任务。
6. 状态可能为 `opening → configuring → uploading → submitting → generating → confirming（豆包要求确认时）→ downloading → downloaded`。人工发送任务会先停在 `queued`，本机点击“发送到豆包”后进入 `awaiting_manual_send`，由你在豆包网页亲自发送，出片后点“关联下载”归档。若页面无法确认，会进入 `needs_review`；点击“日志”可查看原因。人工确认了对应视频时可以使用“复查归档”，其结果会标记为 `human_verified_upload`，**不算自动出片验证**。

> 仅使用自己有权操作的账号及允许的网页功能。脚本不会提取/共享 Cookie，不会绕过验证码、登录挑战、访问限制或生成额度限制，也不会自动切换账号。

### 便携版升级并保留登录状态

1. 完全关闭旧版程序及其打开的 Chrome 窗口。
2. 将新版 ZIP 解压到一个新文件夹。
3. 把旧版目录中的整个 `data` 文件夹复制到新版解压目录，替换新版的空 `data` 文件夹。
4. 启动新版。程序会自动升级数据库结构，并把账号 Profile 的旧绝对路径转换为相对路径 `data/profiles/<账号ID>`，无需重新登录。

请复制整个 `data`，不要只复制 `manager.sqlite3` 或单个 `profiles` 子目录。登录状态通常只能在原 Windows 用户和原电脑上继续使用。

从当前便携版开始，SQLite 中的账号 Profile、视频和素材文件位置只保存相对路径，并限制在 EXE 同目录的 `data` 内；数据库使用结构版本号管理。后续版本必须在保留该 `data` 布局的基础上做增量升级。

## 本地自动化测试

安装 **FFmpeg（包含 ffprobe）** 并加入 `PATH` 后，双击 **`selftest.bat`**。该脚本使用真实 Chrome/Chromium 与本地构造的模拟页面，验证点击选项、素材上传、提交、识别视频、下载字节、异常处理、幂等和重启保护。

开发者也可以执行：

```powershell
python -m pip install -r requirements-test.txt
python -m playwright install chrome
python -m pytest -q tests
```

Linux 测试用 `/usr/bin/chromium`；Windows 用 Playwright 安装的 Chrome。离线页面模拟的测试文件在 `tests/`，**不访问豆包官网**。

## 豆包官网实机验收（只有真正出片后才算通过）

1. 运行服务，先在账号管理台打开对应 Chrome 并人工登录豆包。
2. 在 PowerShell/CMD 中进入工程目录，用你自己的 `acc_...` 账号档案 ID 执行以下命令。脚本会要求确认生成额度。

```bat
:: 文生视频（首轮推荐只做 1 个 4 秒镜头）
test-live.bat --account-id acc_xxxxxx --mode text2video --duration 4 --ratio 16:9 --prompt "电影级国漫，远景雨后城楼，缓慢推进"

:: 首帧图生视频
test-live.bat --account-id acc_xxxxxx --mode image2video --first-frame "D:\images\first.png" --duration 4 --ratio 16:9

:: 首尾帧生视频
test-live.bat --account-id acc_xxxxxx --mode first_last_frame --first-frame "D:\images\first.png" --last-frame "D:\images\last.png"

:: 人物/场景参考图
test-live.bat --account-id acc_xxxxxx --mode reference_images --reference "D:\images\role.png" --reference "D:\images\scene.png"
```

如果中断或超时，只按已有任务 ID 继续查询，**不要重新执行生成命令**：

```bat
test-live.bat --job-id job_xxxxxxxxxxxx
```

测试报告及验证下载文件保存在 `data/acceptance_reports/`，包含任务 ID、各阶段状态、实际媒体大小、SHA256、可选 ffprobe 视频流信息，以及 `live_website_verified` 布尔值。只有 `result_source=web_automated` 且下载完成，脚本才会报告 `LIVE_AUTO_PASS`。人工导入时显示 `MANUAL_FILE_VERIFIED`，不声称自动出片成功。测试失败时保留错误状态与报告；不得盲目重复提交。

## 单入口对外接口（推荐，供外部系统集成，拉取式）

外部系统只需三个接口：提交 → 轮询 → 下载。默认 `http://127.0.0.1:8765`。若设置了 `DOUBAO_LOCAL_API_KEY`，所有接口需带 `X-API-Key` 请求头。

### ① 提交任务（图片直接作为文件字段上传）

`POST /api/v1/videos`，`Content-Type: multipart/form-data`。

**普通字段：**

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `account_id` | string | 可选 | 自动选择 | 账号 ID；留空则后端自动选一个可用账号 |
| `prompt` | string | ✅ | — | 提示词；`@图N` 会自动翻译成「参考图N」 |
| `mode` | string | 可选 | `text2video` | `text2video` / `image2video` / `first_last_frame` / `reference_images` |
| `ratio` | string | 可选 | `16:9` | 画幅，以豆包为准（常见 `16:9` / `9:16` / `1:1`） |
| `duration` | int | 可选 | `4` | 秒数，以豆包为准（常见 `4` / `10` / `15`） |
| `model` | string | 可选 | 豆包默认 | `Seedance 2.5` / `Seedance 2.0` / `Seedance 2.0 Fast` / `Seedance 2.0 Mini` |
| `project_name` `episode_name` `shot_name` | string | 可选 | 空 | 项目、集数、镜头名称 |
| `idempotency_key` | string | 可选 | 空 | 幂等键，重复调用不重复建单 |
| `auto_start` | bool | 可选 | `true` | `true` 立即执行；`false` 排队后用 `start` 启动 |

**文件字段（按 mode）：**

| mode | 文件字段 |
| --- | --- |
| `reference_images` | `reference`（1～9 个文件） |
| `image2video` | `first_frame`（1 个） |
| `first_last_frame` | `first_frame` + `last_frame`（各 1 个） |
| `text2video` | 无 |

图片约束：PNG / JPEG / WEBP，≤15MB，128～8192 像素。

**响应（202）：**

```json
{ "job_id": "job_xxx", "status": "queued",
  "status_url": "/api/v1/videos/job_xxx",
  "video_url": "/api/v1/videos/job_xxx/video" }
```

### ② 轮询状态

`GET /api/v1/videos/{job_id}` → 返回 `status`、`error_code`、`error_message`、`video_url` 等。

状态机：`queued → opening → configuring → uploading → submitting → generating → confirming（豆包要求确认时）→ downloading → downloaded`。终态：`downloaded` 成功，`needs_review` / `failed` 失败（失败不要重发，需人工处理）。

### ③ 回传视频

`GET /api/v1/videos/{job_id}/video` → `status=downloaded` 时直接返回 MP4 字节流；未生成返回 404。

### 完整示例（curl）

```bash
# 参考生视频，多张图 + 模型 + 15 秒
curl -X POST http://127.0.0.1:8765/api/v1/videos \
  -F "account_id=acc_9b661e5a38" \
  -F "prompt=@图1 的红色纸船作为主体，@图2 的蓝色方块作为湖面背景，缓缓漂动" \
  -F "mode=reference_images" \
  -F "ratio=16:9" -F "duration=15" -F "model=Seedance 2.0 Fast" \
  -F "project_name=大妖也得为我打工" -F "episode_name=第一集" -F "shot_name=苏小棠抬头" \
  -F "reference=@D:/images/角色.png" -F "reference=@D:/images/场景.png"

# 轮询
curl http://127.0.0.1:8765/api/v1/videos/job_xxx

# 下载
curl -o out.mp4 http://127.0.0.1:8765/api/v1/videos/job_xxx/video
```

> 提示词里的 `@图N` 会在提交时自动翻译成自然语言「参考图N」（例如 `@图1` → `参考图1`）。图片按 `reference_asset_ids` 顺序作为附件上传，两者作为同一条消息发出，豆包后台自动把图片当参考画面、文字当动作描述。

## 本地任务模式与人工发送

任务模式由本地管理台顶部的“任务模式设置”统一选择：默认“自动执行”，也可切换为“人工处理”。远端 `lzg` 上的 H3 只提供任务和回推信息，不传 `submit_mode` 或 `auto_start`；本地服务按当前设置决定执行方式。

人工模式的流程如下：

1. **H3 建单**：`POST /api/v1/videos`（或 `/api/v1/videos/generations`）只提交任务内容。任务按本地“人工处理”设置进入 `queued`，不会自动执行。
2. **本机点击“发送到豆包”**：管理台调用 `POST /api/v1/videos/generations/{id}/start`。脚本会打开豆包、选择模式/比例/时长/模型、上传参考图，并把提示词填进输入框；**不点击发送**。任务状态变为 `awaiting_manual_send`，页面保持打开并置于前台。
3. **你在豆包网页亲自发送**：需要确认参数时也由你在网页处理（或用管理台“发送确认语”按钮）。
4. **出片后点“下载视频”**：管理台调用 `POST /api/v1/videos/generations/{id}/recall?force_redownload=true` 复用准备时的标签页，按提交前记录的卡片序号重新下载并校验视频，状态变为 `downloaded`，`result_source=manual_web_link`。
5. **点“推送服务器”**：H3 提交任务时会附带单任务回推地址和随机令牌；管理台调用 `POST /api/v1/videos/generations/{id}/push-result`，主动把已归档 MP4 回传 H3。H3 只提交、不轮询本地管理台。
6. **需要再次执行时**：任务行“复制为新任务”（`POST /api/v1/videos/generations/{id}/copy?confirm_copy=true`）按服务器最初提交的参数创建新任务，继承提交方式和服务器回推信息，但不复制网页会话、运行状态或视频结果；系统会重新选择额度足够的空闲账号（优先避开原账号）。复制出的任务保持 `queued`，不会自动执行，由操作者在任务列表中手动启动。原任务保持不变。

约束与兜底：

- 准备页面和 Chrome 窗口要一直开着；服务重启会把 `awaiting_manual_send` 转成 `needs_review`（`MANUAL_SEND_INTERRUPTED`），额度按“可能已消耗”保留占用。此时仍可点“重新发送”沿用原任务重来，不需要在 H3 另建任务。
- 同一天内的重新发送沿用它已有的额度占用，不会重复占用；跨天或额度已释放时才重新占用当天额度。
- H3 侧对 manual 任务的 `needs_review` / 普通 `failed` 保持 `processing`（最长 7 天），等待本机操作者重新发送或关联下载；只有操作者明确“判定失败”（`MANUAL_CONFIRMED_FAILED`）时才显示失败并停止等待。自动任务仍按失败处理。
- 如果豆包还停在“确认生成”，点“关联下载”会得到 `CONFIRM_REQUIRED`；先在网页确认，再点一次。
- 页面丢失或无法关联时，用“复查归档”手工导入 MP4；下载成功后额度会结算为已消耗。
- 想恢复全自动，在本地管理台把“任务模式设置”切换为“自动执行”；后续 H3 任务接收后会立即执行，成功归档后自动回推服务器。

### SSH 隧道（H3 → 本机 8765）

本机服务只监听 `127.0.0.1:8765`。远端 H3 通过 SSH 反向隧道访问，`~/.ssh/config` 中 `Host lzg` 必须写 IPv4 目标：

```sshconfig
Host lzg
  HostName 183.224.229.249
  User lzg
  Port 2223
  RemoteForward 8765 127.0.0.1:8765
```

保持一个 `ssh lzg` 会话不断开，远端即可用 `curl http://127.0.0.1:8765/api/health` 访问管理台。H3 的 `doubao_local` 视频配置 `base_url` 填 `http://127.0.0.1:8765/`，需要鉴权时同时在管理台设置 `DOUBAO_LOCAL_API_KEY` 并在 H3 配置里填同一个 `X-API-Key`。

## 对外 HTTP API

默认主机：`http://127.0.0.1:8765`，Swagger：`http://127.0.0.1:8765/docs`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/accounts` | 创建账号档案（`name` 留空则默认命名 `doubao01`、`doubao02`…） |
| GET | `/api/accounts` | 列出未归档账号 |
| GET | `/api/accounts/suggested-name` | 返回下一个默认账号名 |
| PATCH | `/api/accounts/{id}` | 设置账号会员标识与优先级 |
| POST | `/api/accounts/{id}/open` | 打开对应账号的独立 Chrome |
| POST | `/api/accounts/{id}/pause` | 暂停账号用于新任务（不打断已运行任务） |
| POST | `/api/accounts/{id}/resume` | 恢复账号用于新任务 |
| DELETE | `/api/accounts/{id}` | 删除账号档案：登录资料默认移入 `data/trash/`，`?purge_data=true` 彻底删除 |
| POST | `/api/v1/videos/assets` | multipart 上传 PNG/JPEG/WEBP（≤15MB） |
| POST | `/api/v1/videos/generations` | 创建网页生成任务 |
| POST | `/api/v1/videos/generations/{id}/start` | 仅启动 `queued` 任务，重复执行返回 409 |
| POST | `/api/v1/videos/generations/{id}/retry?confirm_retry=true` | 复制 `needs_review` / `failed` 未完成任务的提示词、模式和素材，优先更换为空闲且额度足够的账号后重新排队 |
| POST | `/api/v1/videos/generations/{id}/copy?confirm_copy=true` | 按服务器初始提交参数复制为新的 `queued` 任务，重新选择账号并继承原服务器回推信息 |
| GET | `/api/v1/videos/generations/{id}` | 查询任务状态、生成参数、归档信息 |
| GET | `/api/v1/videos/generations/{id}/events` | 查看状态事件与错误 |
| POST | `/api/v1/videos/generations/{id}/recall` | 幂等召回原任务：复用原会话并按绑定卡片归档；数据库锁防止重复召回，绝不重新提交生成 |
| POST | `/api/v1/videos/generations/{id}/open-page` | 在任务所属账号的托管 Chrome 中打开已记录的豆包会话页；已有同页标签时直接切到前台 |
| GET/POST/PUT/DELETE | `/api/v1/videos/confirmation-presets` | 查询、新增、编辑或删除确认语预设；默认预设可编辑但不可删除，自动执行遇到确认步骤时实时使用该默认预设 |
| POST | `/api/v1/videos/generations/{id}/send-confirmation?confirm_send=true` | 从请求体选择 `preset_id`，随时向任务已记录的豆包会话页发送该确认语；重复发送可能再次触发生成或消耗额度 |
| POST | `/api/v1/videos/generations/{id}/download-result?confirm_review=true` | 兼容旧调用；内部使用统一召回流程 |
| POST | `/api/v1/videos/generations/{id}/push-result` | 把本地已归档视频主动回推给创建任务的 H3 服务 |
| POST | `/api/jobs/bulk-delete` | 永久删除所选任务及其归档视频，不写入 `data/trash`；进行中的任务拒绝删除 |
| GET | `/api/v1/videos/confirm-answer` | 返回当前默认预设中的确认语原文 |
| POST | `/api/v1/videos/generations/{id}/manual-result` | `needs_review` 人工核对并上传 MP4/WEBM 归档 |
| POST | `/api/v1/videos/generations/{id}/reconcile` | **仅**在已有该任务准确视频 URL 时尝试下载；不会重新生成 |
| GET | `/api/jobs/{id}/video` | 获取归档视频 |
| GET | `/api/v1/videos/capabilities` | 当前适配器能力说明 |

完整创建请求示例：

```json
{
  "account_id": "acc_xxxxxx",
  "project_id": "大妖也得为我打工",
  "episode_id": "EP01",
  "shot_id": "SHOT03",
  "prompt": "电影级3D半写实国漫，苏小棠在公房猛地抬头，暖色窗光，摄影机缓慢推进。",
  "mode": "image2video",
  "first_frame_asset_id": "asset_替换成上传图片返回的ID",
  "ratio": "16:9",
  "duration": 4,
  "idempotency_key": "dayao-ep01-shot03-v1",
  "auto_start": false
}
```

`mode` 支持：`text2video` / `image2video` / `first_last_frame` / `reference_images`。首尾帧模式还需 `last_frame_asset_id`；多参考图需要 `reference_asset_ids`（1～9 个）。`duration` 当前允许 `4`、`10` 或 `15`，`ratio` 为 `16:9`、`9:16`、`1:1`。**这些是当前适配器的校验范围，不是对豆包官网支持规格的保证。**

### 状态说明与安全边界

- `queued` 任务可主动启动；其他状态不得重复启动。
- 成功点击“生成”但无法确认提交时，任务转 `needs_review`。这时需要人工核对官网历史记录，避免双重计费。
- 重新启动服务时，`opening` / `configuring` / `uploading` / `submitting` / `generating` / `downloading` 任务会保守转 `needs_review`，不重发。
- 页面如果要求登录、验证码、额度、会员、风控确认，会停止执行并等待用户处理。
- 下载源必须是允许的 HTTPS 视频 CDN 域名；未知域名/重定向拒绝自动访问；仅接受 MP4/WEBM，并在系统有 ffprobe 时校验包含视频流。
- `data/` 有浏览器登录态、素材、私人媒体与数据库。绝不可提交到 Git、发给他人或公开共享。
- 新建账号档案时若名称留空，按 `doubao01`、`doubao02`… 顺序命名：取已有 `doubao+数字` 档案（含已归档）的最大编号 +1，编号不足两位补零，重号自动跳过；前端“添加账号档案”会预填这个名字，可改。
- “归档”只把账号移出列表，不动任何文件；“删除账号档案”会解除任务的账号关联（任务与已归档视频保留），并把该账号的登录资料与浏览器下载目录移动到 `data/trash/<时间>_<账号ID>/`，确认无误后可手动清空；`?purge_data=true` 直接删除不进回收目录。浏览器窗口未关闭或有任务在跑时会拒绝删除。

若设置环境变量 `DOUBAO_LOCAL_API_KEY`，`/api/v1/videos/*` 写/读接口要求请求头 `X-API-Key`；当前旧版管理 API 和静态前端仍仅用于可信本机环境，**不应暴露公网或不可信局域网**。实机脚本可从同名环境变量读取密钥。

## 文件结构

```text
app.py                       # FastAPI 管理服务、账号、分镜、手工导入
web_api.py                   # 网页生成四模式执行器、状态机与防误报
static/index.html            # Windows 本地管理台前端（网页应用）
scripts/live_acceptance.py   # 在 Windows 已登录浏览器上做真实出片验收
selftest.bat                 # 离线自动化回归测试
test-live.bat                # 实机验收入口
setup.bat / start.bat        # 安装与启动
tests/                       # Chromium + API + 异常用例
requirements-test.txt        # 开发/测试依赖
data/                        # 含会话和视频；发布 ZIP 仅含空目录，升级时复制旧版整个 data
```

## 实际限制与后续适配

目前已确认豆包正式页面的普通参考图上传槽和提交控件，尚未完整确认提交后的结果卡片与真实下载地址。无验证时适配器会显式报错而非冒险重发。下一次实机验收应先人工核对当前 `SUBMISSION_UNCONFIRMED` 任务是否已经生成，再测试修复后的提交证据与结果下载识别。

### 从旧版本迁移账号（可选）

如果 V2 已有登录状态，请**先关闭 V2 与 V3 服务**，在一个尚未建立 V3 数据库的全新 V3 目录运行：

```powershell
python scripts/migrate_from_v2.py --from-dir "D:\DoubaoAccountDeskV2" --to-dir "D:\DoubaoAccountDeskV3"
```

脚本不会覆盖已有 V3 数据库，会复制账号资料目录与视频文件，更新新目录下的账号 profile 路径。迁移前请自己备份 `data/`；本功能只适用于旧版数据布局，不负责迁移远端账号权限或登录状态失效问题。
