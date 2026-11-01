# Claude 异常处理 Agent

本功能是豆包视频业务的增量模块，默认关闭。正常任务不会调用 Agent；只有现有
worker、recall worker、下载/回推异常出口在已经完成原状态更新后，才会异步转交诊断。

## 配置

支持以下环境变量：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API Key | 空 |
| `ANTHROPIC_MODEL` | Claude 模型 ID | 空 |
| `ANTHROPIC_BASE_URL` | Anthropic Messages API 根地址 | `https://api.anthropic.com` |
| `DOUBAO_AGENT_ENABLED` | 是否启用 | `false` |
| `DOUBAO_AGENT_MODE` | `diagnose_only` 或 `auto_safe` | `diagnose_only` |
| `DOUBAO_AGENT_TIMEOUT_SECONDS` | Claude API 超时秒数 | `30` |
| `DOUBAO_AGENT_RECOVERY_OBSERVE_SECONDS` | 执行原任务召回后观察结果的秒数 | `20` |
| `DOUBAO_AGENT_DEDUPE_SECONDS` | 同一任务触发去重窗口 | `300` |
| `DOUBAO_AGENT_MAX_ACTIONS` | 单次异常最多连续执行的恢复动作数 | `4` |
| `DOUBAO_AGENT_MAX_CONCURRENCY` | 同时处理的异常数 | `2` |

Agent 使用 Claude Messages API。PowerShell 示例：

```powershell
$env:ANTHROPIC_BASE_URL = "https://api.anthropic.com"
$env:ANTHROPIC_MODEL = "<Anthropic 控制台中可用的 Claude 模型 ID>"
$env:ANTHROPIC_API_KEY = "<真实的 Anthropic API Key>"
$env:DOUBAO_AGENT_ENABLED = "true"
$env:DOUBAO_AGENT_MODE = "diagnose_only"
```

`localhost:60007` 只提供 Anthropic 兼容接口，并不代表它背后的模型是 Claude。
当前该地址返回的是 `deepseek-v4-flash`，不能作为 Claude Agent 的验收配置。若使用
自建 Claude 网关，需确认网关实际路由到 Claude 模型，再设置对应的
`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL` 和鉴权值。

本应用会读取项目级 `.claude/settings.local.json` 中的 `env`。进程环境变量优先，
未设置的环境变量才使用该文件中的值。该文件已加入 `.gitignore`，避免提交本地鉴权值。
当前文件连接的是 `localhost:60007` 的 `deepseek-v4-flash`，因此只能用于接口联调，
尚不是 Claude 模型。

首次启用建议保持 `diagnose_only`，确认日志中的诊断稳定后再切换为 `auto_safe`。

## 触发位置

- 自动 worker 的 `ReviewNeeded` / 未预期异常出口。
- recall worker 的 `ReviewNeeded` / 未预期异常出口。
- 人工任务点击发送后出现 `SUBMISSION_UNCONFIRMED` 的出口。
- 人工确认语发送失败/不确定的既有异常出口。
- H3 回推异常出口。

原异常处理、任务状态和业务事件仍先按原逻辑写入。Agent 只创建后台任务，Claude
超时、网络错误或诊断失败只会写入独立日志，不会覆盖原异常。

## 异常与恢复策略

### 代理可自行处置（放开权限）

在 `auto_safe` 模式下，代理可以**连续执行多个恢复动作**把异常处理到底，
一个动作失败就换下一个思路，直到救回或达到动作上限
（`DOUBAO_AGENT_MAX_ACTIONS`，默认 4，同一动作不会重复尝试）：

| 工具 | 作用 | 适用 |
| --- | --- | --- |
| `restart_browser` | 重启该账号的受管浏览器 | 浏览器崩溃、上下文失效 |
| `refresh_page` | 刷新原任务页 | 页面卡死、DOM 异常 |
| `recall_existing_result` | 按原任务绑定关系召回已有结果 | 等待超时、结果未出现、下载失败 |
| `retry_h3_push` | 重推已有归档视频到 H3 | 回推失败 |
| `restart_auto` | 就地重新自动执行（保持原任务 ID） | 提交前失败、确认未花费额度 |
| `regenerate` | 复制出新任务并重跑（原任务保留） | 原任务已不可救、确认未花费额度 |

提交前的参数/模型/素材/提示词/提交按钮类失败，代理会自行修复或重跑；
带 `SUBMISSION_UNCONFIRMED`、`CONFIRM_*` 等“可能已提交”的异常也会交给代理，
但它只能做不花钱的动作。

### 仍然必须人工处理（代码强制红线）

下面这些即使放开权限也不会自动执行，是写在策略里的硬边界：

- 登录失效。
- 验证码、安全验证、人机验证。
- 账号额度不足（禁止换号或绕过限制）。
- 无法判断是否已经扣费（`quota_state=unknown`）。
- 任务已进入 `confirming` / `generating` / `downloading`，
  或错误码表明可能已提交/已扣费：禁止 `restart_auto` 与 `regenerate`，
  避免为一次可能已经花过钱的生成再埋单。
- 已存在归档视频：禁止召回覆盖或重新生成覆盖。

Agent 始终不提供修改提示词、更换图片、切换账号、绕过额度、删除或覆盖视频的工具；
策略层会拒绝这些动作。`diagnose_only` 模式下完全不执行任何动作。


## 上下文与日志

传给模型的内容只有文本和结构化数据，不包含截图、OCR 或视觉识别结果，并会剔除
`Cookie`、密码、`Authorization`、API Key 等敏感信息。网页内容和助手消息会明确标记
为不可信数据，不能覆盖系统提示词。

Agent 独立日志：

```text
data/agent_recovery/exceptions.jsonl
```

每条记录包含异常时间、`job_id`、异常类型/代码、诊断结果、修复建议、执行动作、
恢复是否成功，以及是否需要人工介入。不会修改原有业务数据库表结构。

## 测试

测试使用 Mock Claude 和离线任务数据，不访问真实豆包账号：

```powershell
uv run python -m pytest tests/test_agent_recovery.py -q
```
