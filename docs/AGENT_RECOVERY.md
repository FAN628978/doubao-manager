# Claude CLI 异常自主恢复

异常恢复入口默认启动本机 `claude -p` 非交互进程。程序启动时自动检测并接管异常，
可在管理台“任务执行设置”中通过“Claude 异常恢复”开关动态启停；无需配置 API Key 或模型。正常视频生成、下载和调度链路不会启动 Claude。
每个异常任务拥有独立的 CLI 进程、MCP 工具会话和额度预算。

运行时只使用 Claude Code CLI；不再提供 Messages API 或其他 Agent 的入口或自动回退。

角色定义和业务知识按 Claude Code 项目结构维护：

- `.claude/agents/doubao-recovery.md`
- `.claude/skills/doubao-recovery/SKILL.md`

服务在启动 `claude -p` 时读取这两份定义并注入任务上下文。MCP 配置因任务
和一次性 token 而动态生成，不写入项目级静态 `.mcp.json`。

## 环境要求

- 已安装并登录 Claude Code CLI，终端可执行 `claude --version`。
- 使用 CLI 自己的 Claude 登录和模型配置；异常恢复服务不会读取或转发
  `.claude/settings.local.json` 中的 `ANTHROPIC_*` 旧配置。
- 管理台通过 `start.bat` 或 `python -m uvicorn app:app --app-dir src` 在
  `127.0.0.1:8765` 运行。
- 运行管理台的 Python 环境可以执行 `python -m agent_recovery.mcp_server`。

## 配置

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DOUBAO_AGENT_ENABLED` | 环境级总开关；设为 `false` 时管理台不能启用 Claude | `true` |
| `DOUBAO_CLAUDE_CLI_PATH` | Claude CLI 绝对路径；留空自动检测 PATH 和 npm 目录 | 自动检测 |
| `DOUBAO_AGENT_CLI_TIMEOUT_SECONDS` | 单个 Claude CLI 进程超时秒数 | `900` |
| `DOUBAO_AGENT_CLI_PERMISSION_MODE` | 旧配置兼容字段；实际固定为最高权限 `bypassPermissions` | `bypassPermissions` |
| `DOUBAO_AGENT_PYTHON_PATH` | MCP 子进程 Python 路径 | 当前 Python |
| `DOUBAO_AGENT_SPEND_BUDGET` | 单次异常允许的付费恢复动作数 | `1` |
| `DOUBAO_AGENT_MAX_ACTIONS` | 单次异常最多工具动作数 | `12` |
| `DOUBAO_AGENT_MAX_ACTION_ATTEMPTS` | 同一工具最多重复次数 | `3` |
| `DOUBAO_AGENT_MAX_CONCURRENCY` | 同时运行的异常恢复数 | `2` |
| `DOUBAO_AGENT_DEDUPE_SECONDS` | 同一任务重复通知去重窗口 | `300` |
| `DOUBAO_RECOVERY_BRIDGE_URL` | CLI/MCP 回调管理台的本地地址 | `http://127.0.0.1:8765` |

`DOUBAO_*` 配置可从进程环境或项目 `.claude/settings.local.json` 的 `env`
读取，进程环境优先。Claude 的登录、模型和 provider 配置只由 Claude CLI
自身管理；服务不会覆盖 `ANTHROPIC_*`。

PowerShell 示例：

```powershell
$env:DOUBAO_CLAUDE_CLI_PATH = "$env:APPDATA\npm\claude.cmd"
$env:DOUBAO_AGENT_CLI_TIMEOUT_SECONDS = "900"
$env:DOUBAO_AGENT_SPEND_BUDGET = "1"
```

以上均为可选覆盖项。正常情况下只需本机已安装并登录 Claude Code CLI；
启动管理台后会自动以 `bypassPermissions` 运行异常恢复，不会覆盖 CLI 的
自有登录，也不会继承 `ANTHROPIC_*` 模型网关配置。

## 执行流程

1. 原 worker/recall/推送异常先按既有逻辑更新任务状态。
2. `ExceptionRecoveryService` 为该任务分配账号恢复租约。
3. 服务生成一次性工具 token，写入临时 MCP 配置。
4. 启动独立 `claude -p --output-format json` 进程，传入任务 ID、异常、
   阶段、会话 URL 和最近业务事件。
