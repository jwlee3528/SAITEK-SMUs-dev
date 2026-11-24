@echo off
cd /d "%~dp0"
set PYTHONPATH=src
python "src\smu_app\main.py" %*
