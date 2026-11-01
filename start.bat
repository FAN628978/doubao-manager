@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未找到 uv，请先安装 uv 并将其加入 PATH。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在使用 uv 创建 Python 虚拟环境...
  uv venv .venv
  if errorlevel 1 goto :fail
)

uv run --no-project --python ".venv\Scripts\python.exe" python -c "import fastapi, uvicorn, playwright, multipart, PIL" >nul 2>nul
if errorlevel 1 (
  echo 正在使用 uv 安装 Python 依赖...
  uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt
  if errorlevel 1 goto :fail
)

if not exist data mkdir data

rem 启动前释放被残留进程占用的 8765 端口（避免 WinError 10048）
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
  echo 检测到残留进程 PID %%P 占用 8765 端口，正在结束...
  taskkill /f /pid %%P >nul 2>nul
)

start "Doubao Account Desk" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8765"
echo Doubao Account Desk 正在启动：http://127.0.0.1:8765
echo 关闭本窗口即可停止本地服务。
uv run --no-project --python ".venv\Scripts\python.exe" python -m uvicorn app:app --app-dir src --host 127.0.0.1 --port 8765
pause
exit /b %errorlevel%

:fail
echo.
echo [ERROR] 启动失败，请检查 uv、Python 环境和网络后重试。
pause
exit /b 1
