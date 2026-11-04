from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

class VideoRequest(BaseModel):
    # 外部 H3 任务可指定稳定 ID（例如 H3 8758 对应 job_8758）。
    job_id: Optional[str] = Field(default=None, pattern=r"^job_[1-9]\d*$", max_length=64)
    # 留空 = 进入“待处理”后不绑定账号；任务进入“正在处理”前才分配账号。
    account_id: Optional[str] = Field(default=None)
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


class AutoDispatchSetting(BaseModel):
    auto_dispatch_enabled: bool


class AutoDispatchIntervalSetting(BaseModel):
    auto_dispatch_interval_seconds: int = Field(ge=1, le=3600)


class TaskAcceptanceSetting(BaseModel):
    accepting_tasks: bool


class VideoModelSetting(BaseModel):
    member_model: Literal["Seedance 2.5", "Seedance 2.0", "Seedance 2.0 Fast", "Seedance 2.0 Mini"]
    non_member_model: Literal["Seedance 2.5", "Seedance 2.0", "Seedance 2.0 Fast", "Seedance 2.0 Mini"]


class ProcessingLimitSetting(BaseModel):
    processing_limit: int = Field(ge=0)


class IgnoreQuotaSetting(BaseModel):
    ignore_quota_check: bool


class ClaudeRecoverySetting(BaseModel):
    claude_recovery_enabled: bool


class AutoSwitchOnExceptionSetting(BaseModel):
    auto_switch_on_exception: bool


class ConfirmationSendRequest(BaseModel):
    preset_id: str = Field(min_length=1, max_length=80)


class QueueAccountRequest(BaseModel):
    """Assign (or clear with null) the account of a not-yet-started job."""
    account_id: Optional[str] = Field(default=None, max_length=120)


class ReviewNeeded(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)
