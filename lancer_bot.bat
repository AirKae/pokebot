@echo off
REM Double-clique sur ce fichier pour lancer le bot en continu
cd /d "%~dp0"
title PokeBot
python monitor.py --loop 180
pause
