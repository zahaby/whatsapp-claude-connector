@echo off
echo go %* >> "%STUB_STATE%\calls"
if "%1"=="build" type nul > whatsapp-bridge.exe
exit /b 0
