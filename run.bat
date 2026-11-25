@echo off
chcp 65001 >nul
title Tarona Izla Bot

echo ========================================
echo   🎵 Tarona Izla Bot - Ishga tushirish
echo ========================================
echo.

:: PATH ni kengaytirish
set "PATH=%PATH%;%APPDATA%\Python\Python314\Scripts;%APPDATA%\Python\Python312\Scripts"

:: .env faylini tekshirish
if not exist ".env" (
    echo [!] .env fayl topilmadi!
    echo     .env faylga BOT_TOKEN ni yozing.
    echo.
    pause
    exit /b 1
)

:: Botni ishga tushirish
echo [*] Bot ishga tushmoqda...
echo.
python -u bot.py

pause
