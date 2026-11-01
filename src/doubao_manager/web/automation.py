from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, UnidentifiedImageError
from playwright.async_api import APIRequestContext, Download, Page

from .config import (
    ACTIVE,
    ASSISTANT_MESSAGE,
    CHAT_PAGE_URL,
    CLEAN_MEDIA_MARK,
    CONFIRM_ANSWER,
    CONFIRM_ASK,
    CONFIRM_LABELS,
    DETAIL_PANEL,
    DETAIL_SAVE,
    EDITOR_SELECTORS,
    GENERATION_STARTED,
    HOME_MODE_SWITCH,
    MANUAL_FAILED,
    MANUAL_WAIT,
    MAX_IMAGE,
    MAX_VIDEO,
    MENTION_RE,
    MODES,
    REAL_CHAT_URL,
    SETTINGS_PANEL,
    SETTINGS_TEXT,
    SIDEBAR_FAILED,
    SUBMIT_SELECTORS,
    TRUSTED_CDNS,
    VIDEO_CARD,
    VIDEO_PAGE,
    WORK_HOME_TEXT,
)
from .models import ReviewNeeded, VideoRequest
from .utils import adaptive_poll, automatic_step_delay, concise_job_title, safe, set_job_page_title, ts

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
        if more is not None:
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
        else:
            # Current composer rollouts may expose a plain text chip without a
            # stable test id. Match the exact label so chat history is ignored.
            controls = page.locator('button,[role="button"]')
            clicked = False
            for i in range(min(await controls.count(), 80)):
                control = controls.nth(i)
                try:
                    if (await control.is_visible()
                            and (await control.inner_text()).strip() == "视频生成"):
                        await control.click()
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
    started_at = asyncio.get_running_loop().time()
    deadline = started_at + timeout_seconds
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
        await adaptive_poll(page, started_at)
    raise ReviewNeeded("WAIT_TIMEOUT", "生成等待超时，不能认定为失败或自动重新提交")


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
        await set_job_page_title(
            page,
            concise_job_title(
                req.project_id,
                req.episode_id,
                req.shot_id,
                fallback=req.task_name,
            ),
        )
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
