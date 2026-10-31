"""Authorized, UI-only Doubao web adapter. No cookie export, hidden APIs or account cycling.

Fail-closed: unknown UI, unverified settings/uploads/submission, expired session => needs_review.
Tests use an explicitly enabled local test page; this DOES NOT certify the live Doubao website.
"""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import os
import random
import re
import sqlite3
import struct
import uuid
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Literal, Optional
from urllib.parse import urlparse, urljoin
from fastapi import File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from PIL import Image, UnidentifiedImageError

VIDEO_PAGE = "https://www.doubao.com/"
TRUSTED_CDNS = ("douyinvod.com", "doubao.com", "byteimg.com", "volces.com", "volccdn.com", "bytecdn.cn")
ACTIVE = ("opening", "configuring", "uploading", "submitting", "generating", "confirming", "downloading")
# 人工发送模式：页面已经填好提示词和素材，但发送按钮由操作者在豆包网页亲自点击。
# 它不持有账号锁（worker 已结束），也不属于 ACTIVE；服务重启时单独转为需人工复查。
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
MEMBER_MODEL = "Seedance 2.0"
MAX_IMAGE = 15 * 1024 * 1024
MAX_VIDEO = 600 * 1024 * 1024
# 每个账号每天的视频生成额度（秒）。按本地日期归零；可用环境变量覆盖，便于灰度或调整额度。
DAILY_VIDEO_SECONDS = int(os.getenv("DOUBAO_DAILY_VIDEO_SECONDS", "30") or "30")
TERMINAL = ("downloaded", "needs_review", "failed")


