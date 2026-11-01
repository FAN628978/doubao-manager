@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
python -m pip install -r requirements-test.txt
if errorlevel 1 goto :fail
python -m playwright install chrome
if errorlevel 1 goto :fail
python -m pytest -q tests
if errorlevel 1 goto :fail
echo [PASS] 离线测试通过。这不是豆包官网实机出片验收。
pause
exit /b 0
:fail
echo [FAIL] 测试失败，请保留完整输出。
pause
exit /b 1
