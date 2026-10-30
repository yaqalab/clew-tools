@echo off
rem Builds the Windows program and runs the unit tests. Usage: build.cmd [Release|Debug]
setlocal
rem One code page for configuring and building: MSVC writes its "including file" lines in it,
rem and ninja matches them against the prefix CMake recorded (CMakeLists.txt). Another code
rem page at build time changes the line, ninja then records no header for that build, and a
rem changed header later leaves stale objects.
chcp 65001 >nul
set CONFIG=%1
if "%CONFIG%"=="" set CONFIG=Release
set "PATH=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer;%PATH%"
for /f "usebackq delims=" %%i in (`vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set VSDIR=%%i
if not defined VSDIR (echo Visual Studio with the C++ tools was not found.& exit /b 1)
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
cd /d "%~dp0"
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=%CONFIG% || exit /b 1
cmake --build build || exit /b 1
build\unit_tests.exe || exit /b 1