def ts(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def safe(s): return re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", s).strip("._")[:64] or "none"


async def automatic_step_delay(submit_mode: str = "auto") -> int:
    """Space automatic workflow stages by a human-like random 5–10 seconds."""
    if submit_mode != "auto" or os.getenv("DOUBAO_TEST_MODE") == "1":
        return 0
    seconds = random.randint(5, 10)
    await asyncio.sleep(seconds)
    return seconds


def account_value(account, key: str, default=None):
    return account[key] if key in account.keys() else default


def enforce_account_model(req: "VideoRequest", account) -> None:
    if bool(account_value(account, "is_member", 0)):
        req.model = MEMBER_MODEL


def local_day() -> str:
    """本地日历日，作为额度归零边界（每天 30 秒按本地自然日计算）。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def quota_summary(c: sqlite3.Connection, account_id: str, day: str | None = None) -> dict:
    """读取某账号某天的额度记录：已消耗 / 排队占用 / 剩余 / 上限。"""
    day = day or local_day()
    row = c.execute("SELECT reserved_seconds, consumed_seconds FROM account_quota WHERE account_id=? AND day=?",
                    (account_id, day)).fetchone()
    reserved = row["reserved_seconds"] if row else 0
    consumed = row["consumed_seconds"] if row else 0
    unknown = 0
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_web_jobs'").fetchone():
        unknown_row = c.execute(
            """SELECT COALESCE(SUM(w.duration), 0) AS seconds
               FROM video_web_jobs w JOIN jobs j ON j.id=w.job_id
               WHERE j.account_id=? AND w.quota_day=? AND w.quota_state='unknown'""",
            (account_id, day)).fetchone()
        unknown = int(unknown_row["seconds"] or 0) if unknown_row else 0
    return {"account_id": account_id, "day": day,
            "daily_limit_seconds": DAILY_VIDEO_SECONDS,
            "consumed_seconds": consumed,
            "reserved_seconds": reserved,
            "unknown_seconds": unknown,
            "queued_reserved_seconds": max(0, reserved - unknown),
            "remaining_seconds": max(0, DAILY_VIDEO_SECONDS - consumed - reserved)}


def reserve_quota(c: sqlite3.Connection, account_id: str, seconds: int, day: str | None = None) -> None:
    """任务创建时占用额度，避免同一账号被排队任务超额分配。"""
    if not account_id or seconds <= 0:
        return
    day = day or local_day()
    c.execute("""INSERT INTO account_quota(account_id, day, reserved_seconds, consumed_seconds)
                 VALUES(?,?,?,0)
                 ON CONFLICT(account_id, day) DO UPDATE SET reserved_seconds = reserved_seconds + excluded.reserved_seconds""",
              (account_id, day, seconds))


def quota_held_today(w) -> bool:
    """该任务今天是否已占用额度。

    新任务创建时 quota_state='reserved'；早期版本的任务 state 为空但 quota_day
    已写入且已调用 reserve_quota，同样视为已占用，避免重复占用。
    """
    state = (w["quota_state"] or "") if w else ""
    return (w["quota_day"] or "") == local_day() and state in ("", "reserved", "unknown", "consumed")



def settle_quota(c: sqlite3.Connection, jobid: str, new_status: str,
                 prior_status: str = "", error_code: str = "") -> None:
    """任务进入终态时结算额度：
    - downloaded：把排队占用转成已消耗（确实出了片、确实花了额度）。
    - needs_review：若任务可能已提交/确认，则转为 unknown 并继续占用额度；明确未提交才释放。
    - failed：网页已明确失败，释放排队占用。
    人工复查（reconcile / manual-result）会把 needs_review 重新推到 downloaded，此时再转为已消耗。
    """
    if new_status not in TERMINAL:
        return
    w = c.execute("SELECT duration, quota_day, quota_state FROM video_web_jobs WHERE job_id=?", (jobid,)).fetchone()
    if not w:
        return
    dur = w["duration"] or 0
    j = c.execute("SELECT account_id FROM jobs WHERE id=?", (jobid,)).fetchone()
    account_id = j["account_id"] if j else None
    if not account_id or dur <= 0:
        return
    day = w["quota_day"] or local_day()
    c.execute("INSERT OR IGNORE INTO account_quota(account_id, day, reserved_seconds, consumed_seconds) VALUES(?,?,0,0)",
              (account_id, day))
    state = w["quota_state"] or ""
    if new_status == "downloaded":
        if state != "consumed":
            if state == "released":
                c.execute("UPDATE account_quota SET consumed_seconds = consumed_seconds + ? WHERE account_id=? AND day=?",
                          (dur, account_id, day))
            else:
                c.execute("""UPDATE account_quota
                             SET reserved_seconds = MAX(0, reserved_seconds - ?),
                                 consumed_seconds = consumed_seconds + ?
                             WHERE account_id=? AND day=?""", (dur, dur, account_id, day))
            c.execute("UPDATE video_web_jobs SET quota_state='consumed' WHERE job_id=?", (jobid,))
    elif new_status == "needs_review":
        definitely_unspent = error_code in {
            "LOGIN_REQUIRED", "VERIFICATION_REQUIRED", "ACCOUNT_LIMIT", "SETTING_NOT_FOUND",
            "SETTING_UNSUPPORTED", "SETTING_UNVERIFIED", "MODEL_NOT_FOUND", "MODEL_UNVERIFIED",
            "UPLOAD_SLOT_NOT_FOUND", "UPLOAD_UNVERIFIED", "EDITOR_NOT_FOUND", "PROMPT_UNVERIFIED",
            "SUBMIT_NOT_FOUND", "CONFIRM_NOT_SENT", "CONFIRM_REQUIRED",
        }
        # 人工发送阶段重启时无法知道操作者是否已经在网页点过发送，按“可能已消耗”保留占用，
        # 等关联下载/人工归档成功后再转为已消耗。
        known_maybe_spent = error_code == "MANUAL_SEND_INTERRUPTED"
        uncertain = (not definitely_unspent and
                     (prior_status in ("submitting", "confirming", "generating", "downloading", MANUAL_WAIT) or
                      error_code in {"SUBMISSION_UNCONFIRMED", "WAIT_TIMEOUT",
                                     "INTERRUPTED_REVIEW_REQUIRED", "WORKER_EXCEPTION",
                                     "WEB_AUTOMATION_ERROR", "RESULT_URL_NOT_FOUND", "RESULT_NOT_READY"}) or
                     known_maybe_spent)
        if uncertain and state in ("", "reserved"):
            c.execute("UPDATE video_web_jobs SET quota_state='unknown' WHERE job_id=?", (jobid,))
        elif state in ("", "reserved", "unknown"):
            c.execute("UPDATE account_quota SET reserved_seconds = MAX(0, reserved_seconds - ?) WHERE account_id=? AND day=?",
                      (dur, account_id, day))
            c.execute("UPDATE video_web_jobs SET quota_state='released' WHERE job_id=?", (jobid,))
    elif new_status == "failed" and state in ("", "reserved", "unknown"):
        c.execute("UPDATE account_quota SET reserved_seconds = MAX(0, reserved_seconds - ?) WHERE account_id=? AND day=?",
                  (dur, account_id, day))
        c.execute("UPDATE video_web_jobs SET quota_state='released' WHERE job_id=?", (jobid,))


def local_day_from_iso(s: str) -> str:
    """把任务的 created_at（带时区 ISO）转成本地日历日，用于历史额度回填。"""
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return local_day()


def backfill_existing_quota(c: sqlite3.Connection) -> None:
    """一次性回填历史任务：downloaded 视为当天已消耗；旧 queued/进行中任务不回溯占用。幂等。"""
    rows = c.execute("""SELECT j.account_id, j.created_at, w.job_id, w.duration, w.quota_state
                        FROM jobs j JOIN video_web_jobs w ON j.id = w.job_id
                        WHERE j.status = 'downloaded' AND w.quota_state = ''""").fetchall()
    for r in rows:
        if not r["account_id"] or (r["duration"] or 0) <= 0:
            continue
        day = local_day_from_iso(r["created_at"])
        c.execute("INSERT OR IGNORE INTO account_quota(account_id, day, reserved_seconds, consumed_seconds) VALUES(?,?,0,0)",
                  (r["account_id"], day))
        c.execute("UPDATE account_quota SET consumed_seconds = consumed_seconds + ? WHERE account_id=? AND day=?",
                  (r["duration"], r["account_id"], day))
        c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='consumed' WHERE job_id=?",
                  (day, r["job_id"]))


class VideoRequest(BaseModel):
    account_id: Optional[str] = Field(default=None)  # 留空则后端自动选择一个可用账号
    project_id: str = Field(default="", max_length=120)
    episode_id: str = Field(default="", max_length=120)
    shot_id: str = Field(default="", max_length=120)
    task_name: str = Field(default="", max_length=160)
    prompt: str = Field(min_length=1, max_length=10000)
    mode: Literal["text2video", "image2video", "first_last_frame", "reference_images"] = "text2video"
    # 时长/画幅以豆包网页实际提供的选项为准，适配器不硬编码具体取值；仅做宽松的合理性约束。
    duration: int = Field(default=4, ge=1, le=60)
    ratio: str = Field(default="16:9", min_length=1, max_length=16)
    model: Optional[str] = Field(default=None, max_length=64)
    first_frame_asset_id: Optional[str] = None
    last_frame_asset_id: Optional[str] = None
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=9)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)
    result_push_url: Optional[str] = Field(default=None, max_length=2048)
    result_push_token: Optional[str] = Field(default=None, min_length=16, max_length=256)
    auto_start: bool = True
    # auto   = 当前全自动：脚本点击发送并自动确认。
    # manual = 只配置网页、上传素材、填入提示词，停在输入框里等操作者亲自发送；
    #          生成完成后由操作者点“关联下载”再归档。manual 任务创建时始终先排队，
    #          必须由本机操作者显式启动，避免 H3 调度器替人按下发送键。
    submit_mode: Optional[Literal["auto", "manual"]] = None

    @model_validator(mode="after")
    def modes_match_assets(self):
        first, last, refs = self.first_frame_asset_id, self.last_frame_asset_id, self.reference_asset_ids
        if self.mode == "text2video" and (first or last or refs):
            raise ValueError("文生视频不能附带图片素材")
        if self.mode == "image2video" and (not first or last or refs):
            raise ValueError("首帧图生视频需要且只允许 first_frame_asset_id")
        if self.mode == "first_last_frame" and (not first or not last or refs):
            raise ValueError("首尾帧模式必须提供两个素材 ID")
        if self.mode == "reference_images" and (not refs or first or last or len(refs) != len(set(refs))):
            raise ValueError("参考图模式需要 1～9 个不重复的素材 ID")
        return self


class ConfirmationPresetInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def trim_values(self):
        self.name = self.name.strip()
        self.text = self.text.strip()
        if not self.name or not self.text:
            raise ValueError("预设名称和确认语不能为空")
        return self


class SubmitModeSetting(BaseModel):
    submit_mode: Literal["auto", "manual"]


class ProcessingLimitSetting(BaseModel):
    processing_limit: int = Field(ge=0)


class ConfirmationSendRequest(BaseModel):
    preset_id: str = Field(min_length=1, max_length=80)


class BindPageRequest(BaseModel):
    page_url: str = Field(min_length=1, max_length=2048)


class ReviewNeeded(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def is_safe_media(u: str) -> bool:
    p = urlparse(u)
    host = (p.hostname or "").lower()
    if p.scheme == "https" and any(host == d or host.endswith("." + d) for d in TRUSTED_CDNS):
        return True
    # Only used in local, explicit e2e tests. NEVER enable on an exposed production server.
    return (os.getenv("DOUBAO_TEST_MODE") == "1" and p.scheme == "http" and
            host in ("127.0.0.1", "localhost") and p.port is not None)


def target_page_url():
    if os.getenv("DOUBAO_TEST_MODE") == "1":
        test = os.getenv("DOUBAO_TEST_PAGE", "")
        p = urlparse(test)
        if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1") and p.port:
            return test
    return VIDEO_PAGE


async def first_visible(page, selectors):
    for s in selectors:
        try:
            loc = page.locator(s)
            for i in range(min(await loc.count(), 8)):
                el = loc.nth(i)
                if await el.is_visible(): return el
        except Exception: continue
    return None


def select_candidates(kind):
    # Accessible selectors preferred; class hashes are *not* considered stable.
    if kind == "mode":
        return ['[data-testid="video-mode"]', '[data-testid="generation-mode"]',
                'select[name="mode"]', '[role="combobox"][aria-label*="模式"]',
                'button[aria-label*="生成模式"]']
    if kind == "ratio":
        return ['[data-testid="video-ratio"]', 'select[name="ratio"]',
                '[role="combobox"][aria-label*="比例"]', 'button[aria-label*="画幅"]',
                'button[aria-label*="比例"]']
    return ['[data-testid="video-duration"]', 'select[name="duration"]',
            '[role="combobox"][aria-label*="时长"]', 'button[aria-label*="时长"]']


SETTINGS_PANEL = '[data-input-engine-actionbar-render-entry-key="video-generation-params-panel"]'
SETTINGS_TEXT = re.compile(r"(?:自动|16:9|9:16|1:1).*?\d+\s*(?:s|秒)", re.I)

HOME_MODE_SWITCH = '[data-testid="conversation-mode-switch"]'
WORK_HOME_TEXT = re.compile(r"今天有什么工作要处理|下载豆包工作电脑版")


async def home_mode(page):
    """读取首页「对话/工作」分段控件的选中项；老版 UI 没有该控件时返回 None。

    选中按钮没有 aria-selected，只有当前项带精确的 ``text-dbx-text-primary`` token
    （未选中项只有 ``hover:text-dbx-text-primary``），所以用 classList 判断而不是
    字符串包含。
    """
    try:
        return await page.evaluate(r"""() => {
          const sw = document.querySelector('[data-testid="conversation-mode-switch"]');
          if (!sw) return null;
          const selected = Array.from(sw.querySelectorAll('button')).find(b =>
            b.classList.contains('text-dbx-text-primary') ||
            b.getAttribute('aria-selected') === 'true' ||
            b.getAttribute('aria-checked') === 'true');
          return selected ? (selected.innerText || '').trim() : null;
        }""")
    except Exception:
        return None


async def work_mode_active(page):
    mode = await home_mode(page)
    if mode is not None:
        return mode == "工作"
    # 分段控件读不出来时退回文案判断，避免页面结构变化后静默留在工作模式。
    try:
        text = await page.evaluate(
            "() => document.querySelector('[data-testid=\"flow_chat_guidance_page\"]')?.innerText || ''")
    except Exception:
        return False
    return bool(WORK_HOME_TEXT.search(text or ""))


async def click_home_mode(page, label):
    switch = await first_visible(page, [HOME_MODE_SWITCH])
    if switch is None:
        return False
    try:
        buttons = switch.get_by_text(label, exact=True)
        for i in range(min(await buttons.count(), 6)):
            button = buttons.nth(i)
            if await button.is_visible():
                await button.click()
                return True
    except Exception:
        pass
    return False


async def dismiss_blocking_dialogs(page) -> int:
    """Close nuisance dialogs only when they expose an explicit close control.

    Login, verification, generation-confirmation and video-detail dialogs are business
    state and must remain visible for the normal automation or a human operator.
    """
    try:
        closed = await page.evaluate(r"""() => {
          const visible = el => {
            if (!(el instanceof HTMLElement)) return false;
            const s = getComputedStyle(el), r = el.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden' &&
                   Number(s.opacity || 1) > 0 && r.width > 4 && r.height > 4;
          };
          const protectedText = /人机验证|安全验证|验证码|captcha|扫码登录|登录后|确认.*生成|生成.*确认|消耗.*(?:积分|额度)|视频详情|保存到本地|下载到本地/i;
          const closeSelector = [
            'button[aria-label*="关闭"]','[role="button"][aria-label*="关闭"]',
            'button[aria-label*="close" i]','[role="button"][aria-label*="close" i]',
            'button[title*="关闭"]','button[title*="close" i]',
            '[data-testid*="close" i]','[class*="modal-close" i]',
            '[class*="dialog-close" i]','[class*="popup-close" i]'
          ].join(',');
          const roots = Array.from(document.querySelectorAll(
            '[role="dialog"],[aria-modal="true"],[data-testid*="modal" i],'+
            '[data-testid*="popup" i],[class*="Modal"],[class*="modal"],'+
            '[class*="Dialog"],[class*="dialog"],[class*="Popup"],[class*="popup"]'
          )).filter(visible);
          for (const el of document.querySelectorAll('body *')) {
            if (!visible(el) || getComputedStyle(el).position !== 'fixed') continue;
            if (el.querySelector(closeSelector)) roots.push(el);
          }
          const clicked = new Set();
          let count = 0;
          for (const root of [...new Set(roots)]) {
            if (!visible(root) || root.querySelector('video')) continue;
            const text = (root.innerText || '').replace(/\s+/g, ' ').trim();
            if (protectedText.test(text)) continue;
            const candidates = Array.from(root.querySelectorAll(`${closeSelector},button,[role="button"]`));
            const close = candidates.find(el => {
              if (!visible(el) || clicked.has(el)) return false;
              const label = `${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} `+
                            `${el.getAttribute('data-testid') || ''} ${el.className || ''}`;
              const buttonText = (el.innerText || el.textContent || '').trim();
              return /关闭|close|dismiss/i.test(label) || /^(?:×|✕|✖|关闭)$/i.test(buttonText);
            });
            if (!close) continue;
            clicked.add(close);
            close.click();
            count += 1;
          }
          return count;
        }""")
    except Exception:
        return 0
    if closed:
        await page.wait_for_timeout(250)
    return int(closed or 0)


async def ensure_chat_mode(page):
    """视频技能只在「对话」首页存在，配置前必须确认并切回。

    「工作」首页仍然挂载 ``[data-testid=chat_input]``，所以等输入框出现并不能证明
    能配置视频；继续下去会在 enter_video_mode 里耗掉 30 秒重试后以
    SETTING_NOT_FOUND 失败。优先点分段控件「对话」，控件不可用时退回侧栏「新对话」
    （两条路径都已在真实页面验证），切完再确认生效。
    """
    if not await work_mode_active(page):
        return
    if not await click_home_mode(page, "对话"):
        new_chat = await first_visible(page, ['[data-testid="create_conversation_button"]'])
        if new_chat is None:
            raise ReviewNeeded("WORK_MODE_ACTIVE", "豆包停在「工作」模式，找不到「对话」入口；未提交任务")
        await new_chat.click()
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        if not await work_mode_active(page):
            return
        await page.wait_for_timeout(300)
    raise ReviewNeeded("WORK_MODE_ACTIVE", "豆包停在「工作」模式，切回「对话」未生效；未提交任务")


async def combined_video_settings(page):
    """Find the current composer control such as "自动 · 10s"."""
    stable = await first_visible(page, [SETTINGS_PANEL])
    if stable is not None:
        return stable
    controls = page.locator('button,[role="button"]')
    for i in range(min(await controls.count(), 80)):
        control = controls.nth(i)
        try:
            if not await control.is_visible():
                continue
            text = " ".join(filter(None, [await control.inner_text(),
                                           await control.get_attribute("aria-label"),
                                           await control.get_attribute("title")]))
            if SETTINGS_TEXT.search(text):
                return control
        except Exception:
            continue
    labels = page.get_by_text(SETTINGS_TEXT)
    for i in range(min(await labels.count(), 20)):
        try:
            if await labels.nth(i).is_visible():
                return labels.nth(i)
        except Exception:
            continue
    return None


async def enter_video_mode(page):
    """Activate the video skill in either composer variant.

    Some accounts expose a dedicated ``skill_bar_button_17`` chip; others keep
    the same skill behind the "更多技能" popover (``skill_bar_button_more`` ->
    ``skill-panel-v2``). Video mode is only proven by the parameters panel —
    a visible "视频生成" menu item is not enough.
    """
    launch = await first_visible(page, ['[data-testid="skill_bar_button_17"]'])
    if launch is not None:
        await launch.click()
    else:
        more = await first_visible(page, ['[data-testid="skill_bar_button_more"]'])
        if more is None:
            return False
        await more.click()
        try:
            await page.wait_for_selector('[data-testid="skill-panel-v2"]', state="visible", timeout=5000)
        except Exception:
            pass
        panel = await first_visible(page, ['[data-testid="skill-panel-v2"]'])
        if panel is None:
            return False
        item = panel.get_by_text("视频生成", exact=True)
        clicked = False
        for i in range(min(await item.count(), 8)):
            try:
                if await item.nth(i).is_visible():
                    await item.nth(i).click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return False
    for _ in range(20):
        if await combined_video_settings(page) is not None:
            return True
        await page.wait_for_timeout(300)
    return False


async def set_ui_option(page, kind, value):
    """Select and verify an explicit UI value. Never treat prompt text as a substitute."""
    await dismiss_blocking_dialogs(page)
    elt = await first_visible(page, select_candidates(kind))
    if elt is None and kind == "mode":
        # The video skill sits either on a dedicated chip or inside the "更多技能"
        # popover depending on the account's UI rollout. Image modes are then
        # determined by the explicit upload slot used below.
        if await combined_video_settings(page) is not None:
            return
        deadline = asyncio.get_running_loop().time() + 30
        attempts = 0
        while asyncio.get_running_loop().time() < deadline:
            if await combined_video_settings(page) is not None:
                return
            # A click delivered while the SPA is still hydrating is inert, so retry a few times.
            if attempts < 3:
                attempts += 1
                if await enter_video_mode(page):
                    return
            await page.wait_for_timeout(500)
    if elt is None and kind in ("ratio", "duration"):
        elt = await combined_video_settings(page)
    if elt is None:
        raise ReviewNeeded("SETTING_NOT_FOUND", f"未找到可确认的{kind}控件；未提交任务")
    tag = await elt.evaluate("e => e.tagName.toLowerCase()")
    required = str(value)
    if tag == "select":
        values = await elt.locator("option").evaluate_all("els => els.map(e => ({value:e.value,text:e.textContent.trim()}))")
        match = next((x for x in values if x["value"] == required or x["text"] == required), None)
        if not match:
            # Mode can be labelled by Chinese text in a select.
            if kind == "mode":
                match = next((x for x in values if x["text"] == MODES[required]), None)
        if not match: raise ReviewNeeded("SETTING_UNSUPPORTED", f"网页未提供{kind}={required}的选项")
        await elt.select_option(value=match["value"])
        actual = await elt.input_value()
        if actual != match["value"]: raise ReviewNeeded("SETTING_UNVERIFIED", f"{kind}设置未确认")
        return

    if kind == "duration":
        slider = await first_visible(page, ['[role="slider"][aria-valuemin][aria-valuemax]'])
        if slider is None:
            await elt.click()
            slider = await first_visible(page, ['[role="slider"][aria-valuemin][aria-valuemax]'])
        if slider is not None:
            minimum = int(await slider.get_attribute("aria-valuemin"))
            maximum = int(await slider.get_attribute("aria-valuemax"))
            target_value = int(required) - 4
            if not minimum <= target_value <= maximum:
                raise ReviewNeeded("SETTING_UNSUPPORTED", f"网页时长滑块不支持{required}s")
            await slider.focus()
            await slider.press("Home")
            for _ in range(target_value - minimum):
                await slider.press("ArrowRight")
            verified = False
            for _ in range(15):
                if int(await slider.get_attribute("aria-valuenow")) == target_value:
                    verified = True
                    break
                await page.wait_for_timeout(200)
            if not verified:
                raise ReviewNeeded("SETTING_UNVERIFIED", "duration滑块设置后无法验证，停止提交")
            return

    names = [required, MODES.get(required, required)]
    if kind == "duration": names += [required + "秒", required + " 秒", required + "s", required + " s"]
    async def choose_visible_option():
        for name in names:
            for s in (f'[role="option"]:text-is("{name}")', f'[role="menuitem"]:text-is("{name}")',
                      f'[role="menuitemradio"]:text-is("{name}")', f'button:text-is("{name}")',
                      f'[role="radio"]:text-is("{name}")'):
                option = await first_visible(page, [s])
                if option is not None:
                    await option.click()
                    return True
            options = page.get_by_text(name, exact=True)
            for i in range(min(await options.count(), 8)):
                if await options.nth(i).is_visible():
                    await options.nth(i).click()
                    return True
        return False

    # Doubao keeps the combined ratio/duration popover open after choosing a
    # ratio. Reuse an already-visible duration option before toggling it closed.
    clicked = await choose_visible_option()
    if not clicked:
        await elt.click()
        clicked = await choose_visible_option()
    if not clicked: raise ReviewNeeded("SETTING_UNSUPPORTED", f"未找到{kind}={required}的菜单项")
    # Verify selected value is exposed in the collapsed control or its semantic value.
    observed = (await elt.get_attribute("aria-label") or "") + " " + (await elt.inner_text() or "")
    observed += " " + (await elt.get_attribute("data-value") or "")
    if not any(k in observed for k in names):
        checked = await page.locator(f'[role="option"][aria-selected="true"]:text-is("{required}")').count()
        checked += await page.locator(f'[role="radio"][aria-checked="true"]:text-is("{required}")').count()
        if not checked: raise ReviewNeeded("SETTING_UNVERIFIED", f"{kind}选项点击后无法验证，停止提交")


async def select_model(page, model_name: str):
    """选择视频生成模型（以豆包实际提供的模型为准）。"""
    await dismiss_blocking_dialogs(page)
    trigger = None
    for sel in ('[data-testid="chat_input_action_model"]', 'button:has-text("模型")',
                'button:has-text("Seedance")'):
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                trigger = loc
                break
        except Exception:
            continue
    if trigger is None:
        raise ReviewNeeded("MODEL_TRIGGER_NOT_FOUND", "未找到模型选择器")
    await trigger.click()
    target = None
    deadline = asyncio.get_running_loop().time() + 8
    while asyncio.get_running_loop().time() < deadline:
        items = page.locator('[role="menuitem"]')
        try:
            for i in range(await items.count()):
                item = items.nth(i)
                if not await item.is_visible():
                    continue
                text = (await item.inner_text() or "").strip()
                if text.split("\n")[0].strip() == model_name.strip():
                    target = item
                    break
        except Exception:
            pass
        if target is not None:
            break
        await page.wait_for_timeout(300)
    if target is None:
        raise ReviewNeeded("MODEL_NOT_FOUND", f"模型列表里没有找到 {model_name}")
    await target.click()
    await page.wait_for_timeout(400)


async def upload_slot(page, role: str, paths: list[Path]):
    """Upload assets with explicit slot mapping; ambiguous controls are rejected."""
    await dismiss_blocking_dialogs(page)
    selectors = {
        "first": ['input[type="file"][data-slot="first"]', 'input[type="file"][data-testid="first-frame-upload"]',
                  'input[type="file"][aria-label*="首帧"]'],
        "last": ['input[type="file"][data-slot="last"]', 'input[type="file"][data-testid="last-frame-upload"]',
                 'input[type="file"][aria-label*="尾帧"]'],
        "references": ['input[type="file"][data-slot="references"]', 'input[type="file"][data-testid="reference-upload"]',
                       'input[type="file"][aria-label*="参考图"]'],
    }[role]
    loc = None
    for s in selectors:
        inputs = page.locator(s)
        if await inputs.count() == 1:
            loc = inputs.first; break
    if loc is None and role == "references":
        composer = page.locator('[data-testid="chat_input"]').first
        cards = composer.locator('[data-testid="attachment-image-card"]')
        before = await cards.count()
        upload = composer.locator('button').first
        if await upload.count() == 1 and await upload.is_visible():
            try:
                async with page.expect_file_chooser(timeout=5000) as chooser_info:
                    await upload.click()
                chooser = await chooser_info.value
                if len(paths) > 1 and not chooser.is_multiple():
                    raise ReviewNeeded("MULTI_UPLOAD_UNSUPPORTED", "页面参考图上传入口不支持一次多图")
                await chooser.set_files([str(x) for x in paths])
                verified = False
                for _ in range(int(os.getenv("DOUBAO_UPLOAD_VERIFY_TIMEOUT_MS", "12000")) // 250):
                    if await cards.count() == before + len(paths):
                        verified = True
                        break
                    await page.wait_for_timeout(250)
                if not verified:
                    raise ReviewNeeded("UPLOAD_UNVERIFIED", "参考图未显示上传预览；不发送生成任务")
                return
            except ReviewNeeded:
                raise
            except Exception:
                pass
    # Single image input is unambiguous for single-frame only.
    if loc is None and role == "first":
        inputs = page.locator('input[type="file"][accept*="image"]')
        if await inputs.count() == 1: loc = inputs.first
    if loc is None:
        raise ReviewNeeded("UPLOAD_SLOT_NOT_FOUND", f"找不到明确的{role}上传槽；未提交任务")
    if role == "references" and len(paths) > 1 and await loc.get_attribute("multiple") is None:
        raise ReviewNeeded("MULTI_UPLOAD_UNSUPPORTED", "页面参考图上传控件不支持一次多图，不能假定顺序")
    await loc.set_input_files([str(x) for x in paths])
    # Wait for upload confirmation in app UI (not just presence in input.files).
    marker = page.locator(f'[data-asset-status="uploaded"][data-slot="{role}"]')
    try:
        await marker.wait_for(state="visible", timeout=int(os.getenv("DOUBAO_UPLOAD_VERIFY_TIMEOUT_MS", "12000")))
    except Exception:
        raise ReviewNeeded("UPLOAD_UNVERIFIED", f"{role}未显示上传成功确认；不发送生成任务")
    count = await marker.get_attribute("data-count")
    if count is not None and int(count) != len(paths):
        raise ReviewNeeded("UPLOAD_COUNT_MISMATCH", f"{role}成功数量不正确")


async def read_state(page):
    """UI-only snapshot. Multiple historical videos may coexist with a new result.

    Doubao mounts no <video> for a finished result until the card is opened, so
    completion is judged from the message list, never from player tags. Confirm and
    failure flags are scoped to the trailing assistant message: the conversation body
    keeps older "生成中/生成完成" wording, which would otherwise match forever.
    """
    return await page.evaluate(r"""(cfg) => {
      const videoUrls = [];
      for (const v of document.querySelectorAll('video')) {
        const u = v.currentSrc || v.src || v.querySelector('source[src]')?.src;
        if (u && /^https?:/.test(u) && !videoUrls.includes(u)) videoUrls.push(u);
      }
      for (const a of document.querySelectorAll('a[href]')) {
        if (/\.(mp4|webm)(\?|$)/i.test(a.href) && !videoUrls.includes(a.href)) videoUrls.push(a.href);
      }
      const messages = Array.from(document.querySelectorAll(cfg.messageSel));
      const fallback = document.getElementById('message');
      if (!messages.length && fallback) messages.push(fallback);
      const last = messages.at(-1) || null;
      const lastText = last ? (last.innerText || last.textContent || '') : '';
      let cards = document.querySelectorAll(cfg.cardSel).length;
      const result = document.getElementById('result');
      if (result) cards += result.querySelectorAll('video').length;
      const text = (document.body?.innerText || '').slice(-5500);
      const scope = lastText || text.slice(-1500);
      return {videoUrls, videoUrl: videoUrls.at(-1) || null, url: location.href, text,
        messageCount: messages.length, lastMessageText: lastText.slice(-2000), videoCards: cards,
        awaitingConfirm: new RegExp(cfg.confirmAsk).test(scope) && !cards,
        generationStarted: new RegExp(cfg.started).test(scope),
        failed: /生成失败|任务失败/.test(scope),
        running: /生成中|正在生成|排队中|处理中|generating|processing/i.test(text) ||
                 !!document.querySelector('[data-generation-status="running"]'),
        runningLast: /生成中|正在生成|排队中|处理中|generating|processing/i.test(scope) ||
                 !!document.querySelector('[data-generation-status="running"]'),
        done: /生成完成|视频已生成|生成成功|视频生成好了|completed/i.test(text) ||
                 cards > 0 || !!document.querySelector('[data-generation-status="completed"]')};
    }""", {"messageSel": ASSISTANT_MESSAGE, "cardSel": VIDEO_CARD,
           "confirmAsk": CONFIRM_ASK, "started": GENERATION_STARTED})


async def read_composer_state(page):
    """Read only the active composer so chat-history content cannot confirm a submit."""
    return await page.evaluate(r"""() => {
      const composer = document.querySelector('[data-testid="chat_input"]');
      const scope = composer || document;
      const editor = scope.querySelector('[data-testid="chat_input_input"] [contenteditable="true"],'
        + '[role="textbox"][contenteditable="true"],div[contenteditable="true"],textarea');
      const prompt = editor ? ('value' in editor ? editor.value : editor.innerText || editor.textContent || '') : '';
      const send = scope.querySelector('[data-testid="chat_input_send_button"],button[type="submit"]');
      return {exists: !!composer, prompt: prompt.trim(),
        attachmentCount: scope.querySelectorAll('[data-testid="attachment-image-card"]').length,
        sendDisabled: !send || send.disabled || send.getAttribute('aria-disabled') === 'true'};
    }""")

async def send_composer_text(page, text):
    """Answer the assistant in the composer, used for the parameter-confirmation turn."""
    editor = await first_visible(page, EDITOR_SELECTORS)
    if editor is None:
        raise ReviewNeeded("EDITOR_NOT_FOUND", "确认生成时没有找到输入框")
    await editor.fill(text)
    submit = await first_visible(page, SUBMIT_SELECTORS)
    if submit is None or not await submit.is_enabled():
        raise ReviewNeeded("SUBMIT_NOT_FOUND", "确认生成时没有找到发送按钮")
    await submit.click()


MENTION_RE = re.compile(r"@图(\d+)")


def translate_mention_markers(prompt: str) -> str:
    """把提示词里的 @图N 标记翻译成自然语言「参考图N」，走纯文本提交（更稳，不依赖 @ 弹层）。"""
    return MENTION_RE.sub(lambda m: f"参考图{int(m.group(1))}", prompt)


def normalize_prompt_newlines(prompt: str) -> str:
    """contenteditable 编辑器只认 \\n 换行，填入前统一 \\r\\n / \\r，避免 \\r 被编辑器丢弃。"""
    return prompt.replace("\r\n", "\n").replace("\r", "\n")


def prompts_equivalent(filled: str, expected: str) -> bool:
    """回读校验只比对有意义文本：统一换行、折叠连续空行后再比较。

    contenteditable 把每一行渲染成一个块、把每个空行再渲染成一个 <br>，innerText
    回读因此永远无法与带 CRLF / 空行的原文逐字节相等。折叠空行只容忍「空行数量」的
    差异——丢行或截断会改变非空内容，仍会被发现。
    """
    norm = lambda s: re.sub(r"\n+", "\n", s.replace("\r\n", "\n").replace("\r", "\n")).strip()
    return norm(filled) == norm(expected)


async def confirm_pending_generation(page, confirmation_text: str):
    """Push the generation past Doubao's "确认后我再开始生成视频" gate.

    An explicit confirm control inside the trailing assistant message wins; otherwise
    answer in the composer. Callers own the resend budget - this clicks or types once.
    """
    message = page.locator(ASSISTANT_MESSAGE).last
    try:
        if await message.count():
            for label in CONFIRM_LABELS:
                control = message.locator("button, [role='button']").filter(has_text=label)
                if await control.count() and await control.first.is_visible():
                    await control.first.click()
                    return f"button:{label}"
    except Exception:
        pass
    if not confirmation_text.strip():
        raise ReviewNeeded("CONFIRMATION_PRESET_MISSING", "默认确认语为空，未发送确认消息")
    await send_composer_text(page, confirmation_text)
    return f"composer:{confirmation_text}"


async def detail_panel_mounted(page) -> bool:
    for selector in DETAIL_PANEL:
        try:
            loc = page.locator(selector)
            for i in range(min(await loc.count(), 3)):
                if await loc.nth(i).is_visible():
                    return True
        except Exception:
            continue
    return False


async def detail_save_button(page):
    """Find the right-side panel's 保存 control (its file is the unwatermarked master)."""
    for scope in ('[data-testid="canvas_panel_container"]',
                  '[data-testid="samantha_layout_right_side"]'):
        base = page.locator(scope)
        for selector in DETAIL_SAVE:
            try:
                loc = base.locator(selector)
                for i in range(min(await loc.count(), 5)):
                    el = loc.nth(i)
                    if await el.is_visible():
                        return el
            except Exception:
                continue
    # The download testid is specific enough to accept outside the panel scopes;
    # text-only 保存 buttons elsewhere on the page are NOT the video save control.
    try:
        loc = page.locator(DETAIL_SAVE[0])
        for i in range(min(await loc.count(), 5)):
            el = loc.nth(i)
            if await el.is_visible():
                return el
    except Exception:
        pass
    return None


def clean_media_url(urls) -> str | None:
    for u in reversed(list(urls or [])):
        if u and CLEAN_MEDIA_MARK in u:
            return u
    return None


async def click_result_card(page, card_index: int | None = None) -> bool:
    """Click the bound result card; a click during SPA hydration can be inert."""
    card = page.locator(VIDEO_CARD)
    try:
        card_count = await card.count()
    except Exception:
        return False
    if not card_count:
        return False
    target = card.nth(card_index) if card_index is not None and 0 <= card_index < card_count else card.last
    try:
        await target.scroll_into_view_if_needed(timeout=3000)
        await target.hover(timeout=2000)
        await page.wait_for_timeout(250)
        await target.click(timeout=4000)
        return True
    except Exception:
        return False


async def open_result_detail(page, known_urls, card_index: int | None = None,
                             allow_known: bool = False):
    """Open the result card and return ``(media_url, save_button, panel_mounted)``.

    Clicking the card mounts Doubao's right-side detail panel. Its 保存 button produces
    the clean master (source carries ``lr=unwatermarked``), while the plain ``<video>``
    inside the chat card points at the watermarked playback stream. Callers must use the
    returned save control when it exists; the URL alone is only a fallback for legacy
    pages/offline fixtures.
    """
    save = await detail_save_button(page)
    panel = await detail_panel_mounted(page)
    started = asyncio.get_running_loop().time()
    deadline = started + max(3.0, float(os.getenv("DOUBAO_DETAIL_TIMEOUT", "12")))
    clean_wait = float(os.getenv("DOUBAO_CLEAN_URL_WAIT", "2"))
    save_seen_at = started if save is not None else None
    clicked = False
    attempts = 0
    last_click_at = started - 10
    if save is None:
        clicked = await click_result_card(page, card_index)
        last_click_at = asyncio.get_running_loop().time()
        attempts = 1 if clicked else 0
    urls = []
    last_url = None
    url_seen_at = None
    while asyncio.get_running_loop().time() < deadline:
        now = asyncio.get_running_loop().time()
        urls = (await read_state(page))["videoUrls"]
        fresh = [u for u in urls if u not in known_urls]
        candidate = fresh[-1] if fresh else (
            urls[-1] if allow_known and (clicked or panel) and urls else None)
        if candidate:
            last_url = candidate
            url_seen_at = url_seen_at or now
        if save is None:
            save = await detail_save_button(page)
            if save is not None:
                save_seen_at = now
        if panel is False:
            panel = await detail_panel_mounted(page)
        # The first click can be delivered while the SPA is still hydrating. Retry as
        # long as neither the panel, the save control nor a clean source has mounted.
        if (save is None and not panel and not clean_media_url(urls)
                and now - last_click_at >= 2.0 and attempts < 5):
            attempts += 1
            clicked = await click_result_card(page, card_index) or clicked
            last_click_at = asyncio.get_running_loop().time()
        if last_url:
            clean = clean_media_url(urls)
            if save is not None:
                # The clean panel source can mount slightly after the chat player and
                # the save control; give it a moment so the stored URL is not the
                # watermarked playback address.
                if clean or (save_seen_at is not None and now - save_seen_at >= clean_wait):
                    return clean or last_url, save, True
            elif (os.getenv("DOUBAO_TEST_MODE") == "1" and not panel and url_seen_at
                  and now - url_seen_at >= 1.5):
                return last_url, None, False  # legacy offline fixture only
            # Panel mounted but the save control is still mounting (it appears ~0.8s
            # after the card click on the live page): keep polling until the deadline.
        await page.wait_for_timeout(300)
    return clean_media_url(urls) or last_url, save, panel


async def open_result_video(page, known_urls, card_index: int | None = None, allow_known: bool = False):
    """Backward-compatible URL-only view of :func:`open_result_detail`."""
    url, _save, _panel = await open_result_detail(page, known_urls, card_index, allow_known)
    return url


async def download_media(page, url: str, target: Path):
    if not is_safe_media(url):
        raise ReviewNeeded("UNTRUSTED_VIDEO_URL", "视频来源不在受信任域名名单，需人工核实")
    # Do not let a trusted CDN redirect the browser to arbitrary internal URLs.
    # Ask for the whole object explicitly: Doubao's player uses Range requests, and a
    # 206 slice archived as a finished video still passes the container-header check.
    for redirect_number in range(4):
        response = await page.request.get(url, headers={"Range": "bytes=0-"},
                                          timeout=120000, max_redirects=0)
        if 300 <= response.status < 400:
            location = response.headers.get("location")
            url = urljoin(url, location or "")
            if not location or not is_safe_media(url):
                raise ReviewNeeded("UNTRUSTED_REDIRECT", "视频下载重定向到了未受信任地址")
            if redirect_number == 3:
                raise ReviewNeeded("TOO_MANY_REDIRECTS", "视频下载重定向次数过多")
            continue
        break
    if not response.ok: raise ReviewNeeded("DOWNLOAD_HTTP_ERROR", f"下载失败 HTTP {response.status}")
    if int(response.headers.get("content-length", "0") or 0) > MAX_VIDEO:
        raise ReviewNeeded("FILE_TOO_LARGE", "视频文件超过600MB")
    data = await response.body()
    if response.status == 206:
        total = re.search(r"/(\d+)\s*$", response.headers.get("content-range", "") or "")
        if not total or int(total.group(1)) != len(data):
            raise ReviewNeeded("PARTIAL_DOWNLOAD",
                               f"视频地址只返回分段（{len(data)} 字节），不能作为完整成片归档")
    if len(data) < 256 or len(data) > MAX_VIDEO:
        raise ReviewNeeded("INVALID_VIDEO_SIZE", "视频文件大小不合理")
    if not (data[4:8] == b"ftyp" or data[:4] == b"\x1aE\xdf\xa3"):
        raise ReviewNeeded("INVALID_VIDEO", "下载内容不是 MP4/WEBM 容器")
    target = target.with_suffix(".webm" if data[:4] == b"\x1aE\xdf\xa3" else ".mp4")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".part")
    try:
        temp.write_bytes(data)
        # ffprobe is optional on Windows, but when installed enforce real video stream.
        import shutil
        if shutil.which("ffprobe"):
            p = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=codec_type", "-of", "default=noprint_wrappers=1", str(temp),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await p.communicate()
            if p.returncode or b"codec_type=video" not in out:
                raise ReviewNeeded("INVALID_VIDEO_STREAM", "ffprobe 未检测到视频流")
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return str(target)


