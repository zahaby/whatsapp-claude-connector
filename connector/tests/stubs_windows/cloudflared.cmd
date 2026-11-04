@echo off
echo cloudflared %* >> "%STUB_STATE%\calls"
exit /b 0
