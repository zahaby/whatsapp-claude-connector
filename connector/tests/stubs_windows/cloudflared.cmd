@echo off
echo cloudflared %* >> "%STUB_STATE%\calls"
rem Like the real tool: options after the tunnel name are refused.
echo %* | findstr /R /C:"tunnel info [^- ][^ ]* -" >nul && (echo "cloudflared tunnel info" accepts exactly one argument, the ID or name of the tunnel to get info about. 1>&2 & exit /b 1)
echo %* | findstr /C:"info -o json" >nul && echo {"id":"8f3569a9-6bc5-4355-924d-ba2d2cae182b","name":"wa-connector"}
exit /b 0
