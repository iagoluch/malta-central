@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Malta Desktop - Windows

rem Janela nativa do desktop X do celular, sem navegador e sem WebView.
rem A central Flask continua sendo a dona da serial; este app a reaproveita
rem ou inicia uma copia oculta em 127.0.0.1:8666.

where py >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Python Launcher ^(py^) nao encontrado.
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

echo [3/3] Iniciando o Malta Desktop
echo.
echo Log: %LOCALAPPDATA%\malta-central\desktop.log
echo Para encerrar, feche a janela do Malta Desktop.
echo.
if "%~1"=="" (
  start "" ".venv\Scripts\pythonw.exe" malta_desktop.py
  exit /b 0
)

rem Com argumentos (--check, --connect), mantem o console para ver a saida.
".venv\Scripts\python.exe" malta_desktop.py %*
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
  echo.
  echo [ERRO] O Malta Desktop encerrou com codigo %APP_EXIT%.
  pause
)
exit /b %APP_EXIT%

:error
echo.
pause
exit /b 1
