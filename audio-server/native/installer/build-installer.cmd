@echo off
rem Builds the program (Release, unit tests run), then the installer, at the version in tools\VERSION:
rem   tools\audio-server\native\dist\clew-audio-server-setup-<version>.exe
rem Needs Visual Studio's C++ tools and Inno Setup 6 (winget install JRSoftware.InnoSetup).
rem
rem Signing: when this user's certificate store holds the YaqaLab code-signing certificate
rem (make-cert.ps1 makes one), the program, the installer and the uninstaller are signed with it.
rem Without it everything is built unsigned. CLEW_SIGN_TIMESTAMP=<RFC 3161 URL> adds a timestamp
rem (that sends the files' hashes to that server; off unless set).
setlocal
cd /d "%~dp0"
call ..\build.cmd Release || exit /b 1
rem Dictation's speech server (docs/28), built once with Python into tools\stt-server\dist.
if not exist ..\..\..\stt-server\dist\clew-stt-server\clew-stt-server.exe (
  powershell -NoProfile -ExecutionPolicy Bypass -File ..\..\..\stt-server\build_exe.ps1 || exit /b 1
)
set /p VERSION=<..\..\..\VERSION
if not defined VERSION (echo tools\VERSION is empty.& exit /b 1)
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (echo Inno Setup 6 was not found: winget install JRSoftware.InnoSetup& exit /b 1)

powershell -NoProfile -Command "if (Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -eq 'CN=YaqaLab' -and $_.NotAfter -gt (Get-Date) -and $_.HasPrivateKey }) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo No YaqaLab certificate in this user's store: building unsigned. make-cert.ps1 makes one.
  "%ISCC%" /Q /DAppVersion=%VERSION% clew-audio-server.iss || exit /b 1
  goto done
)

set "SIGNTOOL="
for /f "delims=" %%s in ('dir /b /s /o:n "%ProgramFiles(x86)%\Windows Kits\10\bin\*signtool.exe" 2^>nul ^| findstr /i "\\x64\\signtool.exe"') do set "SIGNTOOL=%%s"
if not defined SIGNTOOL (echo signtool.exe was not found; install the Windows SDK.& exit /b 1)
set "STAMP="
if defined CLEW_SIGN_TIMESTAMP set "STAMP=/tr %CLEW_SIGN_TIMESTAMP% /td SHA256"
set SIGN=sign /fd SHA256 /n YaqaLab /s My %STAMP%

"%SIGNTOOL%" %SIGN% /d "Clew audio server" ..\build\clew-audio-server.exe || exit /b 1
"%SIGNTOOL%" %SIGN% /d "Clew dictation server" ..\..\..\stt-server\dist\clew-stt-server\clew-stt-server.exe || exit /b 1
"%ISCC%" /Q /DAppVersion=%VERSION% /DSign "/Syaqalab=$q%SIGNTOOL%$q %SIGN% /d $qClew Audio Server$q $f" clew-audio-server.iss || exit /b 1

:done
echo Built: %~dp0..\dist
