@echo off
setlocal
cd /d "%~dp0"
python build.py %*
if errorlevel 1 (
  echo.
  echo La compilacion ha fallado.
  exit /b 1
)
echo.
echo Ejecutable generado en dist\ExamForge.exe
endlocal
