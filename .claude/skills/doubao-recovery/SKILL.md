---
name: doubao-recovery
description: Doubao video job recovery knowledge for task-page binding, result recall, download, H3 retry, and account-safe recovery.
---

# 豆包视频业务知识

## 任务阶段

- `opening`：启动账号浏览器并打开豆包。
- `configuring`：选择视频模式、模型、比例、时长。
- `uploading`：上传并校验图片素材。
- `submitting`：点击发送任务提示词。进入此阶段后，提交是否成功都可能不确定。
- `confirming`：等待或发送参数确认语。确认语最多只能发送一次。
- `generating`：等待豆包生成视频。
- `downloading`：打开详情面板并保存无水印成片。
- `needs_review`：原业务已转人工复核。
- `downloaded`：视频已归档并进入 H3 交付流程。
- `failed`：原业务已明确失败。

## 定位与持久化

任务的 `session_url`、`session_id` 和 `result_card_index` 是唯一会话与视频卡片绑定。
历史任务会由数据库迁移补全可识别的会话 ID。服务重启后必须使用这些持久化字段恢复定位。
无法确认卡片序号时只能查历史结果，不能打开会话里的最后一个视频或猜测其他分片。

## 工具选择

1. 浏览器或页面异常：`locate_task_page` / `switch_page` / `refresh_page`；
   浏览器环境不可用时 `restart_browser`。
2. 页面元素漂移或弹窗：`inspect_dom` / `dismiss_popup`，再重新读取 DOM。
3. 结果不确定：`verify_task_status`；已有结果则 `recall_existing_result` 或
   `download_video`。
4. 仅 H3 推送失败：`retry_h3_push`。
5. 可证明未提交且额度未消耗：`restart_auto`；仍在预算内且需要保留原任务时才用
   `regenerate`。
6. 账号 lane 残留且确认属于当前任务：`release_account`。它不取消任务、不改状态。

## 验收标准

- 恢复成功必须有真实状态或文件结果验证，不能只凭“命令已发送”判断。
- 页面重启、刷新、切换、关弹窗等中间动作即使成功，也不等于任务恢复完成。
- 恢复动作完成后继续原业务流程；无需人工时保持 `needs_human=false`。
- 无法恢复时，`manual_reason` 必须说明具体失败证据以及人工下一步。
