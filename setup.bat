@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo ==========================================
echo Doubao Account Desk - Environment Setup
echo ==========================================
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未找到 Python。请安装 Python 3.10 或更高版本，并勾选 Add Python to PATH。
  pause
  exit /b 1
)
python -m pip install --upgrade pip
if errorlevel 1 goto :fail
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail
python scripts\install_chrome.py
if errorlevel 1 (
  echo [WARN] 项目专用 Chrome 安装未完成。请检查网络后重新运行 setup.bat。
)
echo.
echo [OK] 环境安装完成。双击 start.bat 启动。
pause
exit /b 0
:fail
echo.
echo [ERROR] 安装失败，请检查 Python、网络和权限后重试。
pause
exit /b 1