def media_duration_seconds(path) -> Optional[float]:
    """Best-effort MP4 duration from the movie header. None when it cannot be read.

    Used by the manual page-recovery path to refuse a video whose length does not match
    the job's requested duration (for example an older result card in the conversation).
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if data[4:8] != b"ftyp":
        return None
    i = data.find(b"mvhd")
    while i >= 0:
        try:
            version = data[i + 4]
            if version == 0:
                timescale, duration = struct.unpack(">II", data[i + 16:i + 24])
            elif version == 1:
                timescale, duration = struct.unpack(">IQ", data[i + 24:i + 36])
            else:
                return None
            if timescale:
                return duration / timescale
        except struct.error:
            return None
        i = data.find(b"mvhd", i + 4)
    return None


async def verify_video_file(path: Path) -> str:
    """Validate an archived download and return its real container suffix."""
    size = path.stat().st_size
    if size < 256 or size > MAX_VIDEO:
        raise ReviewNeeded("INVALID_VIDEO_SIZE", "视频文件大小不合理")
    with path.open("rb") as source:
        head = source.read(12)
    if head[4:8] == b"ftyp":
        suffix = ".mp4"
    elif head[:4] == b"\x1aE\xdf\xa3":
        suffix = ".webm"
    else:
        raise ReviewNeeded("INVALID_VIDEO", "下载内容不是 MP4/WEBM 容器")
    import shutil
    if shutil.which("ffprobe"):
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=codec_type", "-of", "default=noprint_wrappers=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await probe.communicate()
        if probe.returncode or b"codec_type=video" not in out:
            raise ReviewNeeded("INVALID_VIDEO_STREAM", "ffprobe 未检测到视频流")
    return suffix


async def save_detail_video(page, save, target: Path) -> str:
    """Click the detail panel's 保存 and archive the browser-downloaded master file."""
    download = None
    try:
        # Playwright timeouts are milliseconds; the env var is documented in seconds.
        async with page.expect_download(
                timeout=float(os.getenv("DOUBAO_SAVE_TIMEOUT", "60")) * 1000) as info:
            await save.click()
        download = await info.value
    except Exception as exc:
        # Some rollouts put 保存到本地/下载到本地 behind a small menu.
        for label in ("保存到本地", "下载到本地", "保存视频"):
            item = page.get_by_text(label, exact=True)
            try:
                if not await item.count():
                    continue
                async with page.expect_download(timeout=30000) as info:
                    await item.first.click()
                download = await info.value
                break
            except Exception:
                continue
        if download is None:
            raise ReviewNeeded("DETAIL_SAVE_FAILED",
                               f"点击保存后没有产生下载：{str(exc)[:200]}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".download")
    try:
        try:
            await download.save_as(temp)
        except Exception as exc:
            raise ReviewNeeded("DETAIL_SAVE_FAILED", f"保存下载文件失败：{str(exc)[:200]}") from exc
        final = target.with_suffix(await verify_video_file(temp))
        temp.replace(final)
        return str(final)
    finally:
        temp.unlink(missing_ok=True)


async def download_result_video(page, known_urls, target: Path, card_index: int | None = None,
                                allow_known: bool = False, on_ready=None):
    """Archive a finished result from the opened detail panel, watermark-free.

    The chat player's URL carries ``lr=video_gen_watermark_dyn``; only the detail panel's
    保存 button (verified byte-identical to the ``lr=unwatermarked`` source) is accepted.
    Legacy inline-player pages without a detail panel keep the old URL download so the
    offline fixtures still exercise the adapter.
    """
    url, save, panel = await open_result_detail(page, known_urls, card_index, allow_known)
    if save is not None:
        if on_ready is not None:
            await on_ready()
        try:
            path = await save_detail_video(page, save, target)
        except ReviewNeeded:
            # The panel's own source is byte-identical to the file its 保存 downloads,
            # so a failed save click may still be recovered without touching the
            # watermarked chat-player stream. Re-read the DOM: the clean source may
            # have mounted after the URL we opened with.
            clean = clean_media_url((await read_state(page)).get("videoUrls") or [])
            if not clean and url and CLEAN_MEDIA_MARK in url:
                clean = url
            if not clean:
                raise
            path = await download_media(page, clean, target)
            return {"video_url": clean, "output": path}
        try:
            mounted = (await read_state(page)).get("videoUrls") or []
        except Exception:
            mounted = []
        return {"video_url": clean_media_url(mounted) or url or "", "output": path}
    if panel:
        raise ReviewNeeded("DETAIL_SAVE_NOT_FOUND",
                           "视频详情面板已打开，但没有找到保存按钮；拒绝归档带水印的播放流")
    if url is None:
        raise ReviewNeeded("RESULT_URL_NOT_FOUND", "已看到结果卡片，但打不开播放器或播放器没有可下载地址")
    if os.getenv("DOUBAO_TEST_MODE") != "1":
        # On the live site the chat-player URL is watermarked; only the offline
        # fixtures have no detail panel and legitimately use this legacy path.
        raise ReviewNeeded("DETAIL_SAVE_NOT_FOUND",
                           "未能打开视频详情面板的保存按钮；拒绝归档带水印的播放流")
    if on_ready is not None:
        await on_ready()
    path = await download_media(page, url, target)
    return {"video_url": url, "output": path}


async def wait_confirm_send(page, before, baseline_cards, timeout_seconds):
    """Decide whether the confirmation answer actually left the composer.

    Reading the contenteditable once immediately after clicking send is a race: Doubao
    can keep the text for a moment while it accepts the message. Accept any independent
    send signal, and report failure only when the composer still holds the answer after
    the whole grace period.
    """
    before_count = int(before.get("messageCount") or 0)
    before_url = before.get("url") or ""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        composer = await read_composer_state(page)
        state = await read_state(page)
        if not composer["prompt"]:
            return "sent", state
        if (int(state.get("messageCount") or 0) > before_count
                or (state.get("url") or "") != before_url
                or not state.get("awaitingConfirm")
                or state.get("generationStarted")
                or state.get("runningLast")
                or int(state.get("videoCards") or 0) > baseline_cards):
            return "sent", state
        if asyncio.get_running_loop().time() >= deadline:
            return "not_sent", state
        await page.wait_for_timeout(250)


async def wait_for_result(page, initial, target, on_status, confirmation_text: str,
                          timeout_seconds=900):
    baseline_cards = int(initial.get("videoCards") or 0)
    known_urls = set(initial.get("videoUrls") or [initial.get("videoUrl")]) - {None}
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    stable, confirmation_answered = 0, False
    while asyncio.get_running_loop().time() < deadline:
        s = await read_state(page)
        t = s["text"]
        if re.search(r"人机验证|安全验证|验证码|captcha", t, re.I):
            raise ReviewNeeded("VERIFICATION_REQUIRED", "页面要求人工验证")
        if re.search(r"额度不足|次数已用完|已达上限|请开通会员", t):
            raise ReviewNeeded("ACCOUNT_LIMIT", "账号额度不足，不自动换号")
        if s["failed"]:
            raise ReviewNeeded("GENERATION_FAILED", "网页显示生成失败")
        if await dismiss_blocking_dialogs(page):
            s = await read_state(page)
        if s["awaitingConfirm"] and not confirmation_answered:
            # Answering costs quota. Send exactly one confirmation; if the trailing
            # message keeps its old confirmation text (Doubao updates it in place while
            # generation starts), never answer it a second time.
            confirmation_answered = True
            await on_status("confirming", "豆包要求确认生成参数，正在发送确认语")
            await automatic_step_delay()
            how = await confirm_pending_generation(page, confirmation_text)
            verdict, _ = await wait_confirm_send(
                page, s, baseline_cards, float(os.getenv("DOUBAO_CONFIRM_SEND_GRACE", "10")))
            if verdict == "not_sent":
                raise ReviewNeeded("CONFIRM_NOT_SENT", "确认生成的消息没有发出去；未消耗额度，需人工核对页面")
            await on_status("generating", f"确认语已发送，等待豆包生成（将消耗额度）：{how}")
            continue
        # Never attribute an older card to this generation: only a card count above the
        # pre-submit baseline, seen stable across two polls, counts as this job's result.
        # Busy-ness is read from the trailing message only - the chat history keeps
        # "视频正在生成中" wording from earlier turns forever.
        stable = stable + 1 if int(s.get("videoCards") or 0) > baseline_cards and not s["runningLast"] else 0
        if stable >= 2:
            await automatic_step_delay()
            async def ready():
                await on_status("downloading", "检测到本任务的新视频，正在打开详情面板下载无水印成片")
            result = await download_result_video(page, known_urls, target,
                                                 card_index=baseline_cards, on_ready=ready)
            return {"video_url": result["video_url"], "web_page_url": s["url"],
                    "output": result["output"]}
        await asyncio.sleep(float(os.getenv("DOUBAO_POLL_SECONDS", "4")))
    raise ReviewNeeded("WAIT_TIMEOUT", "生成等待超时，不能认定为失败或自动重新提交")


REAL_CHAT_URL = re.compile(r"/chat/(?!local_)[0-9A-Za-z_-]+(?:[/?#]|$)")
# Any specific conversation route, including the temporary /chat/local_<id> form.
# local_ pages are profile-local but still identify the job's own conversation for
# "open in the account browser"; only persisted_url waits for the durable numeric id.
CHAT_PAGE_URL = re.compile(r"/chat/[0-9A-Za-z_-]+(?:[/?#]|$)")


async def persisted_url(page, starting):
    """Wait for Doubao to replace /chat/ or the throwaway /chat/local_<id> route.

    A new conversation opens as /chat/ or /chat/local_<timestamp> and is rewritten to
    the real conversation id shortly after the first message. Only the real id can be
    reopened later, which is what recovery and manual review depend on.
    """
    url = starting or ""
    for _ in range(20):
        # Off-site pages (the local fixtures) never rewrite, so only wait on Doubao itself.
        if "doubao.com" not in url or REAL_CHAT_URL.search(url):
            return url
        await page.wait_for_timeout(500)
        url = (await read_state(page))["url"]
    return url


async def perform(ctx, req: VideoRequest, assets: dict[str, Path], target: Path, on_status, on_page,
                  on_binding=None, on_manual_ready=None, confirmation_text: str = "",
                  close_on_success: bool = False, on_review_page=None):
    page = await ctx.new_page()  # New tab ensures previous videos not attributed to this job.
    await on_status("opening", "打开豆包视频页")
    try:
        await page.goto(target_page_url(), wait_until="domcontentloaded", timeout=60000)
        await page.bring_to_front()
        # The composer is wired up after the document exists. Clicking the video skill
        # before then does nothing at all, so wait for it instead of a blind sleep.
        try:
            await page.wait_for_selector('[data-testid="chat_input"], [data-testid="chat_input_input"]',
                                         state="visible", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(1200)
        baseline = await read_state(page)
        if re.search(r"登录后才能|请先登录|扫码登录", baseline["text"]):
            raise ReviewNeeded("LOGIN_REQUIRED", "请在浏览器内人工登录")
        if re.search(r"人机验证|安全验证|验证码|captcha", baseline["text"], re.I):
            raise ReviewNeeded("VERIFICATION_REQUIRED", "页面要求人工验证；未提交任务")
        if re.search(r"额度不足|次数已用完|已达上限|请开通会员", baseline["text"]):
            raise ReviewNeeded("ACCOUNT_LIMIT", "账号额度不足；未提交任务，不自动换号")
        if await dismiss_blocking_dialogs(page):
            baseline = await read_state(page)
        # 「工作」首页也保留 chat_input，必须显式切回「对话」再配置视频。
        await ensure_chat_mode(page)
        await on_status("configuring", "设置视频模式、比例、时长，并核对实际控件")
        await set_ui_option(page, "mode", req.mode)
        await set_ui_option(page, "ratio", req.ratio)
        await set_ui_option(page, "duration", req.duration)
        if req.model:
            await select_model(page, req.model)
        await page.keyboard.press("Escape")
        if req.mode != "text2video":
            await on_status("uploading", "上传并确认图片素材")
            if req.mode == "image2video":
                await upload_slot(page, "first", [assets[req.first_frame_asset_id]])
            elif req.mode == "first_last_frame":
                await upload_slot(page, "first", [assets[req.first_frame_asset_id]])
                await upload_slot(page, "last", [assets[req.last_frame_asset_id]])
            else:
                await upload_slot(page, "references", [assets[x] for x in req.reference_asset_ids])
        await dismiss_blocking_dialogs(page)
        editor = await first_visible(page, EDITOR_SELECTORS)
        if editor is None: raise ReviewNeeded("EDITOR_NOT_FOUND", "没有找到提示词输入框")
        # @图N 标记翻译成自然语言「参考图N」，然后按纯文本填入（附件 + 自然语言，稳定）。
        prompt_text = normalize_prompt_newlines(translate_mention_markers(req.prompt))
        await editor.fill(prompt_text)  # Do not fake duration/ratio via prompt.
        before_submit = await read_composer_state(page)
        if not prompts_equivalent(before_submit["prompt"], prompt_text):
            raise ReviewNeeded("PROMPT_UNVERIFIED", "提示词写入后无法验证；未提交任务")
        initial = await read_state(page)
        if on_binding is not None:
            await on_binding(int(initial.get("videoCards") or 0))
        if req.submit_mode == "manual":
            # 人工发送：只做到“输入框里已经填好提示词和素材”，绝不点击发送。
            # 页面保持打开并置于前台，由操作者在豆包网页亲自发送。
            if on_manual_ready is None:
                raise ReviewNeeded("MANUAL_MODE_UNSUPPORTED", "适配器未配置人工发送就绪回调")
            try:
                await page.bring_to_front()
            except Exception:
                pass
            await on_manual_ready(page, initial)
            return {"manual": True, "video_url": "", "web_page_url": initial.get("url") or "",
                    "output": ""}
        await automatic_step_delay(req.submit_mode or "auto")
        await on_status("submitting", "点击提交中；此后任何不确定状态禁止重发")
        await dismiss_blocking_dialogs(page)
        submit = await first_visible(page, SUBMIT_SELECTORS)
        if submit is None or not await submit.is_enabled():
            raise ReviewNeeded("SUBMIT_NOT_FOUND", "未找到可点击的提交按钮")
        await submit.click()
        confirmed = False
        after = initial
        for _ in range(20):
            await page.wait_for_timeout(250)
            after = await read_state(page)
            composer_after = await read_composer_state(page)
            prompt_cleared = bool(before_submit["prompt"]) and not composer_after["prompt"]
            attachments_consumed = (before_submit["attachmentCount"] == 0 or
                                    composer_after["attachmentCount"] < before_submit["attachmentCount"])
            # Doubao replies to a submitted prompt by asking to confirm the parsed video
            # parameters (and only then spends quota), so "awaitingConfirm" is itself proof
            # the message went out. URL/running/done are not guaranteed to change yet.
            assistant_replied = int(after.get("messageCount") or 0) > int(initial.get("messageCount") or 0)
            confirmed = (after["url"] != initial["url"] or after["running"] or after["done"] or
                         after["awaitingConfirm"] or after["generationStarted"] or assistant_replied or
                         (prompt_cleared and attachments_consumed))
            if confirmed:
                break
        if not confirmed:
            # Record whatever page we landed on: manual review needs it and quota may be spent.
            await on_page(after["url"])
            raise ReviewNeeded("SUBMISSION_UNCONFIRMED", "点击后无法证明已提交，禁止自动重试")
        await on_page(await persisted_url(page, after["url"]))
        await on_status("generating", "已经观察到提交迹象，等待生成")
        result = await wait_for_result(
            page, initial, target, on_status, confirmation_text,
            timeout_seconds=float(os.getenv("DOUBAO_RESULT_TIMEOUT", "900")))
        if close_on_success:
            try:
                await page.close()
            except Exception:
                pass
        return result
    except ReviewNeeded as exc:
        if on_review_page is not None and exc.code in ("VERIFICATION_REQUIRED", "LOGIN_REQUIRED"):
            try:
                await page.bring_to_front()
            except Exception:
                pass
            await on_review_page(page)
        raise
    except Exception as e:
        raise ReviewNeeded("WEB_AUTOMATION_ERROR", str(e)[:350]) from e


def recover_interrupted_jobs(db):
    """Safe restart: never blindly resubmit a job that may have reached the website."""
    with db() as c:
        c.executescript("""CREATE TABLE IF NOT EXISTS video_web_jobs (
          job_id TEXT PRIMARY KEY REFERENCES jobs(id), ratio TEXT, duration INTEGER,
          idempotency_key TEXT UNIQUE, video_url TEXT DEFAULT '', web_page_url TEXT DEFAULT '', error_code TEXT DEFAULT '',
          mode TEXT DEFAULT 'text2video', assets_json TEXT DEFAULT '{}', request_fingerprint TEXT DEFAULT '', result_source TEXT DEFAULT '', model TEXT DEFAULT '',
          quota_day TEXT DEFAULT '', quota_state TEXT DEFAULT '', task_name TEXT DEFAULT '',
          result_card_index INTEGER, recall_state TEXT DEFAULT '', recall_token TEXT DEFAULT '',
          recall_started_at TEXT DEFAULT '', submit_mode TEXT DEFAULT 'auto',
          result_push_url TEXT DEFAULT '', result_push_token TEXT DEFAULT '',
          result_pushed_at TEXT DEFAULT '', result_push_error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS video_web_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, status TEXT, message TEXT, timestamp TEXT);
        CREATE TABLE IF NOT EXISTS video_web_assets (
          id TEXT PRIMARY KEY, sha256 TEXT, original_name TEXT, stored_path TEXT, mime TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS account_quota (
          account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          day TEXT NOT NULL,
          reserved_seconds INTEGER NOT NULL DEFAULT 0,
          consumed_seconds INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (account_id, day));
        CREATE TABLE IF NOT EXISTS local_settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO local_settings(key,value) VALUES('video_submit_mode','auto');
        CREATE INDEX IF NOT EXISTS idx_account_quota_day ON account_quota(day);
        """)
        default_limit = max(0, int(os.environ.get("DOUBAO_MAX_CONCURRENT_ACCOUNTS", "0")))
        c.execute(
            "INSERT OR IGNORE INTO local_settings(key,value) VALUES('processing_limit',?)",
            (str(default_limit),),
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(video_web_jobs)")}
        for name, ddl in (("mode", "TEXT DEFAULT 'text2video'"), ("assets_json", "TEXT DEFAULT '{}'"),
                          ("request_fingerprint", "TEXT DEFAULT ''"), ("result_source", "TEXT DEFAULT ''"),
                          ("model", "TEXT DEFAULT ''"), ("quota_day", "TEXT DEFAULT ''"), ("quota_state", "TEXT DEFAULT ''"),
                          ("task_name", "TEXT DEFAULT ''"), ("result_card_index", "INTEGER"),
                          ("recall_state", "TEXT DEFAULT ''"), ("recall_token", "TEXT DEFAULT ''"),
                          ("recall_started_at", "TEXT DEFAULT ''"), ("submit_mode", "TEXT DEFAULT 'auto'"),
                          ("result_push_url", "TEXT DEFAULT ''"), ("result_push_token", "TEXT DEFAULT ''"),
                          ("result_pushed_at", "TEXT DEFAULT ''"), ("result_push_error", "TEXT DEFAULT ''")):
            if name not in cols: c.execute(f"ALTER TABLE video_web_jobs ADD COLUMN {name} {ddl}")
        unfinished = c.execute("SELECT id,status FROM jobs WHERE status IN (" + ",".join("?" * len(ACTIVE)) + ") "
                               "AND id IN (SELECT job_id FROM video_web_jobs)", ACTIVE).fetchall()
        for r in unfinished:
            c.execute("UPDATE jobs SET status='needs_review', error_message='服务重启中断：需核对网页，禁止自动重复提交', updated_at=? WHERE id=?", (ts(), r["id"]))
            c.execute("UPDATE video_web_jobs SET error_code='INTERRUPTED_REVIEW_REQUIRED' WHERE job_id=?", (r["id"],))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (r["id"], "needs_review", "重启恢复：停止自动提交，等待人工复查", ts()))
            settle_quota(c, r["id"], "needs_review", prior_status=r["status"],
                         error_code="INTERRUPTED_REVIEW_REQUIRED")
        # 人工发送阶段页面与登录上下文随服务重启消失：不知道操作者是否已点过发送，
        # 因此转人工复查，既保留额度占用也提示用“关联下载/复查归档”取回结果。
        waiting = c.execute("SELECT id FROM jobs WHERE status=? "
                            "AND id IN (SELECT job_id FROM video_web_jobs)", (MANUAL_WAIT,)).fetchall()
        for r in waiting:
            c.execute("UPDATE jobs SET status='needs_review', "
                      "error_message='服务重启：任务停在人工发送阶段，请在豆包网页核对是否已发送；"
                      "如已出片请点“关联下载”或“复查归档”取回', updated_at=? WHERE id=?", (ts(), r["id"]))
            c.execute("UPDATE video_web_jobs SET error_code='MANUAL_SEND_INTERRUPTED' WHERE job_id=?", (r["id"],))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (r["id"], "needs_review", "重启恢复：人工发送阶段中断，等待人工核对", ts()))
            settle_quota(c, r["id"], "needs_review", prior_status=MANUAL_WAIT,
                         error_code="MANUAL_SEND_INTERRUPTED")
        # The legacy /api/jobs/{id}/prepare endpoint used to overwrite a web job with the
        # manual-only "ready" status. That hides 复查归档 and blocks manual-result, so put
        # such jobs back into human review instead of leaving them unrecoverable.
        stranded = c.execute("SELECT id FROM jobs WHERE status='ready' "
                             "AND id IN (SELECT job_id FROM video_web_jobs)").fetchall()
        for r in stranded:
            c.execute("UPDATE jobs SET status='needs_review', updated_at=? WHERE id=?", (ts(), r["id"]))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (r["id"], "needs_review", "启动修复：网页任务曾被人工准备覆盖状态，已恢复为需人工复查", ts()))
            settle_quota(c, r["id"], "needs_review", prior_status="ready")
        backfill_existing_quota(c)


