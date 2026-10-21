@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Malta Central - Configurar rede USB

echo Malta Central - configuracao de RNDIS e Internet no Windows
echo O script pode solicitar permissao de Administrador via UAC.
echo Conecte o telefone, mantenha run_windows.bat aberto e aguarde o RNDIS aparecer.
echo Nenhuma imagem, particao ou configuracao de boot do celular sera modificada.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows_network.ps1"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo.
  echo [ERRO] A configuracao de rede nao foi concluida. Veja a mensagem acima.
)
exit /b %RESULT%
