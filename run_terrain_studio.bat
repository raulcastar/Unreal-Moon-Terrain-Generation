@echo off
REM ==========================================================
REM  Lunar Terrain Studio launcher
REM  Uses the Windows Python launcher so the interpreter that
REM  actually carries numpy/scipy/matplotlib is selected.
REM ==========================================================
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
