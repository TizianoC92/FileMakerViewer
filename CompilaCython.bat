@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Ambiente Python non trovato. Crea prima la cartella .venv.
    pause
    exit /b 1
)

set "VSWHERE=C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
set "VS_INSTALL="
if exist "%VSWHERE%" (
    for /f "usebackq tokens=*" %%V in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VS_INSTALL=%%V"
)
if not defined VS_INSTALL set "VS_INSTALL=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
if not exist "%VS_INSTALL%\VC\Auxiliary\Build\vcvars64.bat" (
    echo Microsoft C++ Build Tools non trovato. Installa il carico di lavoro Strumenti di compilazione C++.
    pause
    exit /b 1
)

call "%VS_INSTALL%\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 (
    echo Inizializzazione del compilatore non riuscita.
    pause
    exit /b 1
)
set "DISTUTILS_USE_SDK=1"

"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements-build.txt"
if errorlevel 1 (
    echo Installazione di Cython non riuscita.
    pause
    exit /b 1
)

"%~dp0.venv\Scripts\python.exe" "%~dp0setup.py" build_ext --inplace
if errorlevel 1 (
    echo Compilazione non riuscita. Verifica che Microsoft C++ Build Tools sia installato.
    pause
    exit /b 1
)

echo Lettore compilato. Avvia il programma con Avvia.bat.
pause