5. Claude 通过受控 MCP 工具多轮检查页面、处理 DOM/弹窗、召回和下载结果、
   修复任务或重推 H3。
6. 每次工具调用都回到现有 `RecoveryBackend`，由策略层检查账号、
   提交状态、额度、归档结果和动作预算。
7. CLI 结束后服务重新读取任务状态。只有任务已进入正常队列、生成、下载或
   归档状态时才记录恢复成功；否则转人工待办并写明原因。

前端任务列表会实时显示 `Claude 处理中`、当前恢复工具和核对阶段。恢复结束后
显示 `Claude 已转人工` 或错误原因；处理中会禁用可能抢占浏览器、重复提交、
下载或推送的操作，但“打开窗口”仍可用于人工观察。

## 安全边界

- CLI 只加载 `doubao_recovery` MCP server，内置 Bash、文件读写、编辑、
  Web 搜索等工具全部禁用。
- MCP server 只能使用启动时注入的单个 `job_id` 和一次性 token。
- 临时 token 不写入长期日志；内部 HTTP 工具桥校验 token 和任务绑定。
- 登录、验证码、账号额度不足和账号被其他任务占用时不自动操作。
- 提交/扣费不确定时禁止 `restart_auto` 和 `regenerate`，必须先核对历史结果。
- 已有归档视频时禁止覆盖、删除或重新生成。
- 付费动作受 `DOUBAO_AGENT_SPEND_BUDGET` 限制。
- Claude 不能编辑生产源码或写入任意文件。

## 恢复工具

| 工具 | 作用 |
| --- | --- |
| `restart_browser` | 重启该账号的受管浏览器上下文 |
| `switch_page` | 切到任务唯一绑定的会话标签 |
| `refresh_page` | 刷新任务页 |
| `inspect_dom` | 读取 DOM、控件、上传和网络错误，不使用截图 |
| `dismiss_popup` | 仅关闭有明确关闭控件的普通弹窗 |
| `locate_task_page` | 恢复任务会话 URL/ID 和标签页绑定 |
| `verify_task_status` | 核对数据库与页面状态 |
| `check_assets` | 检查任务引用素材是否存在 |
| `recall_existing_result` | 按任务卡片绑定召回已有结果 |
| `download_video` | 下载已生成的绑定视频 |
| `retry_h3_push` | 重推已有归档视频到 H3 |
| `restart_auto` | 对已证明未提交的任务重新准备和提交 |
| `regenerate` | 在预算内创建新任务；原任务保留 |
| `release_account` | 释放当前任务持有的残留机器 lane |

## Windows 进程管理

- 自动检测 `claude.exe`、`claude.cmd` 和 npm 全局目录。
- `.cmd` launcher 通过 `cmd.exe /d /s /c` 启动，避免 `WinError 193`。
- 子进程使用无窗口标志并在独立进程组内运行。
- 超时、取消和服务关闭时使用 `taskkill /PID ... /T /F` 清理完整进程树。
- `data/agent_recovery/processes.jsonl` 记录进程 PID、任务 ID 和一次性 marker；
  下次启动只终止命令行中仍含该 marker 的残留进程，避免误杀复用 PID 的其他程序。

## 日志

```text
data/agent_recovery/exceptions.jsonl
data/agent_recovery/processes.jsonl
data/agent_recovery/cli-<job>-<run>.log
```

`exceptions.jsonl` 包含任务、异常、CLI 路径/退出码/超时、完整有序工具账本、
额度预算、恢复结果和人工原因。CLI 原始输出只写入每次运行独立日志，并对常见
凭据格式脱敏。

## 服务重启

- 启动时清理上次运行遗留的 Claude 进程树。
- 既有启动恢复逻辑重新扫描可恢复的 `needs_review` 任务并重新启动独立 CLI。
- 任务、会话 URL、session ID、视频卡片序号和额度状态仍由数据库持久化。
- 已完成、已取消或进入人工待办的任务不会被清除或重新生成。

## 测试

```powershell
uv run python -m pytest tests/test_agent_recovery.py tests/test_agent_recovery_cli.py -q
```
