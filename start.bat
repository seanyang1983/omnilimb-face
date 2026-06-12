@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title omnilimb-face launcher

rem ==========================================================================
rem  omnilimb-face 一键启动
rem  - 自动定位插件自带的 .venv（找不到则回退到系统 python / py）
rem  - 启动 preview.py（独立预览：渲染形象 + 对口型 + 切表情）
rem  - 提供几种常用模式的菜单
rem ==========================================================================

rem 切到本脚本所在目录（插件根），保证相对路径正确
cd /d "%~dp0"

rem ---- 选择 python 解释器 ----------------------------------------------------
set "PY="
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul && set "PY=py -3"
    if not defined PY (
        where python >nul 2>nul && set "PY=python"
    )
)

if not defined PY (
    echo [错误] 找不到 Python。请先在插件目录创建虚拟环境并安装依赖：
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e ".[preview]"
    echo.
    pause
    exit /b 1
)

echo 使用的 Python: %PY%
echo.

rem ---- 菜单 ------------------------------------------------------------------
echo ============================================================
echo            omnilimb-face  一键启动
echo ============================================================
echo   1.  本机预览（默认，浏览器自动打开 http://127.0.0.1:12394/）
echo   2.  本机预览 + 真实 hermes LLM 回复（需 hermes venv）
echo   3.  手机 / 局域网 HTTPS（单端口，可用手机麦克风）
echo   4.  手机 / 局域网 HTTPS + 真实 hermes LLM + 本地 STT
echo   5.  静音模式（无 TTS，仅合成口型）
echo   0.  退出
echo ============================================================
set "CHOICE="
set /p "CHOICE=请选择 [回车=1]: "
if not defined CHOICE set "CHOICE=1"

if "%CHOICE%"=="0" exit /b 0
if "%CHOICE%"=="1" set "ARGS="
if "%CHOICE%"=="2" set "ARGS=--llm"
if "%CHOICE%"=="3" set "ARGS=--lan --https --no-browser"
if "%CHOICE%"=="4" set "ARGS=--lan --https --no-browser --llm --stt"
if "%CHOICE%"=="5" set "ARGS=--no-tts"

if not defined ARGS if not "%CHOICE%"=="1" (
    echo 无效选择：%CHOICE%
    pause
    exit /b 1
)

echo.
echo 启动命令: %PY% preview.py %ARGS%
echo （按 Ctrl+C 可停止服务）
echo.

if "%CHOICE%"=="3" goto :lan_hint
if "%CHOICE%"=="4" goto :lan_hint
goto :run

:lan_hint
echo ------------------------------------------------------------
echo  局域网 HTTPS 提示：
echo   - 网关与页面同端口 :12393（单端口模式）。
echo   - 手机用 https://<本机局域网IP>:12393/ 打开。
echo   - 首次会有自签证书警告，点「继续 / 高级 → 继续访问」即可。
echo   - 查看本机 IP：在新窗口运行 ipconfig，找无线网卡的 IPv4。
echo ------------------------------------------------------------
echo.

:run
%PY% preview.py %ARGS%

echo.
echo 服务已退出。
pause
endlocal
