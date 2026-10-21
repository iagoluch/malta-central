@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Malta Central - Windows

rem Se a deteccao automatica nao escolher a porta certa, descomente e ajuste:
rem set "MC_SERIAL_PORT=COM5"

where py >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Python Launcher (py) nao encontrado.
  echo Instale Python 3.10+ em https://www.python.org/downloads/windows/
  echo Marque a opcao para instalar o Python Launcher.
  goto :error
)

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Python 3.10 ou superior nao foi encontrado pelo launcher py.
  echo Instale uma versao recente de Python e tente novamente.
  goto :error
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Criando ambiente Python isolado...
  py -3 -m venv ".venv"
  if errorlevel 1 goto :error
)

echo [2/3] Verificando dependencias...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERRO] Nao foi possivel instalar dependencias.
  echo Confira sua conexao e tente executar este arquivo novamente.
  goto :error
)

if not defined MC_PORT set "MC_PORT=8666"
set "MC_OPEN_BROWSER=1"
echo [3/3] Iniciando malta-central em http://127.0.0.1:%MC_PORT%
echo Para encerrar, pressione Ctrl+C nesta janela.
".venv\Scripts\python.exe" app.py
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
  echo.
  echo [ERRO] A central encerrou com codigo %APP_EXIT%.
  pause
  exit /b %APP_EXIT%
)
exit /b 0

:error
echo.
pause
exit /b 1
