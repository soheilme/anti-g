@echo off
rem Portable launcher: finds pythonw of any recent Python install
set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not exist "%PYW%" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
if not exist "%PYW%" set "PYW=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not exist "%PYW%" set "PYW=%ProgramFiles%\Python313\pythonw.exe"
if not exist "%PYW%" set "PYW=%ProgramFiles%\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0agtool_gui.py"
