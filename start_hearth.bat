@echo off
rem Double-click to open Hearth in your browser. Needs Python 3.10+ and Ollama running.
cd /d "%~dp0"
python -m hearth ui
pause
