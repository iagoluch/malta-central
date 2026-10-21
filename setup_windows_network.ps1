[CmdletBinding()]
param(
    [string]$InterfaceAlias = "",
    [ValidateRange(0, 300)]
    [int]$WaitSeconds = 75,
    [switch]$ElevatedChild
)

$ErrorActionPreference = "Stop"
$HostAddress = "10.55.0.1"
$HostPrefixLength = 24
$NatPrefix = "10.55.0.0/24"
$NatName = "MaltaCentralRNDIS"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    Write-Host "A configuracao da rede USB requer permissao de Administrador." -ForegroundColor Yellow
    try {
        $scriptPath = '"' + $PSCommandPath + '"'
        $argumentLine = '-NoProfile -ExecutionPolicy Bypass -File ' + $scriptPath +
            ' -WaitSeconds ' + $WaitSeconds + ' -ElevatedChild'
        if (-not [string]::IsNullOrWhiteSpace($InterfaceAlias)) {
            $safeAlias = $InterfaceAlias.Replace('"', '')
            $argumentLine += ' -InterfaceAlias "' + $safeAlias + '"'
        }
        $powershellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        $child = Start-Process -FilePath $powershellPath -ArgumentList $argumentLine -Verb RunAs -Wait -PassThru
        exit [int]$child.ExitCode
    }
    catch {
        Write-Host "A elevacao foi cancelada ou falhou: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

$exitCode = 0
try {
    if (-not (Get-Command Get-NetAdapter -ErrorAction SilentlyContinue)) {
        throw "Os cmdlets de rede do Windows (NetAdapter/NetTCPIP) nao estao disponiveis."
    }
    if (-not (Get-Command Get-NetNat -ErrorAction SilentlyContinue)) {
        throw "O cmdlet Get-NetNat nao esta disponivel nesta instalacao do Windows. A central web e a serial podem funcionar, mas o NAT automatico nao pode ser configurado neste sistema."
    }

    Write-Host ""
    Write-Host "Malta Central - configuracao RNDIS + Internet (Windows)" -ForegroundColor Cyan
    Write-Host "O script NAO altera o celular, a imagem Alpine, o boot ou particoes." -ForegroundColor Gray
    Write-Host "Sub-rede interna: $NatPrefix | IP do PC: $HostAddress" -ForegroundColor Gray
    Write-Host ""

    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $candidates = @()
    do {
        if (-not [string]::IsNullOrWhiteSpace($InterfaceAlias)) {
            $candidates = @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq $InterfaceAlias })
        }
        else {
            $candidates = @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue |
                Where-Object {
                    $label = "$($_.Name) $($_.InterfaceDescription)"
                    $label -match "(?i)(remote\s+ndis|rndis|usb.*ethernet|usb.*network|ethernet.*gadget|android.*ethernet|cdc.*ethernet|motorola.*network)"
                })
        }
        if ($candidates.Count -gt 0) { break }
        if ($WaitSeconds -le 0 -or (Get-Date) -ge $deadline) { break }
        Write-Host "Aguardando o adaptador RNDIS do telefone... mantenha a Malta Central aberta."
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)

    if ($candidates.Count -eq 0) {
        throw "Nenhum adaptador RNDIS reconhecido foi encontrado. Mantenha run_windows.bat aberto, conecte o telefone via USB, espere a central provisionar o RNDIS e execute este arquivo novamente. Confira tambem Configuracoes > Rede e Internet > Configuracoes de rede avancadas."
    }

    $adapter = $null
    if ($candidates.Count -eq 1) {
        $adapter = $candidates[0]
    }
    else {
        Write-Host "Mais de um adaptador possivel foi encontrado:" -ForegroundColor Yellow
        for ($index = 0; $index -lt $candidates.Count; $index++) {
            Write-Host ("  {0}. {1} | {2} | {3}" -f ($index + 1), $candidates[$index].Name, $candidates[$index].InterfaceDescription, $candidates[$index].Status)
        }
        $choice = Read-Host "Digite o numero do adaptador RNDIS"
        $selectedIndex = 0
        if (-not [int]::TryParse($choice, [ref]$selectedIndex) -or
            $selectedIndex -lt 1 -or $selectedIndex -gt $candidates.Count) {
            throw "Selecao de adaptador invalida; nenhuma configuracao foi aplicada."
        }
        $adapter = $candidates[$selectedIndex - 1]
    }

    if ($adapter.Status -eq "Disabled") {
        throw "O adaptador '$($adapter.Name)' esta desabilitado. Habilite-o no Windows e execute este arquivo novamente."
    }

    $ifIndex = [int]$adapter.ifIndex
    Write-Host ("Adaptador selecionado: {0} ({1}), estado={2}" -f $adapter.Name, $adapter.InterfaceDescription, $adapter.Status) -ForegroundColor Cyan

    $conflictingAddresses = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $HostAddress -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceIndex -ne $ifIndex })
    if ($conflictingAddresses.Count -gt 0) {
        $where = ($conflictingAddresses | ForEach-Object { "$($_.InterfaceAlias) [$($_.InterfaceIndex)]" }) -join ", "
        throw "O IP $HostAddress ja esta atribuido a outra interface: $where. Para evitar quebrar outra rede, nada foi removido."
    }

    $conflictingRoutes = @(Get-NetRoute -DestinationPrefix $NatPrefix -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceIndex -ne $ifIndex })
    if ($conflictingRoutes.Count -gt 0) {
        $where = ($conflictingRoutes | ForEach-Object { "$($_.InterfaceAlias) [$($_.InterfaceIndex)]" }) -join ", "
        throw "A rota $NatPrefix ja existe em outra interface: $where. Existe conflito de sub-rede; nenhuma rota existente foi alterada."
    }

    # Alteracoes de IP e DHCP sao limitadas ao adaptador RNDIS selecionado.
    Set-NetIPInterface -InterfaceIndex $ifIndex -AddressFamily IPv4 -Dhcp Disabled -ErrorAction Stop
    $addresses = @(Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue)
    foreach ($address in $addresses) {
        if ($address.IPAddress -ne $HostAddress -and $address.IPAddress -notlike "127.*") {
            Remove-NetIPAddress -InterfaceIndex $ifIndex -IPAddress $address.IPAddress -Confirm:$false -ErrorAction SilentlyContinue
        }
    }

    $desired = @(Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -IPAddress $HostAddress -ErrorAction SilentlyContinue)
    if ($desired.Count -eq 0) {
        New-NetIPAddress -InterfaceIndex $ifIndex -IPAddress $HostAddress -PrefixLength $HostPrefixLength -Type Unicast -ErrorAction Stop | Out-Null
    }
    Set-NetIPInterface -InterfaceIndex $ifIndex -AddressFamily IPv4 -Forwarding Enabled -ErrorAction Stop

    $nats = @(Get-NetNat -ErrorAction Stop)
    $nat = $nats | Where-Object { $_.InternalIPInterfaceAddressPrefix -eq $NatPrefix } | Select-Object -First 1
    if (-not $nat) {
        $nameCollision = $nats | Where-Object { $_.Name -eq $NatName } | Select-Object -First 1
        if ($nameCollision) {
            throw "Ja existe uma regra NAT chamada '$NatName' com sub-rede diferente ($($nameCollision.InternalIPInterfaceAddressPrefix)). O script nao altera nem remove NAT existente."
        }
        try {
            $nat = New-NetNat -Name $NatName -InternalIPInterfaceAddressPrefix $NatPrefix -ErrorAction Stop
        }
        catch {
            if ($nats.Count -gt 0) {
                $otherNats = ($nats | ForEach-Object { "$($_.Name): $($_.InternalIPInterfaceAddressPrefix)" }) -join "; "
            }
            else {
                $otherNats = "nenhuma regra NAT previamente enumerada"
            }
            throw "O Windows nao conseguiu criar o NAT $NatPrefix. Nenhuma regra NAT existente foi removida. Regras detectadas: $otherNats. Detalhe: $($_.Exception.Message). Se WSL, Docker, Hyper-V ou outra ferramenta ja administra WinNAT, pode haver conflito."
        }
    }

    $defaultRoute = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceIndex -ne $ifIndex } |
        Sort-Object RouteMetric |
        Select-Object -First 1

    Write-Host ""
    Write-Host "Configuracao aplicada:" -ForegroundColor Green
    Write-Host ("  Adaptador: {0}" -f $adapter.Name)
    Write-Host ("  IP do PC: {0}/{1}" -f $HostAddress, $HostPrefixLength)
    Write-Host ("  NAT: {0} ({1})" -f $nat.Name, $nat.InternalIPInterfaceAddressPrefix)
    if ($defaultRoute) {
        Write-Host ("  Saida padrao do Windows: {0} via {1}" -f $defaultRoute.InterfaceAlias, $defaultRoute.NextHop)
    }
    else {
        Write-Warning "Nenhuma rota padrao externa foi encontrada. O celular nao tera acesso a Internet ate o Windows voltar a ter conexao externa."
    }
    if ($adapter.Status -ne "Up") {
        Write-Warning "O adaptador nao esta Up. A configuracao esta salva; confira cabo USB/gadget RNDIS e execute novamente se necessario."
    }
    Write-Host ""
    Write-Host "Volte a Malta Desktop > Rede e atualize o diagnostico do celular." -ForegroundColor Green
    Write-Host "Se o gateway responder e a Internet nao, confira conexao externa e conflitos com outro NAT."
}
catch {
    $exitCode = 1
    Write-Host ""
    Write-Host ("[ERRO] {0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host "Nenhuma regra NAT preexistente foi apagada." -ForegroundColor Yellow
}
finally {
    if ($ElevatedChild) {
        Write-Host ""
        Read-Host "Pressione Enter para fechar esta janela"
    }
}
exit $exitCode
