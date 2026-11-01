# 豆包视频业务知识

## 任务阶段

- `opening`：启动浏览器并打开豆包。
- `configuring`：选择视频模式、模型、比例、时长。
- `uploading`：上传并核验图片素材。
- `submitting`：点击发送任务提示词。此阶段之后任何不确定状态都禁止重发。
- `confirming`：等待或发送参数确认语。确认语只能发送一次。
- `generating`：等待豆包生成视频。
- `downloading`：打开详情面板并保存无水印成片。
- `needs_review`：原业务已转人工复查。
- `failed`：原业务已明确失败。

## 常见异常码

- `LOGIN_REQUIRED`：登录失效，必须人工登录。
- `VERIFICATION_REQUIRED`：验证码或安全验证，必须人工完成。
- `ACCOUNT_LIMIT`：额度不足，必须人工处理，禁止换号或绕过。
- `SETTING_NOT_FOUND` / `SETTING_UNSUPPORTED` / `SETTING_UNVERIFIED`：
  页面参数控件不可用或无法验证。
- `MODEL_TRIGGER_NOT_FOUND` / `MODEL_NOT_FOUND`：模型选择失败。
- `UPLOAD_SLOT_NOT_FOUND` / `UPLOAD_UNVERIFIED` / `UPLOAD_COUNT_MISMATCH`：
  素材上传失败或未确认。
- `EDITOR_NOT_FOUND` / `PROMPT_UNVERIFIED`：提示词输入失败。
- `SUBMIT_NOT_FOUND`：提交按钮不可用。
- `SUBMISSION_UNCONFIRMED`：是否已提交不确定。
- `CONFIRM_NOT_SENT` / `CONFIRM_REQUIRED`：确认状态不确定或仍待确认。
- `WAIT_TIMEOUT` / `RESULT_NOT_READY`：原会话结果等待超时或尚未出现。
- `RESULT_URL_NOT_FOUND` / `DETAIL_SAVE_NOT_FOUND`：找不到可下载结果。
- `RESULT_BINDING_MISSING` / `DURATION_MISMATCH`：结果归属不确定。
- `RECALL_EXCEPTION`：召回流程异常。
- `RESULT_PUSH_FAILED`：视频已归档但 H3 回推失败。
- `WORKER_EXCEPTION` / `WEB_AUTOMATION_ERROR`：浏览器、页面加载或自动化异常。

## 恢复工具（代理可自行调用）

- `restart_browser`：通过现有账号浏览器管理功能重新启动浏览器。
- `refresh_page`：刷新当前任务页。
- `recall_existing_result`：按原任务绑定关系下载已有结果，不重新生成。
- `retry_h3_push`：重推已有归档视频到 H3。
- `restart_auto`：就地把当前任务重新自动执行（保持原任务 ID）。
- `regenerate`：从原任务复制出一个新任务并排队重跑（原任务保留）。

## 怎么用这些工具

1. 浏览器 / 页面明显异常：先 `restart_browser` 或 `refresh_page`。
2. 可能已经出片：优先 `recall_existing_result`，不要重新生成。
3. 视频已归档只是回推失败：只用 `retry_h3_push`。
4. 任务在提交前就失败（`opening` / `configuring` / `uploading` / `submitting`
   之前，且额度未被消耗）：可以用 `restart_auto`；仍不行再用 `regenerate`。
5. 一个动作失败可以继续尝试下一个，但不要重复尝试同一个动作。

## 硬边界（代码层面强制，代理无法绕过）

- 登录失效、验证码、安全验证：必须人工，工具不会提供。
- 账号额度不足：必须人工，禁止换号或绕过限制。
- `quota_state=unknown`、或任务已经进入 `confirming` / `generating` /
  `downloading`，或错误码表明“可能已提交/已扣费”：禁止 `restart_auto`
  与 `regenerate`，避免为一次已花钱的生成再埋单。
- 已有归档视频时禁止召回覆盖或重新生成覆盖。

以上动作都不修改提示词/素材、不删除或覆盖已有视频、不切换账号。
