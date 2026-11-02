from __future__ import annotations

import os
import re

VIDEO_PAGE = "https://www.doubao.com/"
TRUSTED_CDNS = ("douyinvod.com", "doubao.com", "byteimg.com", "volces.com", "volccdn.com", "bytecdn.cn")
ACTIVE = ("opening", "configuring", "uploading", "submitting", "generating", "confirming", "downloading")
# 人工发送模式：页面已经填好提示词和素材，但发送按钮由操作者在豆包网页亲自点击。
# 它不持有账号锁（worker 已结束），但仍占用处理队列名额；服务重启时单独转为需人工复查。
MANUAL_WAIT = "awaiting_manual_send"
# 操作者在管理台明确判定失败：与自动检测到的 failed 区分开，H3 收到该错误码后不再把
# manual 任务当成“还能重新发送”而继续等待。
MANUAL_FAILED = "MANUAL_CONFIRMED_FAILED"

# Doubao web drives video generation as a chat conversation. Observed on 2026-10-28:
# the assistant echoes the parsed parameters and waits for the user to confirm before
# any quota is spent, the finished result is a cover-image card (no <video> in the DOM
# until it is opened), and the player then mounts a signed https://v*.vdl.doubao.com URL.
ASSISTANT_MESSAGE = '[data-testid="receive_message"], [data-testid="message_content"]'
VIDEO_CARD = '[class*="block-video"], [data-testid="video-result-card"]'
SETTINGS_PANEL = '[data-input-engine-actionbar-render-entry-key="video-generation-params-panel"]'
SETTINGS_TEXT = re.compile(r"(?:自动|16:9|9:16|1:1).*?\d+\s*(?:s|秒)", re.I)
HOME_MODE_SWITCH = '[data-testid="conversation-mode-switch"]'
WORK_HOME_TEXT = re.compile(r"今天有什么工作要处理|下载豆包工作电脑版")
MENTION_RE = re.compile(r"@图(\d+)")
# 点开结果卡片后，右侧详情面板的“保存”导出的才是无水分片（源地址带 lr=unwatermarked）；
# 聊天卡片里的 <video> 是 lr=video_gen_watermark_dyn 的带水印播放流，不能直接归档。
DETAIL_PANEL = ('[data-testid="samantha_layout_right_side"]',
                '[data-testid="canvas_panel_container"]')
DETAIL_SAVE = ('[data-testid="edit_image_download_button"]',
               'button:has-text("保存")',
               '[role="button"]:has-text("保存")')
CLEAN_MEDIA_MARK = "lr=unwatermarked"
# Sidebar rows carry this marker for ANY conversation, so it cannot gate this job -
# failure is read from the trailing assistant message instead.
SIDEBAR_FAILED = '[data-testid="conversation-list-v2-status-failed"]'
EDITOR_SELECTORS = ['[data-testid="chat_input_input"] [contenteditable="true"]',
                    '[contenteditable="true"][class*="editor"]',
                    '[role="textbox"][contenteditable="true"]',
                    'div[contenteditable="true"]', 'textarea']
# NOTE: [data-testid="send_message"] is the *sent message row*, not a control - clicking it
# does nothing, and once the conversation has any message it would shadow the real button.
SUBMIT_SELECTORS = ['[data-testid="chat_input"] [data-testid="chat_input_send_button"]',
                    '[data-testid="chat_input"] button[aria-label*="发送"]',
                    '[data-testid="chat_input"] button[type="submit"]',
                    '[data-testid="chat_input_send_button"]',
                    'button[type="submit"]', 'button[aria-label*="生成"]', 'button[aria-label*="发送"]']
CONFIRM_ASK = r"视频生成参数确认|确认后.{0,16}开始生成|是否.{0,10}开始生成|请确认.{0,12}参数"
GENERATION_STARTED = r"本次使用.{0,40}生成|预计等待|视频正在生成|正在为你生成|生成好后"
CONFIRM_LABELS = ("现在生成", "确认生成", "直接生成", "确认")
CONFIRM_ANSWER = ("以上严格执行我的要求，不许乱换模型，好好给我出视频，"
                  "不然就投诉你，请使用 Seedance 2.0 模型")
MODES = {"text2video": "文生视频", "image2video": "首帧图生视频", "first_last_frame": "首尾帧生视频", "reference_images": "参考生视频"}
VIDEO_MODELS = ("Seedance 2.5", "Seedance 2.0", "Seedance 2.0 Fast", "Seedance 2.0 Mini")
MEMBER_MODEL = "Seedance 2.0"
NON_MEMBER_MODEL = "Seedance 2.0 Mini"
MAX_IMAGE = 15 * 1024 * 1024
MAX_VIDEO = 600 * 1024 * 1024
# 每个账号每天的视频生成额度（秒）。按本地日期归零；可用环境变量覆盖，便于灰度或调整额度。
DAILY_VIDEO_SECONDS = int(os.getenv("DOUBAO_DAILY_VIDEO_SECONDS", "30") or "30")
TERMINAL = ("downloaded", "needs_review", "failed")
REAL_CHAT_URL = re.compile(r"/chat/(?!local_)[0-9A-Za-z_-]+(?:[/?#]|$)")
CHAT_PAGE_URL = re.compile(r"/chat/[0-9A-Za-z_-]+(?:[/?#]|$)")
