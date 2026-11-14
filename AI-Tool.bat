@echo off
set PYTHONIOENCODING=utf-8
chcp 65001 >NUL
cd /d "%~dp0"
if "%~1"=="" (python "%~dp0agtool.py" & pause) else (python "%~dp0agtool.py" %*)
