@echo off
rem Build the Anna-voice tools.  Usage: build.bat [path\to\ms-ana-decomp\src] [x64]
rem
rem samleaf and samunit only need this repository.  annacorpus and annaunits read Microsoft Anna's corpus,
rem so they need the Microsoft Anna port's sources (https://github.com/KamiKitsune420/ms-ana-decomp).
setlocal
set ANNA=%1
if "%ANNA%"=="" set ANNA=..\..\ms-ana-decomp\src
set ARCH=%2
if "%ARCH%"=="" set ARCH=x64
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not defined VSCMD_VER call :findvs
cd /d %~dp0
if not exist build\%ARCH% mkdir build\%ARCH%
set CF=/nologo /O2 /W3 /fp:precise /D_CRT_SECURE_NO_WARNINGS /Fo:build\%ARCH%\ /Fdbuild\%ARCH%\vc.pdb
cl %CF% /I..\src samleaf.c ..\src\sam.c ..\src\sam_lex.c ..\src\sam_norm.c ..\src\sam_pos.c ..\src\sam_morph.c /Fe:build\%ARCH%\samleaf.exe || exit /b 1
cl %CF% /I..\src samunit.c ..\src\sam.c /Fe:build\%ARCH%\samunit.exe || exit /b 1
if not exist "%ANNA%\anna.h" (
  echo.
  echo Anna's sources were not found at %ANNA% - skipping annacorpus / annaunits.
  echo Pass the path: build.bat path\to\ms-ana-decomp\src
  goto :eof
)
cl %CF% /I"%ANNA%" annaunits.c "%ANNA%\anna_units.c" /Fe:build\%ARCH%\annaunits.exe || exit /b 1
cl %CF% /I"%ANNA%" annacorpus.c "%ANNA%\anna_units.c" "%ANNA%\anna_dec_wmav.c" /Fe:build\%ARCH%\annacorpus.exe || exit /b 1
goto :eof

:findvs
for %%d in ("%VSWHERE%") do set "PATH=%%~dpd;%PATH%"
for /f "usebackq delims=" %%i in (`call "%VSWHERE%" -latest -products * -property installationPath`) do call "%%i\VC\Auxiliary\Build\vcvarsall.bat" %ARCH% >nul
goto :eof