def mount_video_api(app, *, db, get_account, get_job, browser, base_dir, downloads_dir, serialize,
                    close_browser=None):
    base = Path(base_dir).resolve()
    downloads = Path(downloads_dir).resolve()
    asset_dir = base / "data" / "web_assets"
    asset_dir.mkdir(parents=True, exist_ok=True)

    def stored_path(path: Path, root: Path) -> str:
        resolved, allowed = path.resolve(), root.resolve()
        if resolved != allowed and allowed not in resolved.parents:
            raise ValueError(f"只能保存 data 目录内的文件路径：{path}")
        return str(resolved.relative_to(base))

    def resolved_path(value: str, root: Path) -> Path:
        raw = Path(value)
        if raw.is_absolute():
            raise HTTPException(409, "data 中存在绝对文件路径，请使用当前版本重新建立该记录")
        resolved, allowed = (base / raw).resolve(), root.resolve()
        if resolved != allowed and allowed not in resolved.parents:
            raise HTTPException(409, "data 中的文件路径超出允许目录")
        return resolved
    # Serialize browser use per account. Different account profiles may run in parallel.
    # No account rotation or retry after submission.
    locks: dict[str, asyncio.Lock] = {}
    pending: dict[str, asyncio.Task] = {}
    # 人工发送任务停留期间保留它准备页面的句柄：操作者点“关联下载”时优先复用这个
    # 已经打开、且可能已经从 /chat/local_* 转成真实会话 id 的标签页。
    prepared_pages: dict[str, object] = {}
    app.state.video_pending = pending
    app.state.video_prepared_pages = prepared_pages
    # The global processing limit is runtime-configurable. Per-account locks remain the
    # stricter rule: one account can never run two browser tasks at the same time.
    processing_condition = asyncio.Condition()
    active_processing = 0

    def local_processing_limit() -> int:
        with db() as c:
            row = c.execute(
                "SELECT value FROM local_settings WHERE key='processing_limit'"
            ).fetchone()
        try:
            return max(0, int(row["value"] if row else 0))
        except (TypeError, ValueError):
            return 0

    @asynccontextmanager
    async def browser_slot():
        nonlocal active_processing
        async with processing_condition:
            await processing_condition.wait_for(
                lambda: local_processing_limit() == 0
                or active_processing < local_processing_limit()
            )
            active_processing += 1
        try:
            yield
        finally:
            async with processing_condition:
                active_processing = max(0, active_processing - 1)
                processing_condition.notify_all()

    def ensure_schema():
        recover_interrupted_schema_only()

    def recover_interrupted_schema_only():
        # In normal app lifespan recovery runs once; this ensures old installations migrate.
        with db() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS video_web_jobs (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id), ratio TEXT, duration INTEGER, idempotency_key TEXT UNIQUE,
                video_url TEXT DEFAULT '',web_page_url TEXT DEFAULT '',error_code TEXT DEFAULT '',
                mode TEXT DEFAULT 'text2video', assets_json TEXT DEFAULT '{}', request_fingerprint TEXT DEFAULT '', result_source TEXT DEFAULT '', model TEXT DEFAULT '',
                quota_day TEXT DEFAULT '', quota_state TEXT DEFAULT '', task_name TEXT DEFAULT '',
                result_card_index INTEGER, recall_state TEXT DEFAULT '', recall_token TEXT DEFAULT '',
                recall_started_at TEXT DEFAULT '', retry_of_job_id TEXT DEFAULT '',
                submit_mode TEXT DEFAULT 'auto', result_push_url TEXT DEFAULT '',
                result_push_token TEXT DEFAULT '', result_pushed_at TEXT DEFAULT '',
                result_push_error TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS video_web_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,status TEXT,message TEXT,timestamp TEXT);
                CREATE TABLE IF NOT EXISTS video_web_assets (
                id TEXT PRIMARY KEY,sha256 TEXT,original_name TEXT,stored_path TEXT,mime TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS account_quota (
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                day TEXT NOT NULL,
                reserved_seconds INTEGER NOT NULL DEFAULT 0,
                consumed_seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, day));
                CREATE TABLE IF NOT EXISTS confirmation_presets (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, text TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS local_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO local_settings(key,value) VALUES('video_submit_mode','auto');
                CREATE INDEX IF NOT EXISTS idx_account_quota_day ON account_quota(day);
            """)
            default_limit = max(0, int(os.environ.get("DOUBAO_MAX_CONCURRENT_ACCOUNTS", "0")))
            c.execute(
                "INSERT OR IGNORE INTO local_settings(key,value) VALUES('processing_limit',?)",
                (str(default_limit),),
            )
            stamp = ts()
            c.execute("""INSERT OR IGNORE INTO confirmation_presets(id,name,text,created_at,updated_at)
                         VALUES('preset_default','默认确认语',?,?,?)""",
                      (CONFIRM_ANSWER, stamp, stamp))
            cols = {x["name"] for x in c.execute("PRAGMA table_info(video_web_jobs)")}
            for name, typ in (("mode","TEXT DEFAULT 'text2video'"), ("assets_json","TEXT DEFAULT '{}'"),
                              ("request_fingerprint","TEXT DEFAULT ''"), ("result_source","TEXT DEFAULT ''"),
                              ("model","TEXT DEFAULT ''"), ("quota_day","TEXT DEFAULT ''"), ("quota_state","TEXT DEFAULT ''"),
                              ("task_name","TEXT DEFAULT ''"), ("result_card_index","INTEGER"),
                              ("recall_state","TEXT DEFAULT ''"), ("recall_token","TEXT DEFAULT ''"),
                              ("recall_started_at","TEXT DEFAULT ''"),
                              ("retry_of_job_id","TEXT DEFAULT ''"),
                              ("submit_mode","TEXT DEFAULT 'auto'"),
                              ("result_push_url","TEXT DEFAULT ''"),
                              ("result_push_token","TEXT DEFAULT ''"),
                              ("result_pushed_at","TEXT DEFAULT ''"),
                              ("result_push_error","TEXT DEFAULT ''")):
                if name not in cols: c.execute(f"ALTER TABLE video_web_jobs ADD COLUMN {name} {typ}")

    def auth(key):
        expected = os.environ.get("DOUBAO_LOCAL_API_KEY", "")
        if expected and key != expected: raise HTTPException(401, "API Key 无效")

    def local_submit_mode() -> str:
        with db() as c:
            row = c.execute(
                "SELECT value FROM local_settings WHERE key='video_submit_mode'"
            ).fetchone()
        value = (row["value"] if row else "auto").strip().lower()
        return value if value in ("auto", "manual") else "auto"

    @app.get("/api/settings/video-submit-mode", tags=["Local Settings"])
    async def get_video_submit_mode():
        ensure_schema()
        return {"submit_mode": local_submit_mode()}

    @app.put("/api/settings/video-submit-mode", tags=["Local Settings"])
    async def update_video_submit_mode(payload: SubmitModeSetting):
        ensure_schema()
        with db() as c:
            c.execute(
                "INSERT INTO local_settings(key,value) VALUES('video_submit_mode',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (payload.submit_mode,),
            )
        return {"submit_mode": payload.submit_mode}

    @app.get("/api/settings/processing-limit", tags=["Local Settings"])
    async def get_processing_limit():
        ensure_schema()
        return {"processing_limit": local_processing_limit(),
                "active_processing": active_processing}

    @app.put("/api/settings/processing-limit", tags=["Local Settings"])
    async def update_processing_limit(payload: ProcessingLimitSetting):
        ensure_schema()
        with db() as c:
            c.execute(
                "INSERT INTO local_settings(key,value) VALUES('processing_limit',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(payload.processing_limit),),
            )
        async with processing_condition:
            processing_condition.notify_all()
        return {"processing_limit": payload.processing_limit,
                "active_processing": active_processing}

    def fingerprint(req):
        # Display naming is metadata, not generation input; changing it must not break
        # idempotent reuse of an already submitted H3 task.
        val = req.model_dump(exclude={"idempotency_key", "auto_start", "task_name", "result_push_url", "result_push_token"})
        return hashlib.sha256(json.dumps(val, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def resolve_assets(req):
        ids = [x for x in [req.first_frame_asset_id, req.last_frame_asset_id] if x] + req.reference_asset_ids
        out = {}
        with db() as c:
            for aid in ids:
                r = c.execute("SELECT stored_path FROM video_web_assets WHERE id=?", (aid,)).fetchone()
                if not r: raise HTTPException(422, f"素材不存在: {aid}")
                p = resolved_path(r["stored_path"], asset_dir)
                if not p.is_file(): raise HTTPException(422, f"素材文件不存在: {aid}")
                out[aid] = p
        return out

    def emit(jobid, status, msg="", code="", result=None, url="", result_source="web_automated"):
        with db() as c:
            previous = c.execute("SELECT status FROM jobs WHERE id=?", (jobid,)).fetchone()
            prior_status = previous["status"] if previous else ""
            c.execute("UPDATE jobs SET status=?,updated_at=?,error_message=? WHERE id=?",
                      (status, ts(), msg if status in ("needs_review", "failed") else "", jobid))
            if code: c.execute("UPDATE video_web_jobs SET error_code=? WHERE job_id=?", (code, jobid))
            if url: c.execute("UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?", (url, jobid))
            if result:
                relative = stored_path(Path(result["output"]), downloads)
                c.execute("UPDATE jobs SET video_path=? WHERE id=?", (relative, jobid))
                c.execute("UPDATE video_web_jobs SET video_url=?, web_page_url=?, error_code='', result_source=? WHERE job_id=?",
                          (result["video_url"], result["web_page_url"], result_source, jobid))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid,status,msg,ts()))
            settle_quota(c, jobid, status, prior_status=prior_status, error_code=code)

    def detail(jobid):
        row = get_job(jobid)
        with db() as c:
            extra = c.execute("SELECT * FROM video_web_jobs WHERE job_id=?", (jobid,)).fetchone()
            quota = quota_summary(c, row["account_id"]) if row["account_id"] else None
        if not extra: raise HTTPException(404, "不是网页视频任务")
        val = serialize(row)
        val["web"] = dict(extra)
        val["web"]["assets"] = json.loads(val["web"].pop("assets_json") or "{}")
        val["web"].pop("request_fingerprint", None)
        val["web"].pop("quota_day", None)
        val["web"]["quota_state"] = val["web"].get("quota_state") or ""
        val["web"].pop("recall_token", None)
        val["web"].pop("result_push_token", None)
        val["task_name"] = val["web"].get("task_name") or ""
        val["video_file_url"] = f"/api/jobs/{jobid}/video" if row["video_path"] else None
        if quota: val["quota"] = quota
        return val

    def assemble_req(jobid):
        with db() as c:
            r = c.execute("SELECT j.*, w.* FROM jobs j JOIN video_web_jobs w ON j.id=w.job_id WHERE j.id=?", (jobid,)).fetchone()
        if not r: raise HTTPException(404, "视频生成任务不存在")
        a = json.loads(r["assets_json"] or "{}")
        return VideoRequest(account_id=r["account_id"], project_id=r["project_id"], episode_id=r["episode_id"],
                            shot_id=r["shot_id"], prompt=r["prompt"], mode=r["mode"],
                            task_name=r["task_name"] or "", ratio=r["ratio"], duration=r["duration"],
                            model=(r["model"] or None), submit_mode=(r["submit_mode"] or "auto"),
                            result_push_url=(r["result_push_url"] or None),
                            result_push_token=(r["result_push_token"] or None), **a)

    def ensure_start_quota(jobid: str, account_id: str) -> None:
        """Ensure this queued job owns enough quota before its account Chrome opens."""
        with db() as c:
            w = c.execute(
                "SELECT duration,quota_day,quota_state FROM video_web_jobs WHERE job_id=?",
                (jobid,),
            ).fetchone()
            if not w:
                raise HTTPException(404, "视频生成任务不存在")
            if quota_held_today(w):
                return
            duration = int(w["duration"] or 0)
            remaining = quota_summary(c, account_id)["remaining_seconds"]
            if duration > remaining:
                raise HTTPException(
                    409, f"账号当日剩余额度不足（需 {duration} 秒，剩余 {remaining} 秒）"
                )
            reserve_quota(c, account_id, duration)
            c.execute(
                "UPDATE video_web_jobs SET quota_day=?,quota_state='reserved' WHERE job_id=?",
                (local_day(), jobid),
            )

    async def finish_downloaded(jobid: str, message: str, result: dict,
                                result_source: str = "web_automated",
                                close_account_after: bool = False) -> None:
        """Archive a result and complete the automatic server-push stage when configured."""
        emit(jobid, "downloaded", message, result=result, result_source=result_source)
        try:
            with db() as c:
                push = c.execute(
                    "SELECT submit_mode,result_push_url,result_push_token,result_pushed_at "
                    "FROM video_web_jobs WHERE job_id=?", (jobid,)).fetchone()
            if (push
                    and (push["submit_mode"] or "auto") == "auto"
                    and push["result_push_url"]
                    and push["result_push_token"]
                    and not push["result_pushed_at"]):
                try:
                    await push_result(jobid)
                except HTTPException as exc:
                    push_error = str(exc.detail)[:350]
                    emit(jobid, "needs_review", "视频已归档，但自动推送服务器失败：" + push_error,
                         code="RESULT_PUSH_FAILED")
        finally:
            if close_account_after:
                account_id = get_job(jobid)["account_id"]
                with db() as c:
                    quota = quota_summary(c, account_id) if account_id else None
                close_note = ""
                if account_id and close_browser is not None:
                    try:
                        await close_browser(account_id)
                        close_note = "；账号 Chrome 已自动关闭"
                    except Exception as exc:
                        close_note = f"；账号 Chrome 自动关闭失败：{str(exc)[:160]}"
                if quota:
                    quota_note = (f"额度已核对：剩余 {quota['remaining_seconds']} 秒 / "
                                  f"{quota['daily_limit_seconds']} 秒")
                    with db() as c:
                        current = c.execute("SELECT status FROM jobs WHERE id=?", (jobid,)).fetchone()
                        c.execute(
                            "INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                            (jobid, current["status"] if current else "downloaded",
                             quota_note + close_note, ts()),
                        )

    async def worker(jobid, req):
        try:
            async with locks.setdefault(req.account_id, asyncio.Lock()):
                async with browser_slot():
                    with db() as c:
                        claimed = c.execute(
                            "UPDATE jobs SET status='opening',updated_at=? "
                            "WHERE id=? AND status='queued'", (ts(), jobid))
                        if not claimed.rowcount:
                            return
                        c.execute(
                            "INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                            (jobid, "opening", "已取得处理名额，开始浏览器操作", ts()),
                        )
                    assets = resolve_assets(req)
                    ctx = await browser(req.account_id)
                    target = downloads / "library" / safe(req.project_id) / safe(req.episode_id) / safe(req.shot_id or jobid) / (jobid + ".mp4")
                    async def status(s,msg): emit(jobid,s,msg)
                    async def page_url(url): emit(jobid, "submitting", "任务页已记录", url=url)
                    async def binding(card_index):
                        with db() as c:
                            c.execute("UPDATE video_web_jobs SET result_card_index=? WHERE job_id=?",
                                      (card_index, jobid))
                    async def manual_ready(page, state):
                        prepared_pages[jobid] = page
                        url = state.get("url") or ""
                        with db() as c:
                            c.execute("UPDATE video_web_jobs SET web_page_url=?, result_card_index=? WHERE job_id=?",
                                      (url, int(state.get("videoCards") or 0), jobid))
                        emit(jobid, MANUAL_WAIT,
                             "已把提示词和素材填入豆包输入框（未发送）；请在豆包网页亲自点击发送，"
                             "生成完成后回到管理台点击“关联下载”。", url=url)
                    async def review_page(page):
                        prepared_pages[jobid] = page
                    result = await perform(ctx, req, assets, target, status, page_url, binding,
                                           on_manual_ready=manual_ready,
                                           confirmation_text=default_confirmation_text(),
                                           close_on_success=True, on_review_page=review_page)
                    if result.get("manual"):
                        return  # 页面保持打开，等待操作者发送与关联下载
                    await automatic_step_delay(req.submit_mode or "auto")
                    await finish_downloaded(jobid, "已下载并校验视频", result,
                                            close_account_after=True)
        except ReviewNeeded as e:
            emit(jobid,"needs_review",e.message,code=e.code)
        except Exception as e:
            emit(jobid,"needs_review",str(e)[:350],code="WORKER_EXCEPTION")
        finally:
            pending.pop(jobid,None)

    def store_image_asset(blob: bytes, filename: str) -> str:
        """Validate an uploaded image blob and persist it under data/web_assets."""
        if not blob or len(blob) > MAX_IMAGE:
            raise HTTPException(413, "图片为空或超过15MB")
        try:
            with Image.open(io.BytesIO(blob)) as im:
                im.verify()
            with Image.open(io.BytesIO(blob)) as im:
                fmt = im.format
                if im.width < 128 or im.height < 128 or im.width > 8192 or im.height > 8192:
                    raise HTTPException(422, "图片尺寸要求为128～8192像素")
        except (UnidentifiedImageError, OSError, ValueError) as e:
            raise HTTPException(422, "不是有效图片") from e
        if fmt not in ("PNG", "JPEG", "WEBP"):
            raise HTTPException(422, "只接受 PNG/JPEG/WEBP 图片")
        ext = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[fmt]
        sha = hashlib.sha256(blob).hexdigest()
        asset_id = "asset_" + uuid.uuid4().hex[:16]
        target = asset_dir / (asset_id + ext)
        target.write_bytes(blob)
        with db() as c:
            c.execute("INSERT INTO video_web_assets VALUES (?,?,?,?,?,?)",
                      (asset_id, sha, Path(filename or "upload").name,
                       stored_path(target, asset_dir), "image/" + fmt.lower(), ts()))
        return asset_id

    @app.post("/api/v1/videos/assets", status_code=201, tags=["Video Assets"])
    async def upload_asset(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        blob = await file.read(MAX_IMAGE + 1)
        await file.close()
        asset_id = store_image_asset(blob, file.filename)
        return {"asset_id": asset_id, "filename": file.filename,
                "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}

    def confirmation_presets():
        with db() as c:
            return [dict(row) for row in c.execute(
                "SELECT id,name,text,created_at,updated_at FROM confirmation_presets ORDER BY created_at,id")]

    def default_confirmation_text() -> str:
        with db() as c:
            row = c.execute(
                "SELECT text FROM confirmation_presets WHERE id='preset_default'"
            ).fetchone()
        text = (row["text"] if row else "").strip()
        if not text:
            raise RuntimeError("默认确认语预设不存在或内容为空")
        return text

    @app.get("/api/v1/videos/confirmation-presets", tags=["Doubao Web Video"])
    async def list_confirmation_presets(x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        return confirmation_presets()

    @app.post("/api/v1/videos/confirmation-presets", status_code=201, tags=["Doubao Web Video"])
    async def create_confirmation_preset(payload: ConfirmationPresetInput,
                                         x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        preset_id = "preset_" + uuid.uuid4().hex[:16]
        stamp = ts()
        with db() as c:
            c.execute("INSERT INTO confirmation_presets(id,name,text,created_at,updated_at) VALUES(?,?,?,?,?)",
                      (preset_id, payload.name.strip(), payload.text.strip(), stamp, stamp))
        return next(row for row in confirmation_presets() if row["id"] == preset_id)

    @app.put("/api/v1/videos/confirmation-presets/{preset_id}", tags=["Doubao Web Video"])
    async def update_confirmation_preset(preset_id: str, payload: ConfirmationPresetInput,
                                         x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        with db() as c:
            result = c.execute("UPDATE confirmation_presets SET name=?,text=?,updated_at=? WHERE id=?",
                               (payload.name.strip(), payload.text.strip(), ts(), preset_id))
            if not result.rowcount:
                raise HTTPException(404, "确认语预设不存在")
        return next(row for row in confirmation_presets() if row["id"] == preset_id)

    @app.delete("/api/v1/videos/confirmation-presets/{preset_id}", tags=["Doubao Web Video"])
    async def delete_confirmation_preset(preset_id: str,
                                         x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        if preset_id == "preset_default":
            raise HTTPException(409, "默认确认语可以编辑，但不能删除")
        with db() as c:
            count = c.execute("SELECT COUNT(*) FROM confirmation_presets").fetchone()[0]
            if count <= 1:
                raise HTTPException(409, "至少保留一个确认语预设")
            result = c.execute("DELETE FROM confirmation_presets WHERE id=?", (preset_id,))
            if not result.rowcount:
                raise HTTPException(404, "确认语预设不存在")
        return {"ok": True, "id": preset_id}

    def auto_select_account(required_seconds: int, avoid_account_id: str = "") -> str:
        """选择额度足够的账号；避开重试账号和忙碌账号后，优先级越高越先使用。"""
        with db() as c:
            rows = c.execute("SELECT * FROM accounts WHERE archived=0 ORDER BY created_at ASC").fetchall()
        active_rows = [row for row in rows if not bool(account_value(row, "paused", 0))]
        if not rows:
            raise HTTPException(409, "没有可用账号，请先创建账号档案并登录豆包")
        if not active_rows:
            raise HTTPException(409, "所有账号均已暂停使用，请先恢复至少一个账号")
        busy = {get_job(jid)["account_id"] for jid, t in pending.items() if not t.done()}
        with db() as c:
            candidates = []
            for r in active_rows:
                remaining = quota_summary(c, r["id"])["remaining_seconds"]
                if remaining >= required_seconds:
                    candidates.append((r["id"], r["created_at"], remaining,
                                       int(account_value(r, "priority", 0) or 0)))
        if candidates:
            candidates.sort(key=lambda item: (
                item[0] == avoid_account_id,
                item[0] in busy,
                -item[3],
                -item[2],
                item[1],
            ))
            return candidates[0][0]
        raise HTTPException(409, f"所有账号当日视频额度不足（本次需 {required_seconds} 秒，每日上限 {DAILY_VIDEO_SECONDS} 秒）")

    def create_generation(req: VideoRequest, *, retry_of_job_id: str = "", avoid_account_id: str = ""):
        """Persist a generation request and optionally start it. Shared by the JSON and multipart entry points."""
        req.submit_mode = req.submit_mode or local_submit_mode()
        # 人工发送必须由本机操作者显式启动：即使调用方传 auto_start=true 也只建单排队，
        # 防止 H3/外部调度器在用户不在电脑前时替人完成网页发送。
        if req.submit_mode == "manual":
            req.auto_start = False
        if not req.account_id:
            req.account_id = auto_select_account(req.duration, avoid_account_id)
        account = get_account(req.account_id)
        if account["archived"]: raise HTTPException(409, "账号已归档")
        if bool(account_value(account, "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        enforce_account_model(req, account)
        resolve_assets(req)
        fp = fingerprint(req)
        if req.idempotency_key:
            with db() as c:
                previous = c.execute("SELECT job_id,request_fingerprint FROM video_web_jobs WHERE idempotency_key=?",
                                     (req.idempotency_key,)).fetchone()
            if previous:
                if previous["request_fingerprint"] and previous["request_fingerprint"] != fp:
                    raise HTTPException(409, "幂等键已用于不同请求")
                if req.task_name.strip() or req.result_push_url:
                    with db() as c:
                        c.execute("""UPDATE video_web_jobs
                                     SET task_name=CASE WHEN task_name='' THEN ? ELSE task_name END,
                                         result_push_url=CASE WHEN ?!='' THEN ? ELSE result_push_url END,
                                         result_push_token=CASE WHEN ?!='' THEN ? ELSE result_push_token END
                                     WHERE job_id=?""",
                                  (req.task_name.strip(), req.result_push_url or "", req.result_push_url or "",
                                   req.result_push_token or "", req.result_push_token or "", previous["job_id"]))
                return detail(previous["job_id"])
        jobid = "job_" + uuid.uuid4().hex[:12]
        stamp = ts()
        payload = req.model_dump(include={"first_frame_asset_id", "last_frame_asset_id", "reference_asset_ids"}, exclude_none=True)
        try:
            with db() as c:
                remaining = quota_summary(c, req.account_id)["remaining_seconds"]
                if req.duration > remaining:
                    raise HTTPException(
                        409,
                        f"账号当日剩余额度不足（需 {req.duration} 秒，剩余 {remaining} 秒）",
                    )
                c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                          " VALUES(?,?,?,?,?,?,'queued',?,?)",
                          (jobid, req.project_id, req.episode_id, req.shot_id, req.prompt, req.account_id, stamp, stamp))
                c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,idempotency_key,mode,assets_json,request_fingerprint,model,quota_day,quota_state,task_name,retry_of_job_id,submit_mode,result_push_url,result_push_token)"
                          " VALUES(?,?,?,?,?,?,?,?,?,'reserved',?,?,?,?,?)",
                          (jobid, req.ratio, req.duration, req.idempotency_key, req.mode, json.dumps(payload), fp,
                           req.model or "", local_day(), req.task_name.strip(), retry_of_job_id, req.submit_mode,
                           req.result_push_url or "", req.result_push_token or ""))
                reserve_quota(c, req.account_id, req.duration)
        except sqlite3.IntegrityError:
            if req.idempotency_key:
                with db() as c:
                    existing = c.execute("SELECT job_id,request_fingerprint FROM video_web_jobs WHERE idempotency_key=?",
                                         (req.idempotency_key,)).fetchone()
                if existing:
                    if existing["request_fingerprint"] != fp: raise HTTPException(409, "幂等键冲突")
                    if req.task_name.strip() or req.result_push_url:
                        with db() as c:
                            c.execute("""UPDATE video_web_jobs
                                         SET task_name=CASE WHEN task_name='' THEN ? ELSE task_name END,
                                             result_push_url=CASE WHEN ?!='' THEN ? ELSE result_push_url END,
                                             result_push_token=CASE WHEN ?!='' THEN ? ELSE result_push_token END
                                         WHERE job_id=?""",
                                      (req.task_name.strip(), req.result_push_url or "", req.result_push_url or "",
                                       req.result_push_token or "", req.result_push_token or "", existing["job_id"]))
                    return detail(existing["job_id"])
            raise HTTPException(409, "任务冲突")
        emit(jobid, "queued",
             "人工发送任务已排队：请在本机任务列表点击“发送到豆包”"
             if req.submit_mode == "manual" else "任务已写入持久化队列")
        if req.auto_start: start_worker(jobid, req)
        return detail(jobid)

    @app.post("/api/v1/videos/generations", status_code=202, tags=["Doubao Web Video"])
    async def create(req: VideoRequest, x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        return create_generation(req)

    def start_worker(jobid,req):
        # Keep persisted status queued until account and global processing slots are free.
        account = get_account(req.account_id)
        if bool(account_value(account, "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        enforce_account_model(req, account)
        ensure_start_quota(jobid, req.account_id)
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409,"任务已经在等待或执行，拒绝重复提交")
        with db() as c:
            if bool(account_value(account, "is_member", 0)):
                c.execute("UPDATE video_web_jobs SET model=? WHERE job_id=?", (MEMBER_MODEL, jobid))
            row = c.execute("SELECT status FROM jobs WHERE id=?", (jobid,)).fetchone()
            if not row or row["status"] != "queued":
                raise HTTPException(409,"任务可能已提交或处理中，拒绝重复提交")
        pending[jobid] = asyncio.create_task(worker(jobid,req))

    @app.get("/api/v1/videos/generations/{jobid}",tags=["Doubao Web Video"])
    async def query(jobid:str,x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema(); return detail(jobid)

    @app.get("/api/v1/videos/generations/{jobid}/events",tags=["Doubao Web Video"])
    async def events(jobid:str,x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema(); detail(jobid)
        with db() as c:
            return [dict(r) for r in c.execute("SELECT status,message,timestamp FROM video_web_events WHERE job_id=? ORDER BY id",(jobid,))]

    @app.post("/api/v1/videos/generations/{jobid}/start",tags=["Doubao Web Video"])
    async def start(jobid:str,x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema()
        req = assemble_req(jobid)
        resolve_assets(req)
        if get_account(req.account_id)["archived"]: raise HTTPException(409,"账号已归档")
        start_worker(jobid,req)
        return detail(jobid)

    async def reset_generation(jobid: str):
        """Reset an existing generation to queued without opening or submitting a page."""
        item = detail(jobid)
        if item["status"] == "downloaded":
            raise HTTPException(409,"任务已归档，无需重新开始")
        if item["status"] not in ("queued", MANUAL_WAIT, "confirming", "generating", "needs_review", "failed"):
            raise HTTPException(409,"任务正在执行；请等待当前浏览器操作结束后再重新开始")
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409,"该任务正在执行，请稍后再重新开始")
        req = assemble_req(jobid)
        resolve_assets(req)
        if get_account(req.account_id)["archived"]:
            raise HTTPException(409,"账号已归档")
        with db() as c:
            w = c.execute("SELECT duration, quota_day, quota_state FROM video_web_jobs WHERE job_id=?",
                          (jobid,)).fetchone()
            if not w:
                raise HTTPException(404,"视频生成任务不存在")
            dur = int(w["duration"] or 0)
            day = local_day()
            state = w["quota_state"] or ""
            if state == "consumed":
                raise HTTPException(409,"任务额度已结算为已消耗，不能原任务重发")
            # 同一天已经占用的额度直接沿用，避免同一次生成意图被重复占用；
            # 只有额度已释放/跨天/旧任务才需要重新占用当天额度。
            held_today = quota_held_today(w)
            if not held_today:
                if state in ("reserved","unknown") and w["quota_day"]:
                    c.execute("UPDATE account_quota SET reserved_seconds=MAX(0,reserved_seconds-?) "
                              "WHERE account_id=? AND day=?",(dur, req.account_id, w["quota_day"]))
                remaining = quota_summary(c, req.account_id)["remaining_seconds"]
                if dur > remaining:
                    raise HTTPException(409,f"账号当日剩余额度不足（需 {dur} 秒，剩余 {remaining} 秒）")
                reserve_quota(c, req.account_id, dur, day)
                c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='reserved' WHERE job_id=?",
                          (day, jobid))
            claimed = c.execute(
                "UPDATE jobs SET status='queued', error_message='', updated_at=? "
                "WHERE id=? AND status=?",
                (ts(), jobid, item["status"]))
            if not claimed.rowcount:
                raise HTTPException(409,"任务状态已变化，请刷新后再试")
            c.execute("""UPDATE video_web_jobs
                         SET web_page_url='', result_card_index=NULL, video_url='', result_source='',
                             error_code='', recall_state='', recall_token='', recall_started_at=''
                         WHERE job_id=?""", (jobid,))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid,"queued","任务已重置为待执行，旧会话与旧结果不再关联",ts()))
        old = prepared_pages.pop(jobid, None)
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        return req

    @app.post("/api/v1/videos/generations/{jobid}/prepare-manual", tags=["Doubao Web Video"])
    async def prepare_manual(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Prepare the task page without sending, regardless of its saved submit mode."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item.get("account_id") and bool(account_value(get_account(item["account_id"]), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        if item["status"] == "queued":
            req = assemble_req(jobid)
            resolve_assets(req)
            if get_account(req.account_id)["archived"]:
                raise HTTPException(409, "账号已归档")
        else:
            req = await reset_generation(jobid)
        req.submit_mode = "manual"
        start_worker(jobid, req)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/restart-auto", tags=["Doubao Web Video"])
    async def restart_auto(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Restart an idle unfinished task from the beginning in automatic mode."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item.get("account_id") and bool(account_value(get_account(item["account_id"]), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        if item["status"] == "queued":
            req = assemble_req(jobid)
            resolve_assets(req)
            if get_account(req.account_id)["archived"]:
                raise HTTPException(409, "账号已归档")
        else:
            req = await reset_generation(jobid)
        req.submit_mode = "auto"
        start_worker(jobid, req)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/reset",tags=["Doubao Web Video"])
    async def reset(jobid:str,confirm_reset:bool=False,x_api_key:Optional[str]=Header(None)):
        """Reset status and old result binding only; never opens a page or submits."""
        auth(x_api_key); ensure_schema()
        if not confirm_reset:
            raise HTTPException(422,"必须明确确认任务重新开始")
        await reset_generation(jobid)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/resend",tags=["Doubao Web Video"])
    async def resend(jobid:str,confirm_resend:bool=False,x_api_key:Optional[str]=Header(None)):
        """Backward-compatible reset-and-start operation, keeping the original job ID."""
        auth(x_api_key); ensure_schema()
        if not confirm_resend:
            raise HTTPException(422,"必须明确确认重新执行（可能再次消耗视频额度）")
        item = detail(jobid)
        if item.get("account_id") and bool(account_value(get_account(item["account_id"]), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        req = await reset_generation(jobid)
        start_worker(jobid, req)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/mark-failed",tags=["Doubao Web Video"])
    async def mark_failed(jobid:str,confirm_failed:bool=False,x_api_key:Optional[str]=Header(None)):
        """操作者人工判定失败：H3/外部调度会收到 failed，停止继续等待该任务。"""
        auth(x_api_key); ensure_schema()
        if not confirm_failed:
            raise HTTPException(422,"必须明确确认人工判定失败")
        item = detail(jobid)
        if (item["web"].get("submit_mode") or "auto") != "manual":
            raise HTTPException(409,"仅人工发送任务支持人工判定失败")
        if item["status"] == "downloaded":
            raise HTTPException(409,"任务已归档，不能判定失败")
        if item["status"] not in ("queued", MANUAL_WAIT, "needs_review", "failed"):
            raise HTTPException(409,"任务正在执行，请等待当前步骤结束再判定失败")
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409,"该任务正在执行，请稍后再判定")
        if item["status"] == "failed" and item["web"].get("error_code") == MANUAL_FAILED:
            return item
        emit(jobid,"failed","人工判定失败：H3/外部调度将收到失败状态，不再等待该任务",code=MANUAL_FAILED)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/copy", status_code=202,
              tags=["Doubao Web Video"])
    async def copy_generation(jobid: str, confirm_copy: bool = False,
                              x_api_key: Optional[str] = Header(None)):
        """Create a queued job from the original server-submitted request parameters."""
        auth(x_api_key); ensure_schema()
        if not confirm_copy:
            raise HTTPException(422, "必须明确确认复制为新任务（可能再次消耗视频额度）")
        req = assemble_req(jobid)
        original_account_id = req.account_id or ""
        req.account_id = None
        req.idempotency_key = None
        req.auto_start = False
        return create_generation(req, avoid_account_id=original_account_id)

    @app.post("/api/v1/videos/generations/{jobid}/retry", status_code=202,
              tags=["Doubao Web Video"])
    async def retry_generation(jobid: str, confirm_retry: bool = False,
                               x_api_key: Optional[str] = Header(None)):
        """Clone a terminal unfinished task and enqueue it with a newly selected account."""
        auth(x_api_key); ensure_schema()
        if not confirm_retry:
            raise HTTPException(422, "必须明确确认重新生成（可能再次消耗视频额度）")
        original = detail(jobid)
        if original["status"] not in ("needs_review", "failed"):
            raise HTTPException(409, "仅需人工处理或失败的未完成任务可以重新生成")
        with db() as c:
            existing = c.execute(
                """SELECT j.id,j.status FROM jobs j JOIN video_web_jobs w ON w.job_id=j.id
                   WHERE w.retry_of_job_id=? AND j.status NOT IN ('needs_review','failed')
                   ORDER BY j.created_at DESC LIMIT 1""", (jobid,)).fetchone()
        if existing:
            raise HTTPException(409, f"该任务已有重试任务 {existing['id']}（{existing['status']}），不会重复提交")
        req = assemble_req(jobid)
        previous_account_id = req.account_id or ""
        req.account_id = None
        req.idempotency_key = None
        req.auto_start = True
        base_name = (req.task_name or original.get("task_name") or jobid).strip()
        req.task_name = (base_name + " · 重试")[:160]
        return create_generation(req, retry_of_job_id=jobid,
                                 avoid_account_id=previous_account_id)

    @app.post("/api/v1/videos/generations/from-job/{jobid}",status_code=202,tags=["Doubao Web Video"])
    async def from_job(jobid:str, submit_mode: str = "auto", x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema()
        if submit_mode not in ("auto", "manual"):
            raise HTTPException(422, "submit_mode 必须是 auto 或 manual")
        row = get_job(jobid)
        if row["status"]!="queued" or not row["account_id"]:
            raise HTTPException(409,"分镜必须是 queued 状态且已选择账号")
        if bool(account_value(get_account(row["account_id"]), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        req = VideoRequest(account_id=row["account_id"],project_id=row["project_id"],
                           episode_id=row["episode_id"],shot_id=row["shot_id"],prompt=row["prompt"],
                           submit_mode=submit_mode)
        with db() as c:
            if c.execute("SELECT 1 FROM video_web_jobs WHERE job_id=?",(jobid,)).fetchone():
                raise HTTPException(409,"分镜已经是网页视频任务")
            remaining = quota_summary(c, req.account_id)["remaining_seconds"]
            if req.duration > remaining:
                raise HTTPException(
                    409, f"账号当日剩余额度不足（需 {req.duration} 秒，剩余 {remaining} 秒）"
                )
            c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,mode,assets_json,quota_day,quota_state,submit_mode) VALUES(?,?,?,?,?,?,'reserved',?)",
                      (jobid,req.ratio,req.duration,req.mode,"{}",local_day(),submit_mode))
            reserve_quota(c, req.account_id, req.duration)
        if submit_mode == "manual":
            emit(jobid, "queued", "人工发送任务已排队；请在任务列表点击“发送到豆包”")
        else:
            start_worker(jobid,req)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/reconcile",tags=["Doubao Web Video"])
    async def reconcile(jobid:str,x_api_key:Optional[str]=Header(None)):
        """Re-read a saved result URL without re-submitting anything."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item["status"]!="needs_review": raise HTTPException(409,"仅允许核对需人工复查的任务")
        url = item["web"]["web_page_url"]
        if not url: raise HTTPException(409,"无已保存的任务页，请人工检查豆包历史记录")
        # Generic creation pages can show somebody else's completed video. In
        # that case we cannot attribute the result to this job after a crash.
        verified_video_url = item["web"]["video_url"]
        if not verified_video_url:
            raise HTTPException(409,"缺少与当前任务绑定的结果视频URL，禁止把历史视频当成当前结果；请人工核实并使用人工归档接口")
        p=urlparse(url)
        if p.scheme!="https" or p.hostname not in ("doubao.com","www.doubao.com"):
            if not (os.getenv("DOUBAO_TEST_MODE")=="1" and p.scheme=="http" and p.hostname in ("127.0.0.1","localhost")):
                raise HTTPException(422,"页面 URL 非受信任域")
        if jobid in pending: raise HTTPException(409,"任务正在执行")
        ctx = await browser(item["account_id"])
        page = await ctx.new_page()
        await page.goto(url,wait_until="domcontentloaded",timeout=60000)
        await page.wait_for_timeout(1500)
        state = await read_state(page)
        if not (state["done"] and not state["running"]):
            return {"ok":False,"status":"needs_review","message":"未确认完成，未重复发送生成请求"}
        # 卡片序号就是本任务结果的绑定关系；不凭“页面上最后一个视频”猜测。
        cards = int(state.get("videoCards") or 0)
        raw_index = item["web"].get("result_card_index")
        card_index = int(raw_index) if raw_index is not None else (0 if cards == 1 else None)
        if card_index is None or card_index >= cards:
            return {"ok":False,"status":"needs_review",
                    "message":"无法确认任务绑定的视频卡片，未下载（禁止把历史视频当成当前结果）"}
        target = downloads / "library" / safe(item["project_id"]) / safe(item["episode_id"]) / safe(item["shot_id"] or jobid) / (jobid+".mp4")
        result = await download_result_video(page, set(state.get("videoUrls") or []), target,
                                             card_index=card_index, allow_known=True)
        await finish_downloaded(
            jobid, "人工复查成功：从详情面板下载无水印成片，无重复生成",
            {"video_url": result["video_url"], "web_page_url": url, "output": result["output"]},
            close_account_after=True)
        try:
            await page.close()
        except Exception:
            pass
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/bind-page", tags=["Doubao Web Video"])
    async def bind_page(jobid: str, payload: BindPageRequest,
                        x_api_key: Optional[str] = Header(None)):
        """Bind a manually identified Doubao conversation without starting recall."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item["status"] not in (MANUAL_WAIT, "needs_review", "failed"):
            raise HTTPException(409, "仅待人工发送、需人工处理或失败的任务可以绑定网页")
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409, "任务正在执行，请等待当前步骤结束后再绑定网页")
        page_url = payload.page_url.strip()
        parsed = urlparse(page_url)
        try:
            trusted_port = parsed.port in (None, 443)
        except ValueError:
            trusted_port = False
        if (parsed.scheme != "https"
                or (parsed.hostname or "").lower() not in ("doubao.com", "www.doubao.com")
                or parsed.username is not None or parsed.password is not None
                or not trusted_port or not REAL_CHAT_URL.fullmatch(parsed.path)):
            raise HTTPException(422, "请提供 https://www.doubao.com/chat/<会话ID> 格式的具体会话地址")
        current_url = (item["web"].get("web_page_url") or "").strip()
        changed_conversation = current_url.split("?")[0] != page_url.split("?")[0]
        with db() as c:
            if changed_conversation:
                c.execute("""UPDATE video_web_jobs
                             SET web_page_url=?, result_card_index=NULL, video_url='', result_source=''
                             WHERE job_id=?""", (page_url, jobid))
            else:
                c.execute("UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?",
                          (page_url, jobid))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid, item["status"], "已人工绑定具体豆包会话，等待关联下载", ts()))
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/manual-result",tags=["Doubao Web Video"])
    async def manual_result(jobid: str, file: UploadFile = File(...),
                            confirm_review: bool = Form(False), x_api_key: Optional[str] = Header(None)):
        """Explicit human-reviewed result attachment. Never resends a generation request."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item["status"] != "needs_review":
            raise HTTPException(409, "仅允许复查状态下上传经人工核对的结果")
        if not confirm_review:
            raise HTTPException(422, "必须明确确认已在豆包网页核对视频与分镜归属")
        if jobid in pending:
            raise HTTPException(409, "任务正在运行，不能导入结果")
        name = (file.filename or "").lower()
        if not name.endswith((".mp4", ".webm")):
            raise HTTPException(415, "仅接受 MP4 或 WEBM 视频")
        folder = downloads / "library" / safe(item["project_id"]) / safe(item["episode_id"]) / safe(item["shot_id"] or jobid)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (jobid + ".uploading")
        digest = hashlib.sha256()
        total = 0
        try:
            with tmp.open("wb") as dest:
                while True:
                    piece = await file.read(1024 * 1024)
                    if not piece: break
                    total += len(piece)
                    if total > MAX_VIDEO: raise HTTPException(413, "视频不得超过600MB")
                    digest.update(piece)
                    dest.write(piece)
            with tmp.open("rb") as source: magic = source.read(16)
            is_mp4 = magic[4:8] == b"ftyp"
            is_webm = magic[:4] == b"\x1aE\xdf\xa3"
            if total < 256 or not (is_mp4 or is_webm):
                raise HTTPException(422,"视频文件头验证失败")
            # Enforce stream validation when ffprobe is installed.
            import shutil
            if shutil.which("ffprobe"):
                proc = await asyncio.create_subprocess_exec(
                    "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                    "stream=codec_type", "-of", "default=noprint_wrappers=1", str(tmp),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, _ = await proc.communicate()
                if proc.returncode or b"codec_type=video" not in out:
                    raise HTTPException(422, "视频流验证失败")
            target = folder / (jobid + (".mp4" if is_mp4 else ".webm"))
            tmp.replace(target)
            await finish_downloaded(
                jobid, "人工复查归档 sha256=" + digest.hexdigest(),
                {"video_url": "", "web_page_url": item["web"]["web_page_url"], "output": str(target)},
                result_source="human_verified_upload")
            return detail(jobid)
        finally:
            await file.close()
            tmp.unlink(missing_ok=True)

    @app.post("/api/v1/videos/generations/{jobid}/open-page", tags=["Doubao Web Video"])
    async def open_page(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Open the saved Doubao conversation in the account's managed Chrome profile.

        Opening the URL in the user's default browser would miss the managed login
        profile, so the persistent context is reused (or launched) here instead.
        """
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除，无法用登录态打开网页")
        # 人工发送任务的标签页可能还没有真实会话 URL，但它本身就是任务页面。
        prepared = prepared_pages.get(jobid)
        if prepared is not None and not prepared.is_closed():
            await prepared.bring_to_front()
            return {"ok": True, "job_id": jobid, "url": prepared.url, "reused": True}
        url = (item["web"].get("web_page_url") or "").strip()
        if not url:
            raise HTTPException(409, "该任务还没有记录网页地址")
        p = urlparse(url)
        is_test_page = (os.getenv("DOUBAO_TEST_MODE") == "1" and p.scheme == "http"
                        and p.hostname in ("127.0.0.1", "localhost"))
        if not is_test_page:
            if p.scheme != "https" or p.hostname not in ("doubao.com", "www.doubao.com"):
                raise HTTPException(422, "任务网页不是受信任的豆包地址")
            if not CHAT_PAGE_URL.search(url):
                raise HTTPException(409, "该任务只记录了通用对话页，请从豆包历史记录中人工查找具体会话")
        busy = jobid in pending and not pending[jobid].done()
        ctx = await browser(account_id)
        key = url.split("?")[0]
        for existing in ctx.pages:
            try:
                if (existing.url or "").split("?")[0] == key:
                    await existing.bring_to_front()
                    return {"ok": True, "job_id": jobid, "url": url, "reused": True}
            except Exception:
                continue
        if busy:
            raise HTTPException(409, "任务正在执行且页面已切换，暂不能跳转；请等任务结束后再打开")
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.bring_to_front()
        return {"ok": True, "job_id": jobid, "url": url, "reused": False}

    @app.post("/api/v1/videos/generations/{jobid}/send", tags=["Doubao Web Video"])
    async def send_prepared_job(jobid: str, confirm_send: bool = False,
                                x_api_key: Optional[str] = Header(None)):
        """Click send for a task prepared through the manual workflow."""
        auth(x_api_key); ensure_schema()
        if not confirm_send:
            raise HTTPException(422, "必须明确确认发送已准备的任务")
        item = detail(jobid)
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409, "任务正在执行，请稍后再发送")
        page = prepared_pages.get(jobid)
        if page is None or page.is_closed():
            prepared_pages.pop(jobid, None)
            raise HTTPException(409, "已准备的任务页面已关闭，请重新准备豆包网页")
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除")
        if bool(account_value(get_account(account_id), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        async with locks.setdefault(account_id, asyncio.Lock()):
            async with browser_slot():
                await page.bring_to_front()
                await dismiss_blocking_dialogs(page)
                before = await read_state(page)
                composer_before = await read_composer_state(page)
                expected = normalize_prompt_newlines(translate_mention_markers(item.get("prompt") or ""))
                if not prompts_equivalent(composer_before["prompt"], expected):
                    raise HTTPException(409, "任务提示词不在输入框中，请重新准备豆包网页")
                submit = await first_visible(page, SUBMIT_SELECTORS)
                if submit is None or not await submit.is_enabled():
                    raise HTTPException(409, "豆包发送按钮不可用，请打开任务页面人工检查")
                await submit.click()
                after = before
                sent = False
                for _ in range(20):
                    await page.wait_for_timeout(250)
                    after = await read_state(page)
                    composer_after = await read_composer_state(page)
                    prompt_cleared = bool(composer_before["prompt"]) and not composer_after["prompt"]
                    attachments_consumed = (composer_before["attachmentCount"] == 0 or
                                            composer_after["attachmentCount"] < composer_before["attachmentCount"])
                    assistant_replied = int(after.get("messageCount") or 0) > int(before.get("messageCount") or 0)
                    sent = (after.get("url") != before.get("url") or after.get("running") or after.get("done") or
                            after.get("awaitingConfirm") or after.get("generationStarted") or assistant_replied or
                            (prompt_cleared and attachments_consumed))
                    if sent:
                        break
                if not sent:
                    emit(jobid, "needs_review", "点击发送后无法证明任务已发出，请人工检查页面",
                         code="SUBMISSION_UNCONFIRMED", url=after.get("url") or "")
                    raise HTTPException(409, "点击后无法确认是否发送成功；任务已转为需人工处理，禁止重复发送")
                live = await persisted_url(page, after.get("url") or "")
                emit(jobid, "confirming", "已发送任务提示词，等待发送确认语", url=live)
                return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/send-confirmation", tags=["Doubao Web Video"])
    async def send_confirmation(jobid: str, payload: Optional[ConfirmationSendRequest] = None,
                                confirm_send: bool = False,
                                x_api_key: Optional[str] = Header(None)):
        """Send a selected confirmation preset on the job's saved conversation page."""
        auth(x_api_key); ensure_schema()
        if not confirm_send:
            raise HTTPException(422, "必须明确确认发送确认消息（会消耗视频额度）")
        item = detail(jobid)
        preset_id = payload.preset_id if payload else "preset_default"
        with db() as c:
            preset = c.execute("SELECT id,name,text FROM confirmation_presets WHERE id=?",
                               (preset_id,)).fetchone()
        if not preset:
            raise HTTPException(404, "确认语预设不存在")
        confirmation_text = preset["text"]
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除")
        if bool(account_value(get_account(account_id), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        # 人工发送任务常停留在还没有真实会话 URL 的标签页上；有活动准备页就直接复用。
        prepared = prepared_pages.get(jobid)
        reusable = prepared is not None and not prepared.is_closed()
        if prepared is not None and not reusable:
            prepared_pages.pop(jobid, None)
        url = (item["web"].get("web_page_url") or "").strip()
        if not reusable:
            if not url or not CHAT_PAGE_URL.search(url):
                raise HTTPException(409, "该任务没有记录具体会话页，无法补发确认")
            p = urlparse(url)
            is_test_page = (os.getenv("DOUBAO_TEST_MODE") == "1" and p.scheme == "http"
                            and p.hostname in ("127.0.0.1", "localhost"))
            if not is_test_page and (p.scheme != "https" or p.hostname not in ("doubao.com", "www.doubao.com")):
                raise HTTPException(422, "任务网页不是受信任的豆包地址")
        with db() as c:
            w = c.execute("SELECT duration, quota_day, quota_state FROM video_web_jobs WHERE job_id=?",
                          (jobid,)).fetchone()
            if w and not quota_held_today(w):
                dur = int(w["duration"] or 0)
                remaining = quota_summary(c, account_id)["remaining_seconds"]
                if dur > remaining:
                    raise HTTPException(409, f"账号当日剩余额度不足（需 {dur} 秒，剩余 {remaining} 秒）")
        ctx = await browser(account_id)
        page = None
        created_page = False
        if reusable:
            page = prepared
        key = url.split("?")[0] if url else ""
        if page is None:
            for existing in getattr(ctx, "pages", []):
                try:
                    if key and (existing.url or "").split("?")[0] == key:
                        page = existing
                        break
                except Exception:
                    continue
        if page is None:
            page = await ctx.new_page()
            created_page = True
        try:
            if created_page:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.bring_to_front()
            await page.wait_for_timeout(1500)
            state = await read_state(page)
            text = state.get("text") or ""
            if re.search(r"登录后才能|请先登录|扫码登录", text):
                raise HTTPException(409, "需要先在该账号浏览器登录豆包")
            if re.search(r"人机验证|安全验证|验证码|captcha", text, re.I):
                raise HTTPException(409, "页面要求人工验证，未发送")
            if re.search(r"额度不足|次数已用完|已达上限|请开通会员", text):
                raise HTTPException(409, "账号额度不足，未发送")
            if await dismiss_blocking_dialogs(page):
                state = await read_state(page)
            await send_composer_text(page, confirmation_text)
            how = f"composer:{confirmation_text}"
            verdict, _ = await wait_confirm_send(
                page, state, int(state.get("videoCards") or 0),
                float(os.getenv("DOUBAO_CONFIRM_SEND_GRACE", "10")))
            if verdict == "not_sent":
                raise HTTPException(409, "确认语没有发出去，请在豆包页面核对")
            live = (await read_state(page)).get("url") or url
            with db() as c:
                dur = int(w["duration"] or 0) if w else 0
                day = local_day()
                next_status = "generating" if item["status"] in (MANUAL_WAIT, "confirming", "needs_review") else item["status"]
                c.execute("UPDATE jobs SET status=?, updated_at=?, error_message='' WHERE id=?",
                          (next_status, ts(), jobid))
                if live and CHAT_PAGE_URL.search(live):
                    c.execute("UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?", (live, jobid))
                c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                          (jobid, next_status, f"人工发送确认语预设“{preset['name']}”：{how}", ts()))
                if w and not quota_held_today(w):
                    c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='reserved', error_code='' WHERE job_id=?",
                              (day, jobid))
                    reserve_quota(c, account_id, dur, day)
                else:
                    c.execute("UPDATE video_web_jobs SET error_code='' WHERE job_id=?", (jobid,))
            return {"ok": True, "job_id": jobid, "preset_id": preset["id"],
                    "preset_name": preset["name"], "how": how,
                    "message": "确认语已发送"}
        except Exception:
            if created_page:
                try:
                    await page.close()
                except Exception:
                    pass
            raise

    def claim_recall(jobid: str) -> str:
        """Atomically claim a persisted recall lease; stale leases may be reclaimed."""
        token = uuid.uuid4().hex
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds")
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            claimed = c.execute(
                """UPDATE video_web_jobs
                   SET recall_state='recalling', recall_token=?, recall_started_at=?
                   WHERE job_id=? AND (recall_state!='recalling' OR recall_started_at='' OR recall_started_at<?)""",
                (token, ts(), jobid, cutoff))
            if not claimed.rowcount:
                raise HTTPException(409, "该任务已有召回操作正在执行")
            c.execute("UPDATE jobs SET status='generating', error_message='', updated_at=? WHERE id=?",
                      (ts(), jobid))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid, "generating", "已取得数据库召回锁，使用原会话等待结果（不会重新提交）", ts()))
        return token

    def release_recall(jobid: str, token: str) -> None:
        with db() as c:
            c.execute("""UPDATE video_web_jobs SET recall_state='', recall_token='', recall_started_at=''
                         WHERE job_id=? AND recall_token=?""", (jobid, token))

    async def recall_worker(jobid: str, token: str, preserve_downloaded: bool = False):
        page = None
        reused = False
        download_succeeded = False
        try:
            item = detail(jobid)
            account_id = item.get("account_id")
            url = (item["web"].get("web_page_url") or "").strip()
            prepared = prepared_pages.get(jobid)
            is_manual = ((item["web"].get("submit_mode") or "auto") == "manual"
                         or (prepared is not None and not prepared.is_closed()))
            async with locks.setdefault(account_id, asyncio.Lock()):
                async with browser_slot():
                    ctx = await browser(account_id)
                    # 人工发送任务的页面可能还停在还没发消息的临时会话上；优先复用它，
                    # 这样用户在网页发送后 URL 已转成真实 /chat/<id> 时能一并记录下来。
                    if prepared is not None and not prepared.is_closed():
                        page = prepared
                        reused = True
                        try:
                            await page.bring_to_front()
                        except Exception:
                            pass
                        await page.wait_for_timeout(800)
                        try:
                            live = (await read_state(page)).get("url") or ""
                        except Exception:
                            live = ""
                        p = urlparse(live)
                        trusted = (p.hostname in ("doubao.com", "www.doubao.com")
                                   or (os.getenv("DOUBAO_TEST_MODE") == "1"
                                       and p.hostname in ("127.0.0.1", "localhost")))
                        if live and trusted and CHAT_PAGE_URL.search(live):
                            url = live
                            with db() as c:
                                c.execute("UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?", (url, jobid))
                    else:
                        prepared_pages.pop(jobid, None)
                        page = await ctx.new_page()
                        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_timeout(1500)
                    opened_at = asyncio.get_running_loop().time()
                    load_grace = float(os.getenv("DOUBAO_RECALL_LOAD_GRACE", "20"))
                    deadline = (asyncio.get_running_loop().time()
                                + float(os.getenv("DOUBAO_RECALL_TIMEOUT", "900")))
                    while asyncio.get_running_loop().time() < deadline:
                        state = await read_state(page)
                        text = state.get("text") or ""
                        if re.search(r"登录后才能|请先登录|扫码登录", text):
                            raise ReviewNeeded("LOGIN_REQUIRED", "召回时发现账号需要重新登录")
                        if re.search(r"人机验证|安全验证|验证码|captcha", text, re.I):
                            raise ReviewNeeded("VERIFICATION_REQUIRED", "召回页面要求人工验证")
                        if await dismiss_blocking_dialogs(page):
                            state = await read_state(page)
                        if state["failed"] and preserve_downloaded:
                            raise ReviewNeeded("GENERATION_FAILED", "豆包页面明确显示生成失败")
                        if state["failed"]:
                            emit(jobid, "failed", "豆包页面明确显示生成失败", code="GENERATION_FAILED")
                            return
                        if state["awaitingConfirm"]:
                            # 刚打开持久化会话时消息列表尚未水合，最后一条仍是历史的
                            # “确认后开始生成”文案；不能在加载宽限期内据此判死。
                            if asyncio.get_running_loop().time() - opened_at < load_grace:
                                await asyncio.sleep(float(os.getenv("DOUBAO_POLL_SECONDS", "4")))
                                continue
                            raise ReviewNeeded("CONFIRM_REQUIRED", "页面仍等待确认；召回不会自动补发确认消息")

                        card_count = int(state.get("videoCards") or 0)
                        raw_index = item["web"].get("result_card_index")
                        card_index = int(raw_index) if raw_index is not None else None
                        if card_index is None and card_count == 1:
                            card_index = 0
                            with db() as c:
                                c.execute("UPDATE video_web_jobs SET result_card_index=0 WHERE job_id=?", (jobid,))
                        if card_index is None and card_count > 1:
                            raise ReviewNeeded("RESULT_BINDING_MISSING",
                                               "旧任务缺少提交前卡片序号，且会话有多个视频，禁止猜测最后一个结果")
                        if card_index is not None and card_count > card_index and not state["runningLast"]:
                            target = (downloads / "library" / safe(item["project_id"]) / safe(item["episode_id"])
                                      / safe(item["shot_id"] or jobid) / (jobid + ".mp4"))
                            result = await download_result_video(
                                page, set(state.get("videoUrls") or []), target,
                                card_index=card_index, allow_known=True)
                            path = result["output"]
                            got = media_duration_seconds(path)
                            want = int(item["web"].get("duration") or 0)
                            if got is not None and want and abs(got - want) > 1.5:
                                Path(path).unlink(missing_ok=True)
                                raise ReviewNeeded("DURATION_MISMATCH",
                                                   f"下载视频时长 {got:.1f}s 与任务 {want}s 不符，未归档")
                            await finish_downloaded(
                                jobid,
                                "已关联人工发送后生成的视频并归档（无水印成片）" if is_manual
                                else "按任务绑定的视频卡片召回并归档（无水印成片）",
                                {"video_url": result["video_url"],
                                 "web_page_url": state.get("url") or url,
                                 "output": path},
                                result_source="manual_web_link" if is_manual else "bound_web_recall",
                                close_account_after=True)
                            download_succeeded = True
                            return
                        if not state["runningLast"] and card_count == 0:
                            # 人工发送时操作者可能刚点过发送：最后一条助手消息会先说
                            # “本次使用…生成”，此时应继续等卡片，而不是立即判失败。
                            if is_manual and state.get("generationStarted"):
                                await asyncio.sleep(float(os.getenv("DOUBAO_POLL_SECONDS", "4")))
                                continue
                            # 刚打开会话时消息列表还在水合，卡片尚未挂载不能当成“没有结果”。
                            if asyncio.get_running_loop().time() - opened_at < load_grace:
                                await asyncio.sleep(float(os.getenv("DOUBAO_POLL_SECONDS", "4")))
                                continue
                            hint = "；请先在豆包网页点击发送并等待生成" if is_manual else ""
                            raise ReviewNeeded("RESULT_NOT_READY", "原会话尚未出现与任务绑定的视频结果" + hint)
                        await asyncio.sleep(float(os.getenv("DOUBAO_POLL_SECONDS", "4")))
                    raise ReviewNeeded("WAIT_TIMEOUT", "召回等待超时；保留额度未知状态，可稍后再次召回")
        except ReviewNeeded as e:
            if preserve_downloaded:
                emit(jobid, "downloaded", "重新下载失败，已保留原归档视频：" + e.message, code=e.code)
            else:
                emit(jobid, "needs_review", e.message, code=e.code)
        except Exception as e:
            if preserve_downloaded:
                emit(jobid, "downloaded", "重新下载失败，已保留原归档视频：" + str(e)[:300], code="RECALL_EXCEPTION")
            else:
                emit(jobid, "needs_review", str(e)[:350], code="RECALL_EXCEPTION")
        finally:
            # 下载成功后，无论页面来自人工准备还是本次召回都关闭；失败时保留复用页面供人工检查。
            if download_succeeded:
                prepared_pages.pop(jobid, None)
            if page is not None and (download_succeeded or not reused):
                try:
                    await page.close()
                except Exception:
                    pass
            release_recall(jobid, token)
            pending.pop(jobid, None)

    async def begin_recall(jobid: str, wait: bool = False, force_redownload: bool = False):
        item = detail(jobid)
        preserve_downloaded = item["status"] == "downloaded"
        if preserve_downloaded and not force_redownload:
            return item
        if jobid in pending and not pending[jobid].done():
            return item
        allowed = ("downloaded", "needs_review", "failed", MANUAL_WAIT, "confirming", "generating")
        if item["status"] not in allowed:
            raise HTTPException(409, "仅需复查、失败或待人工发送的任务可以关联/召回")
        prepared = prepared_pages.get(jobid)
        reusable = prepared is not None and not prepared.is_closed()
        if prepared is not None and not reusable:
            prepared_pages.pop(jobid, None)
        url = (item["web"].get("web_page_url") or "").strip()
        if not reusable:
            if not url or not CHAT_PAGE_URL.search(url):
                if item["status"] == MANUAL_WAIT:
                    raise HTTPException(409, "准备页面已关闭，且未记录具体会话；请在豆包历史记录中人工核对，"
                                             "并用“复查归档”导入视频")
                raise HTTPException(409, "该任务没有记录具体会话页，无法召回")
            p = urlparse(url)
            is_test_page = (os.getenv("DOUBAO_TEST_MODE") == "1" and p.scheme == "http"
                            and p.hostname in ("127.0.0.1", "localhost"))
            if not is_test_page and (p.scheme != "https" or p.hostname not in ("doubao.com", "www.doubao.com")):
                raise HTTPException(422, "任务网页不是受信任的豆包地址")
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除")
        token = claim_recall(jobid)
        if wait:
            await recall_worker(jobid, token, preserve_downloaded=preserve_downloaded)
            return detail(jobid)
        pending[jobid] = asyncio.create_task(
            recall_worker(jobid, token, preserve_downloaded=preserve_downloaded))
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/recall", tags=["Doubao Web Video"])
    async def recall(jobid: str, force_redownload: bool = False,
                     x_api_key: Optional[str] = Header(None)):
        """Idempotently resume/download an existing result; never submits generation."""
        auth(x_api_key); ensure_schema()
        return await begin_recall(jobid, force_redownload=force_redownload)

    @app.post("/api/v1/videos/generations/{jobid}/download-result", tags=["Doubao Web Video"])
    async def download_result(jobid: str, confirm_review: bool = False,
                              x_api_key: Optional[str] = Header(None)):
        """Backward-compatible alias for the unified recall endpoint."""
        auth(x_api_key); ensure_schema()
        if not confirm_review:
            raise HTTPException(422, "必须明确确认已在豆包网页核对视频与分镜归属")
        return await begin_recall(jobid, wait=True)

    @app.post("/api/v1/videos/generations/{jobid}/push-result", tags=["Doubao Web Video"])
    async def push_result(jobid: str):
        """Push an archived local video back to the H3 task that created this job."""
        ensure_schema()
        row = get_job(jobid)
        if not row["video_path"]:
            raise HTTPException(409, "请先下载并归档视频，再推送服务器")
        with db() as c:
            push = c.execute(
                "SELECT result_push_url,result_push_token,duration,error_code FROM video_web_jobs WHERE job_id=?",
                (jobid,),
            ).fetchone()
        if not push or not push["result_push_url"] or not push["result_push_token"]:
            raise HTTPException(409, "该任务不是由支持回推的 H3 服务提交，无法推送")

        parsed = urlparse(push["result_push_url"])
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise HTTPException(422, "H3 回推地址必须通过本机 SSH 转发端口访问")
        video_path = resolved_path(row["video_path"], downloads)
        if not video_path.is_file():
            raise HTTPException(404, "本地归档视频不存在，请重新下载")

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0)) as client:
                with video_path.open("rb") as video:
                    response = await client.post(
                        push["result_push_url"],
                        headers={"X-Doubao-Push-Token": push["result_push_token"]},
                        data={"duration": str(push["duration"] or "")},
                        files={"file": (video_path.name, video, "video/mp4")},
                    )
            if response.is_error:
                try:
                    payload = response.json()
                    detail_msg = payload.get("message") or payload.get("detail") or response.text
                except Exception:
                    detail_msg = response.text
                raise RuntimeError(f"H3 返回 HTTP {response.status_code}：{str(detail_msg)[:300]}")
        except Exception as exc:
            message = str(exc)[:500]
            with db() as c:
                c.execute("UPDATE video_web_jobs SET result_push_error=? WHERE job_id=?", (message, jobid))
            raise HTTPException(502, f"推送 H3 失败：{message}") from exc

        pushed_at = ts()
        with db() as c:
            recovered = (row["status"] == "needs_review"
                         and push["error_code"] == "RESULT_PUSH_FAILED")
            c.execute(
                "UPDATE video_web_jobs SET result_pushed_at=?,result_push_error='',"
                "error_code=CASE WHEN ? THEN '' ELSE error_code END WHERE job_id=?",
                (pushed_at, recovered, jobid),
            )
            if recovered:
                c.execute("UPDATE jobs SET status='downloaded',error_message='',updated_at=? WHERE id=?",
                          (pushed_at, jobid))
            c.execute(
                "INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                (jobid, "downloaded" if recovered else row["status"],
                 "已将归档视频推送到 H3", pushed_at),
            )
        return {"ok": True, "job_id": jobid, "pushed_at": pushed_at}

    @app.get("/api/v1/videos/capabilities",tags=["Doubao Web Video"])
    async def capabilities():
        return {"provider":"doubao_web","modes":list(MODES),
                "ratios":"doubao_ui_defined","durations":"doubao_ui_defined",
                "state":"implemented_live_verified","precise_ui_controls_required":True,
                "assets":"local_upload_with_explicit_slots","automatic_account_rotation":False,
                "submit_modes":["auto","manual"],
                "manual_flow":"manual 任务创建后先排队；在本机点击开始后只填提示词和素材，不点发送；"
                              "操作者在豆包网页发送，出片后点关联下载（recall）归档；"
                              "需要重发时用 resend，job_id 不变，关联下载只认新页面",
                "manual_resend":True,"manual_resend_keeps_job_id":True,
                "manual_fail_verdict":True,
                "manual_review_upload":True,"reconcile_requires_exact_video_url":True,
                "safe_restart":"no_resubmit_if_interrupted",
                "daily_video_seconds":DAILY_VIDEO_SECONDS,
                "account_auto_select":"quota_aware",
                "note":"duration/ratio 以豆包网页实际提供的选项为准（常见 16:9/9:16/1:1、4/10/15 秒），适配器不再硬编码限制；账号每天额度上限可经 DOUBAO_DAILY_VIDEO_SECONDS 覆盖"}

    @app.get("/api/v1/videos/confirm-answer", tags=["Doubao Web Video"])
    async def confirm_answer(x_api_key: Optional[str] = Header(None)):
        """The exact text the adapter sends when Doubao asks to confirm generation."""
        auth(x_api_key); ensure_schema()
        return {"text": default_confirmation_text()}

    # ---- 单入口接口（供外部系统集成，拉取式）----
    # 一个 multipart 请求带上所有图片与参数即可提交；之后轮询状态、下载视频。

    @app.post("/api/v1/videos", status_code=202, tags=["Doubao Web Video"])
    async def create_video_v1(
        account_id: Optional[str] = Form(default=None),
        prompt: str = Form(...),
        mode: str = Form("text2video"),
        ratio: str = Form("16:9"),
        duration: int = Form(4),
        model: Optional[str] = Form(default=None),
        project_name: str = Form(""),
        episode_name: str = Form(""),
        shot_name: str = Form(""),
        task_name: str = Form(""),
        # Compatibility for existing callers. New integrations should send the *_name fields above.
        project_id: str = Form(""),
        episode_id: str = Form(""),
        shot_id: str = Form(""),
        idempotency_key: Optional[str] = Form(default=None),
        result_push_url: Optional[str] = Form(default=None),
        result_push_token: Optional[str] = Form(default=None),
        auto_start: bool = Form(default=True),
        submit_mode: Optional[str] = Form(default=None),
        reference: Optional[List[UploadFile]] = File(default=None),
        first_frame: Optional[UploadFile] = File(default=None),
        last_frame: Optional[UploadFile] = File(default=None),
        x_api_key: Optional[str] = Header(default=None),
    ):
        """单入口提交：图片作为文件字段直接上传，无需先调用 /assets。"""
        auth(x_api_key); ensure_schema()
        if mode not in MODES:
            raise HTTPException(422, f"mode 必须是 {list(MODES)} 之一")
        if submit_mode is not None and submit_mode not in ("auto", "manual"):
            raise HTTPException(422, "submit_mode 必须是 auto（自动提交）或 manual（人工发送）")

        async def read_blob(f: UploadFile):
            b = await f.read(MAX_IMAGE + 1)
            await f.close()
            return b

        ref_ids = [store_image_asset(await read_blob(f), f.filename) for f in (reference or [])]
        first_id = store_image_asset(await read_blob(first_frame), first_frame.filename) if first_frame else None
        last_id = store_image_asset(await read_blob(last_frame), last_frame.filename) if last_frame else None

        has_ref, has_first, has_last = bool(ref_ids), bool(first_id), bool(last_id)
        if mode == "text2video" and (has_first or has_last or has_ref):
            raise HTTPException(422, "文生视频不能附带图片")
        if mode == "image2video" and (not has_first or has_last or has_ref):
            raise HTTPException(422, "首帧图生视频需要且只允许 first_frame")
        if mode == "first_last_frame" and (not has_first or not has_last or has_ref):
            raise HTTPException(422, "首尾帧模式需要 first_frame 和 last_frame")
        if mode == "reference_images" and (not has_ref or has_first or has_last or not (1 <= len(ref_ids) <= 9)):
            raise HTTPException(422, "参考图模式需要 1～9 个 reference 文件")

        try:
            req = VideoRequest(account_id=account_id, prompt=prompt, mode=mode, ratio=ratio, duration=duration,
                               model=model, project_id=project_name.strip() or project_id,
                               episode_id=episode_name.strip() or episode_id,
                               shot_id=shot_name.strip() or shot_id, task_name=task_name,
                               first_frame_asset_id=first_id, last_frame_asset_id=last_id,
                               reference_asset_ids=ref_ids, idempotency_key=idempotency_key, auto_start=auto_start,
                               submit_mode=submit_mode, result_push_url=result_push_url,
                               result_push_token=result_push_token)
        except ValidationError as e:
            raise HTTPException(422, f"参数校验失败：{e.errors()[0].get('msg', '')}")
        item = create_generation(req)
        return {"job_id": item["id"], "status": item["status"],
                "account_id": item["account_id"], "submit_mode": item["web"].get("submit_mode", "auto"),
                "status_url": f"/api/v1/videos/{item['id']}",
                "video_url": f"/api/v1/videos/{item['id']}/video"}

    @app.get("/api/v1/videos/{job_id}", tags=["Doubao Web Video"])
    async def video_status_v1(job_id: str, x_api_key: Optional[str] = Header(default=None)):
        auth(x_api_key); ensure_schema()
        item = detail(job_id)
        return {"job_id": job_id, "status": item["status"],
                "mode": item["web"].get("mode"), "ratio": item["web"].get("ratio"),
                "duration": item["web"].get("duration"),
                "submit_mode": item["web"].get("submit_mode", "auto"),
                "error_code": item["web"].get("error_code"), "error_message": item.get("error_message"),
                "result_source": item["web"].get("result_source"),
                "quota": item.get("quota"),
                "video_url": f"/api/v1/videos/{job_id}/video" if item.get("video_path") else None,
                "web_video_url": item["web"].get("video_url")}

    @app.get("/api/v1/videos/{job_id}/video", tags=["Doubao Web Video"])
    async def video_file_v1(job_id: str, x_api_key: Optional[str] = Header(default=None)):
        auth(x_api_key); ensure_schema()
        row = get_job(job_id)
        if not row["video_path"]:
            raise HTTPException(404, "该任务还没有归档视频")
        path = resolved_path(row["video_path"], downloads)
        if not path.is_file():
            raise HTTPException(404, "视频文件不存在")
        return FileResponse(path, filename=path.name)
