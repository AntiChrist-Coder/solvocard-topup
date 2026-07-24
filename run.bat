@echo off
cd /d "%~dp0"
py -3.14 -m pip install -q -r requirements.txt
py -3.14 app.py %*
