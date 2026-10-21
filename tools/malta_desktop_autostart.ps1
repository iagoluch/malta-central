# Gera o atalho de inicializacao do Malta Desktop na pasta Startup do usuario.
# Nao usa registro, servico, tarefa agendada nem elevacao (UAC).
# Uso: .\malta_desktop_autostart.ps1 -Install|-Remove|-Status
#      .\malta_desktop_autostart.ps1 -Install -StartupPath "C:\...\Startup"
#
# O .cmd na pasta Startup so e gravado se NAO existir ou se for deste projeto
# (marcador de posse). Um "Malta Desktop.cmd" do usuario nao e sobrescrito:
# o app faz a mesma trava em install_autostart (malta_desktop_core.py).
# Sem a trava, o .bat/-Install destruiria um arquivo do usuario e, depois, o
# app se recusaria a remove-lo (remove_autostart exige o marcador).
#
# Codigos de saida:
#   0  tudo certo (ou nada a fazer)
#   2  uso invalido (mais de um de -Install/-Remove/-Status)
#   3  recusado: o arquivo existe e nao foi criado por este projeto

[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Remove,
    [switch]$Status,
    # Pasta Startup a usar no lugar da do usuario (para teste).
    [string]$StartupPath
)

$ErrorActionPreference = 'Stop'

$Marker = 'rem malta-central - Malta Desktop (autostart do usuario)'
$Repo = Split-Path -Parent $PSScriptRoot
$Startup = if ($StartupPath) { $StartupPath } else { [Environment]::GetFolderPath('Startup') }
$Target = Join-Path $Startup 'Malta Desktop.cmd'
$Pythonw = Join-Path $Repo '.venv\Scripts\pythonw.exe'

function Show-Usage {
    Write-Output 'Uso: malta_desktop_autostart.ps1 -Install | -Remove | -Status'
    Write-Output "Pasta Startup do usuario: $Startup"
    Write-Output "Arquivo gerenciado:       $Target"
}

function Get-TargetText {
    # Conteudo do arquivo gerenciado; $null quando ele nao existe.
    if (-not (Test-Path -LiteralPath $Target)) {
        return $null
    }
    $content = Get-Content -LiteralPath $Target -Raw -ErrorAction Stop
    if ($null -eq $content) {
        return ''
    }
    return [string]$content
}

function Test-OwnedByProject {
    # Trava de posse: so o arquivo com o marcador e nosso.
    $content = Get-TargetText
    if ($null -eq $content) {
        return $false
    }
    return [bool]$content.Contains($Marker)
}

function Show-Status {
    if (-not (Test-Path -LiteralPath $Target)) {
        Write-Output 'instalado: nao'
    } elseif (Test-OwnedByProject) {
        Write-Output 'instalado: sim'
    } else {
        Write-Output 'instalado: nao (o arquivo existe mas nao foi criado por este projeto; -Install seria recusado)'
    }
    Write-Output "arquivo: $Target"
}

function Install-Autostart {
    if (-not (Test-Path -LiteralPath $Pythonw)) {
        Write-Output "aviso: $Pythonw ainda nao existe."
        Write-Output 'aviso: rode run_desktop_windows.bat antes do proximo logon.'
    }
    if ((Test-Path -LiteralPath $Target) -and -not (Test-OwnedByProject)) {
        Write-Output "nao instalado: $Target existe e nao foi criado por este projeto."
        Write-Output 'nao instalado: renomeie-o ou remova-o antes de ativar a inicializacao.'
        exit 3
    }
    $lines = @(
        '@echo off',
        $Marker,
        ('cd /d "{0}"' -f $Repo),
        ('start "" "{0}" malta_desktop.py --autostart' -f $Pythonw),
        ''
    )
    $text = ($lines -join "`r`n")
    New-Item -ItemType Directory -Force -Path $Startup | Out-Null
    [IO.File]::WriteAllText($Target, $text, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "instalado: $Target"
    exit 0
}

function Remove-Autostart {
    if (-not (Test-Path -LiteralPath $Target)) {
        Write-Output "nada para remover: $Target"
        exit 0
    }
    if (-not (Test-OwnedByProject)) {
        Write-Output "nao removido: $Target nao foi criado por este projeto"
        exit 3
    }
    Remove-Item -LiteralPath $Target -Force
    Write-Output "removido: $Target"
    exit 0
}

$selected = @($Install.IsPresent, $Remove.IsPresent, $Status.IsPresent) |
    Where-Object { $_ }
if ($selected.Count -gt 1) {
    Write-Error 'use apenas um de -Install, -Remove ou -Status' -ErrorAction Continue
    exit 2
}

if ($Status) {
    Show-Status
} elseif ($Install) {
    Install-Autostart
} elseif ($Remove) {
    Remove-Autostart
} else {
    Show-Usage
    Show-Status
}
exit 0
