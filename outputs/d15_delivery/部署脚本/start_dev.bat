@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM  双人对话播客自动生成系统 - 启动开发服务（前端 + 后端）
REM
REM  后端 : FastAPI / uvicorn @ 127.0.0.1:8000
REM  前端 : Vite dev          @ 127.0.0.1:5173
REM         vite 代理 '/api' 与正则 '^/feed/.+$' -> 127.0.0.1:8000，
REM         同源转发，JWT HttpOnly Cookie 随请求自动携带。
REM
REM  用法 : 双击本文件即可；停止请运行同目录 stop_dev.bat
REM
REM  说明 : 后端刻意只绑 127.0.0.1（不绑 0.0.0.0）——
REM         既不把服务暴露到局域网，也不会在首次启动时弹
REM         Windows 防火墙授权框。若要让同局域网设备访问，
REM         把 BACKEND_HOST 改成 0.0.0.0，并把 .env 的
REM         PUBLIC_BASE_URL 改成该机的局域网地址。
REM ============================================================

set "BACKEND_DIR=D:\podcast-ai"
set "FRONTEND_DIR=D:\podcast-ai\frontend"
set "PYTHON_EXE=D:\anaconda\envs\cosyvoice\python.exe"
set "VITE_JS=%FRONTEND_DIR%\node_modules\vite\bin\vite.js"
set "BACKEND_HOST=127.0.0.1"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5173"

REM ---- 解析 node.exe：优先 versions\current（不钉死具体版本号）----
set "NODE_EXE="
if exist "%USERPROFILE%\.workbuddy\binaries\node\versions\current\node.exe" set "NODE_EXE=%USERPROFILE%\.workbuddy\binaries\node\versions\current\node.exe"
if not defined NODE_EXE if exist "C:\Users\zzz\.workbuddy\binaries\node\versions\22.22.2-3\node.exe" set "NODE_EXE=C:\Users\zzz\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"

echo.
echo ======================================================
echo   双人对话播客自动生成系统 - 启动开发服务
echo ======================================================
echo.

REM ---- [1/3] 依赖预检 ----
set "MISSING="
if not exist "%BACKEND_DIR%\api\main.py" set "MISSING=%MISSING%  - 后端入口 %BACKEND_DIR%\api\main.py"
if not exist "%PYTHON_EXE%" set "MISSING=%MISSING%  - Python   %PYTHON_EXE%"
if not exist "%VITE_JS%" set "MISSING=%MISSING%  - vite入口 %VITE_JS%"
if not defined NODE_EXE set "MISSING=%MISSING%  - node.exe 未在 .workbuddy\binaries\node 下找到"

if not defined MISSING goto :deps_ok
echo [1/3] 依赖预检 : 失败
echo.
echo   [错误] 以下依赖缺失，无法启动：
echo %MISSING%
echo.
echo   提示：node.exe 由 WorkBuddy 托管，若其升级后版本目录变化，
echo         请修改本脚本的 NODE_EXE 解析段。
echo.
pause
exit /b 1

:deps_ok
echo [1/3] 依赖预检 : 通过

REM ---- [2/3] 端口预检 ----
set "BUSY="
for %%P in (%BACKEND_PORT% %FRONTEND_PORT%) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%%P " ^| findstr "LISTENING"') do set "BUSY=!BUSY! %%P(PID=%%a)"
)

if not defined BUSY goto :ports_ok
echo [2/3] 端口预检 : 失败
echo.
echo   [错误] 端口已被占用：!BUSY!
echo          请先运行 stop_dev.bat，或手动释放端口后再启动。
echo.
pause
exit /b 1

:ports_ok
echo [2/3] 端口预检 : 通过（%BACKEND_PORT% / %FRONTEND_PORT% 空闲）
echo.

REM ---- [3/3] 启动 ----
echo [3/3] 启动后端 uvicorn @ %BACKEND_HOST%:%BACKEND_PORT% ...
start "podcast-backend" cmd /k "cd /d %BACKEND_DIR% && %PYTHON_EXE% -m uvicorn api.main:app --host %BACKEND_HOST% --port %BACKEND_PORT% --log-level info"

echo       启动前端 vite dev @ 127.0.0.1:%FRONTEND_PORT% ...
start "podcast-frontend" cmd /k "cd /d %FRONTEND_DIR% && %NODE_EXE% %VITE_JS% --port %FRONTEND_PORT% --strictPort --host 127.0.0.1"

echo.
echo ======================================================
echo   两个服务已在独立窗口启动。
echo     前端 : http://127.0.0.1:%FRONTEND_PORT%
echo     后端 : http://%BACKEND_HOST%:%BACKEND_PORT%/docs   (Swagger)
echo     RSS  : http://%BACKEND_HOST%:%BACKEND_PORT%/feed/{token}.xml
echo.
echo   提示：后端首次启动要加载模型，浏览器请稍等几秒再刷新；
echo         停止请运行 stop_dev.bat（或直接关掉那两个窗口）。
echo ======================================================
echo.
pause
endlocal
