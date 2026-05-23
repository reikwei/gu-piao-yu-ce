@echo off
setlocal
chcp 65001 >nul
REM -*- coding: utf-8 -*-
REM Git 上传脚本 - 自动提交和推送代码到 GitHub

cd /d %~dp0

if not defined TARGET_REPO set "TARGET_REPO=https://github.com/reikwei/gu-piao-yu-ce.git"
if not defined GIT_PROXY set "GIT_PROXY=http://127.0.0.1:7895"
if not defined DEFAULT_GIT_USER_NAME set "DEFAULT_GIT_USER_NAME=reikwei"
if not defined DEFAULT_GIT_USER_EMAIL set "DEFAULT_GIT_USER_EMAIL=host2ez@qq.com"

echo.
echo ========================================
echo    Kronos A股预测 - Git 上传脚本
echo ========================================
echo.

REM 检查git是否安装
git --version >nul 2>&1
if errorlevel 1 (
    echo ❌ 错误：未检测到 Git 环境
    echo 请先安装 Git: https://git-scm.com/download/win
    pause
    exit /b 1
)

set "CURRENT_BRANCH="
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo ℹ️  当前目录还不是 Git 仓库，正在初始化...
    git init
    if errorlevel 1 (
        echo ❌ Git 仓库初始化失败
        pause
        exit /b 1
    )
    git branch -M main
    if errorlevel 1 (
        echo ❌ main 分支初始化失败
        pause
        exit /b 1
    )
    echo ✅ Git 仓库初始化完成
    echo.
) else (
    for /f %%i in ('git symbolic-ref --short HEAD 2^>nul') do set "CURRENT_BRANCH=%%i"
)
if defined CURRENT_BRANCH if /I not "%CURRENT_BRANCH%"=="main" (
    echo ℹ️  当前分支为 %CURRENT_BRANCH%，正在切换到 main...
    git branch -M main
    if errorlevel 1 (
        echo ❌ 切换到 main 分支失败
        pause
        exit /b 1
    )
    echo ✅ 当前分支已切换为 main
    echo.
)

if /I "%PUSH_GIT_SKIP_PROXY%"=="1" (
    echo 🌐 已跳过代理设置，将直接推送
) else (
    echo 🌐 推送时将使用临时代理 (%GIT_PROXY%)
)
echo.

echo 🎯 目标仓库: %TARGET_REPO%
git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo ℹ️  当前未检测到 origin，正在创建...
    git remote add origin %TARGET_REPO%
) else (
    echo ℹ️  正在校正 origin 到指定仓库...
    git remote set-url origin %TARGET_REPO%
)
if errorlevel 1 (
    echo ❌ 远程仓库设置失败
    pause
    exit /b 1
)
echo ✅ 远程仓库已设置为:
git remote -v
echo.

for /f "delims=" %%i in ('git config user.name 2^>nul') do set "GIT_NAME=%%i"
for /f "delims=" %%i in ('git config user.email 2^>nul') do set "GIT_EMAIL=%%i"
if not defined GIT_NAME if defined GIT_USER_NAME set "GIT_NAME=%GIT_USER_NAME%"
if not defined GIT_EMAIL if defined GIT_USER_EMAIL set "GIT_EMAIL=%GIT_USER_EMAIL%"
if not defined GIT_NAME set "GIT_NAME=%DEFAULT_GIT_USER_NAME%"
if not defined GIT_EMAIL set "GIT_EMAIL=%DEFAULT_GIT_USER_EMAIL%"
if not defined GIT_NAME (
    echo ❌ Git 用户名不能为空
    pause
    exit /b 1
)
if not defined GIT_EMAIL (
    echo ❌ Git 邮箱不能为空
    pause
    exit /b 1
)
git config user.name "%GIT_NAME%"
git config user.email "%GIT_EMAIL%"
if errorlevel 1 (
    echo ❌ Git 提交身份配置失败
    pause
    exit /b 1
)
echo ✅ Git 提交身份已配置为 %GIT_NAME% (%GIT_EMAIL%)
echo.

echo ℹ️  当前仓库状态：
echo.
git status --short
echo.

if defined COMMIT_MSG (
    set "commit_msg=%COMMIT_MSG%"
    echo ℹ️  使用环境变量 COMMIT_MSG: "%commit_msg%"
) else (
    set /p commit_msg="请输入提交信息 (默认 'update code'): "
)
if "%commit_msg%"=="" set commit_msg=update code

echo.
echo ========================================
echo 📋 执行操作步骤：
echo ========================================
echo.

echo [1/3] 添加所有文件...
git add .
if errorlevel 1 (
    echo ❌ 添加文件失败
    pause
    exit /b 1
)
echo ✅ 完成
echo.

echo [2/3] 提交更改 (消息: "%commit_msg%")...
git commit -m "%commit_msg%"
if errorlevel 1 (
    git diff --cached --quiet >nul 2>&1
    if errorlevel 1 (
        echo ❌ 提交失败
        pause
        exit /b 1
    )
    echo ⚠️  没有新更改，跳过提交
)
echo ✅ 完成
echo.

echo [3/3] 推送到远程 main 分支...
if /I "%PUSH_GIT_SKIP_PROXY%"=="1" (
    git push -u origin main
) else (
    git -c http.proxy=%GIT_PROXY% -c https.proxy=%GIT_PROXY% push -u origin main
)
if errorlevel 1 (
    echo ❌ 推送失败，请检查网络连接、仓库地址或远程 main 分支状态
    pause
    exit /b 1
)
echo ✅ 完成
echo.

echo ========================================
echo ✅ 上传完成！代码已推送到 GitHub
echo ========================================
echo.
git log -1 --oneline
echo.
