@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 守忆 - 本地记忆工作台
echo.
echo   守忆 SHOUYI
echo   正在启动本地工作台，浏览器将自动打开。
echo   地址：http://127.0.0.1:8137
echo   关闭此窗口会停止服务。
echo.
where py >nul 2>&1
if errorlevel 1 goto use_python
py -3 app.py
goto finish
:use_python
python app.py
:finish
if errorlevel 1 echo 启动失败。请确认已安装 Python 3.10 或更新版本，且 8137 端口未被其他程序占用。
pause
