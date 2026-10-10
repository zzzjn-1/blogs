@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM  双人对话播客自动生成系统 - 停止开发服务
REM
REM  1) 按监听端口 (8000 / 5173) 结束 uvicorn 与 vite 进程；
REM  2) 关闭 start_dev.bat 起的两个托管命令行窗口；
REM  3) 复核端口是否真的释放，未释放则如实告警。
REM
REM  与旧版的差别：端口匹配收紧为「:端口 + 空格」，
REM  避免 findstr ":8000" 误命中别的字段（如外部地址列）。
REM ============================================================

set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5173"

echo.
echo 正在停止播客开发服务 ...

for %%P in (%BACKEND_PORT% %FRONTEND_PORT%) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%%P " ^| findstr "LISTENING"') do (
        echo   - 端口 %%P 被 PID %%a 占用，正在结束该进程 ...
        taskkill /PID %%a /F >nul 2>&1
    )
)

REM 进程被结束后，cmd /k 的宿主窗口本身不会自动关闭
taskkill /FI "WINDOWTITLE eq podcast-backend" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq podcast-frontend" /F >nul 2>&1

ping -n 2 127.0.0.1 >nul

set "STILL="
for %%P in (%BACKEND_PORT% %FRONTEND_PORT%) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%%P " ^| findstr "LISTENING"') do set "STILL=!STILL! %%P(PID=%%a)"
)

if defined STILL goto :still_busy
echo 完成：%BACKEND_PORT% 与 %FRONTEND_PORT% 均已释放。
echo.
endlocal
exit /b 0

:still_busy
echo.
echo   [警告] 以下端口仍在监听：!STILL!
echo          可能不是本项目启动的进程，未做处理，请手动确认。
echo.
endlocal
exit /b 2
