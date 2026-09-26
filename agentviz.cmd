@echo off
rem Run agentviz from a Windows cmd prompt without installing it.
set PYTHONPATH=%~dp0;%PYTHONPATH%
python -m agentviz %*
