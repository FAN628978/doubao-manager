import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doubao_manager.web.utils import concise_job_title  # noqa: E402


def test_concise_job_title_uses_storyboard_parts():
    assert concise_job_title("摄政王-上端", "第七集", "分镜#17") == "摄政王-7集-#17"


def test_concise_job_title_uses_first_four_project_characters():
    title = concise_job_title("很长的项目名称", "第一百零七集", "分镜#007")

    assert title == "很长的项-107集-#7"


def test_concise_job_title_falls_back_without_metadata():
    assert concise_job_title(fallback="单独的任务名称") == "单独的任务名称"
