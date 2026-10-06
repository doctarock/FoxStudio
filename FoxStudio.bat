@echo off
rem  FoxStudio Desktop launcher -- double-click to run.
rem  Uses the windowless gui-script installed by pip; falls back to pythonw.
set "EXE=%LOCALAPPDATA%\Programs\Python\Python310\Scripts\foxstudio-desktop.exe"
if exist "%EXE%" (
    start "" "%EXE%"
) else (
    start "" pythonw -m foxstudio.gui.app
)
