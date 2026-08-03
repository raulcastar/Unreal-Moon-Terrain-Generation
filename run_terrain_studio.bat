@echo off
REM Lunar Terrain Studio launcher. Uses the Windows "py" launcher to select the
REM interpreter that has numpy/scipy/matplotlib installed.
setlocal

py "%~dp0Terrain_Studio\terrain_studio.py" %*
if errorlevel 1 (
    echo.
    echo [-] The studio exited with an error.
    echo     If a module is missing, install the dependencies with:
    echo         py -m pip install numpy scipy pandas pyyaml matplotlib tifffile
    echo.
    pause
)

endlocal
