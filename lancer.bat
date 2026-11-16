@echo off
cd /d "%~dp0"
echo Dossier : %cd%
if not exist app.py goto noapp
set PY=
py -c "import sys" >nul 2>nul
if not errorlevel 1 set PY=py& goto trouve
python -c "import sys" >nul 2>nul
if not errorlevel 1 set PY=python& goto trouve
echo Python est introuvable. Installe-le depuis python.org en cochant Add Python to PATH.
goto fin
:trouve
echo Installation des outils necessaires...
%PY% -m pip install --upgrade flask yt-dlp imageio-ffmpeg
echo Lancement. Ferme cette fenetre pour arreter l'outil.
%PY% app.py
goto fin
:noapp
echo app.py est introuvable. Mets-le dans le meme dossier que ce fichier.
:fin
echo.
pause
