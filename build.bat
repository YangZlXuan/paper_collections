@echo off
cd /d "%~dp0"
call conda run --no-capture-output -n PaperCollections python build.py
if errorlevel 1 (
  echo Build failed. See the error above.
) else (
  echo EXE created: dist\PaperDownloader.exe
)
pause
