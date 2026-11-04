@echo off
echo cloudflared %* >> "%STUB_STATE%\calls"
echo %* | findstr /C:"-o json" >nul && echo {"id":"tid-123","name":"wa-connector"}
exit /b 0
