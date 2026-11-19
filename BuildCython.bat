@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Python environment not found. Create the .venv folder first.
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
    echo Microsoft C++ Build Tools not found. Install the Desktop development with C++ workload.
    pause
    exit /b 1
)

call "%VS_INSTALL%\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 (
    echo Could not initialize the compiler.
    pause
    exit /b 1
)
set "DISTUTILS_USE_SDK=1"

"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements-build.txt"
if errorlevel 1 (
    echo Could not install Cython.
    pause
    exit /b 1
)

"%~dp0.venv\Scripts\python.exe" "%~dp0setup.py" build_ext --inplace
if errorlevel 1 (
    echo Build failed. Check that Microsoft C++ Build Tools is installed.
    pause
    exit /b 1
)

echo Reader compiled. Run the application with Run.bat.
pause
