@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if "%~1"=="" (
 echo 用法: test-live.bat --account-id acc_xxx --mode text2video --duration 4 --ratio 16:9
 echo 也可以: test-live.bat --job-id job_xxx 仅查询，不重复提交
 echo 首帧: --mode image2video --first-frame "D:\images\first.png"
 echo 首尾帧: --mode first_last_frame --first-frame "D:\first.png" --last-frame "D:\last.png"
 echo 多参考图: --mode reference_images --reference "D:\role.png" --reference "D:\scene.png"
 echo 此脚本会请求确认，可能消耗所选账号的视频生成额度。
 pause
 exit /b 2
)
python scripts\live_acceptance.py %*
set CODE=%errorlevel%
echo [END] ExitCode=%CODE%. 报告保存到 data\acceptance_reports\
pause
exit /b %CODE%
