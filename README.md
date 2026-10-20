# 📱 malta-central

**Central de controle Linux para o Motorola Moto E7 Power (XT2097-5, "malta")** — o celular roda
[Alpine Linux nativo](https://alpinelinux.org) direto na partição `boot_a`, sem Android.

Dashboard web que fala com o celular pelo **console serial USB** (`/dev/ttyACM*`) e mantém um
link **RNDIS com internet pelo cabo**: o PC é o gateway/NAT, o celular é `10.55.0.2` e o PC é
`10.55.0.1`. Na prática o celular vira um **WSL de bolso**: shell root, rede, arquivos e
telemetria, controlados pelo navegador do PC.

## Estado validado

Testado ao vivo em 19/10/2026:

- Celular com Alpine, console ACM e RNDIS no mesmo gadget USB.
- MACs fixos: `dev_addr 02:00:00:00:00:02` / `host_addr 02:00:00:00:00:01`.
- PC vê sempre **`enx020000000001`** com `10.55.0.1/24`; celular recebe `10.55.0.2/24`.
- Internet pelo cabo: ping ao gateway, ping `8.8.8.8` e DNS `dl-cdn.alpinelinux.org` por `1.1.1.1`.
- `/data` (f2fs em `/dev/mmcblk0p60`, ~20 GB) montado automaticamente.
- App sob `systemd` (`malta-central.service`), com reconexão serial automática.
- **Reboot do celular testado**: após reiniciar, o app recriou o RNDIS com os MACs fixos,
  reconfigurou IP/rota/DNS, montou `/data` e voltou a `prov=ok` sem intervenção.
- `sysrq b` é o reset que funciona; o `reboot` do BusyBox é ignorado pelo init do aparelho.
- 18 testes automatizados passando (`unittest`).

## O que o painel mostra

| Painel | Conteúdo |
|--------|----------|
| 🔋 Bateria | Tensão, corrente, temperatura, estado de carga e histórico em gráfico |
| ⚙️ Sistema | Kernel, uptime, load, memória, processos, partições |
| 💾 Persistência | `/data` montado, uso e livre; botão para montar/desmontar |
| 🌐 Rede | Interfaces, IP do `rndis0`, gateway, internet e DNS (probe ao vivo) |
| ⌨️ Terminal | Shell root remoto com histórico; limite de 500 caracteres por comando |
| 📁 Arquivos | Upload/download em base64 com validação de caminho |
| 🎛️ Ações | Montar `/data`, `dmesg`, diagnóstico de rede, `df`, `free`, `ps`, temperaturas, kernel, framebuffer, triggers `net`/`mmc` e reboot |
| ⚙️ Configurações | Tema claro/escuro, intervalo de polling, auto-provisionar e auto-montar |
| 📜 Eventos | Ring buffer de eventos do app, visível na UI e no journal do systemd |

A capacidade da bateria reporta `-1` no kernel stock; por isso a UI usa tensão, corrente e
temperatura em vez de percentual.

## Como o link funciona

```
PC (malta-central)                         Celular (malta)
┌───────────────────────────┐   USB       ┌──────────────────────────────┐
│ enx020000000001           │◀───────────▶│ rndis0  10.55.0.2/24         │
│ 10.55.0.1/24              │   ACM +     │ default via 10.55.0.1        │
│ NAT para wlp2s0           │   RNDIS     │ DNS 1.1.1.1 / 8.8.8.8        │
│ Flask 127.0.0.1:8666      │             │ /data f2fs (20 GB)           │
└───────────────────────────┘             └──────────────────────────────┘
```

1. O init do celular sobe o gadget com **ACM** (console serial).
2. O app detecta que `rndis0` não existe e envia o script de provisionamento:
   unbind → cria `rndis.usb0` com MACs fixos → linka em `configs/c.1` → rebind.
   O ACM continua linkado, então o console não é perdido.
3. O celular configura `10.55.0.2/24`, rota default via `10.55.0.1` e `/etc/resolv.conf`.
4. O PC aplica o perfil NetworkManager `malta-rndis-fixed` (IP manual, `never-default`,
   autoconnect prioridade 100).
5. O dispatcher `/etc/NetworkManager/dispatcher.d/90-malta-nat` liga `ip_forward`, ajusta
   `rp_filter` e cria MASQUERADE/FORWARD para a interface RNDIS no uplink atual (`wlp2s0`).
6. O app monta `/data` pelo trigger `/tmp/GO` e, se o supervisor do init não responder,
   tenta o mount f2fs direto como fallback.
7. O probe testa gateway, `8.8.8.8` e DNS; o resultado aparece no card de rede.

Após um reboot do celular, o ciclo 2–6 roda sozinho. O app espera o ACM voltar, recria o RNDIS,
reconecta e só então marca `prov=ok`.

## Instalação e operação

Pré-requisitos no PC: Python 3, `venv`, `flask`, `pyserial`, usuário no grupo `dialout` e
`sudo` para instalar o serviço e o dispatcher.

```bash
cd malta-central
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# serviço já instalado neste PC:
sudo systemctl enable --now malta-central.service
systemctl status malta-central.service
curl -s http://127.0.0.1:8666/api/health
```

O dashboard escuta **somente em `127.0.0.1:8666`**. Abra no navegador do próprio PC.

O serviço fica em `/etc/systemd/system/malta-central.service`, roda como `iagol` com grupo
suplementar `dialout` e `Restart=always`. Ajuste `User`/`Group`/caminhos se o projeto estiver
em outro usuário ou diretório.

### Lado do PC (persistente)

```bash
# perfil do link fixo (já criado):
nmcli connection show malta-rndis-fixed

# dispatcher de NAT (já instalado e habilitado):
ls -l /etc/NetworkManager/dispatcher.d/90-malta-nat
systemctl is-enabled NetworkManager-dispatcher
```

O dispatcher escolhe o uplink pela rota default no momento em que a interface sobe. Se a
Wi-Fi mudar, basta reconnectar a interface RNDIS (ou rodar o dispatcher manualmente) para
recalcular as regras.

### Fallback manual

`reverse_tether_host.sh` (PC) e `reverse_tether_cell.sh` (celular, via serial) são o caminho
manual de emergência. O caminho normal é o app: ele faz o provisionamento e a montagem
automaticamente. O gadget do malta **não tem ECM/NCM**; o script do celular usa RNDIS.

## API

| Método | Rota | Função |
|--------|------|--------|
| GET | `/` | Dashboard |
| GET | `/api/status` | Telemetria, rede, `/data`, histórico, provisionamento e probe |
| GET | `/api/health` | Saúde do app, serial e provisionamento |
| GET | `/api/actions` | Lista de ações disponíveis |
| POST | `/api/action` | Executa ação (`{"action":"df"}`) |
| POST | `/api/cmd` | Terminal (`{"cmd":"uptime"}`; máx. 500 caracteres) |
| GET | `/api/network` | Interfaces, rotas e DNS do celular |
| POST | `/api/network/refresh` | Força novo probe de rede |
| GET/POST | `/api/settings` | Lê/grava tema, polling e flags de automação |
| GET | `/api/logs` | Eventos do app |
| POST | `/api/upload` | Upload base64 (`{"path":"/tmp/x","content":"..."}`) |
| GET | `/api/download?path=...` | Download de arquivo do celular |

Exemplos:

```bash
curl -s http://127.0.0.1:8666/api/status

curl -s -X POST http://127.0.0.1:8666/api/action \
  -H 'Content-Type: application/json' -d '{"action":"df"}'

curl -s -X POST http://127.0.0.1:8666/api/cmd \
  -H 'Content-Type: application/json' -d '{"cmd":"uptime"}'

curl -s -X POST http://127.0.0.1:8666/api/action \
  -H 'Content-Type: application/json' -d '{"action":"provision"}'
```

`provision` força a recriação da função RNDIS com os MACs fixos mesmo que o link já esteja
ativo; útil se a função antiga tiver ficado com MAC aleatório.

### Protocolo serial

O app envia `<comando>; echo __MC<n>__DONE__` e lê até o marcador **em uma linha própria**.
O eco do comando contém o próprio marcador e por isso não dispara a conclusão. A limpeza
remove exatamente o número de caracteres não-newline do comando enviado, o que funciona
mesmo quando a saída repete literais do comando. Um lock serializa as requisições.

## Testes

```bash
cd malta-central
MC_DISABLE_THREADS=1 venv/bin/python -m unittest discover -s tests -v
```

`MC_DISABLE_THREADS=1` é necessário para importar o app sem subir poller/provisionador; os
testes usam uma serial falsa e não precisam do celular.

## Solução de problemas

**App não responde**

```bash
systemctl status malta-central.service
journalctl -u malta-central.service -n 50 --no-pager
curl -s http://127.0.0.1:8666/api/health
```

**Link fixo não aparece**

```bash
ip -br addr show enx020000000001
nmcli connection show --active
nmcli connection show malta-rndis-fixed
```

No terminal do dashboard, confira o gadget:

```sh
G=/sys/kernel/config/usb_gadget/g1
cat $G/UDC
ls -l $G/configs/c.1/
cat $G/functions/rndis.usb0/dev_addr $G/functions/rndis.usb0/host_addr
cat /tmp/rndis.log
```

Se o MAC estiver aleatório, clique em **Provisionar RNDIS** (o `provision` força a recriação).
Se o log mostrar falha ao remover a função, verifique se `rmdir` da função é suportado; o app
também sabe criar a função quando ela não existe (caso normal após reboot).

**Sem internet no celular**

```sh
ip route                     # default via 10.55.0.1?
ping -c 2 10.55.0.1
ping -c 2 8.8.8.8
cat /etc/resolv.conf         # 1.1.1.1 / 8.8.8.8
```

No PC:

```bash
ip route show default
sysctl net.ipv4.ip_forward
sudo /etc/NetworkManager/dispatcher.d/90-malta-nat enx020000000001 up
sudo iptables -t nat -S POSTROUTING
sudo iptables -S FORWARD
```

**Reboot não acontece**

O BusyBox do initramfs ignora o `reboot` simples. A ação do painel usa
`sync; echo b > /proc/sysrq-trigger`, que resetou o aparelho no teste. Se o kernel tiver
`kernel.sysrq=0`, o app escreve `1` antes.

**`df -k /` falha no celular**

É esperado: o rootfs é initramfs e não tem mount point convencional. A UI mostra
rootfs/initramfs; use `/data` para persistência.

## Segurança

- O dashboard dá **shell root** no celular e não tem autenticação; ele escuta só em
  `127.0.0.1`. Não exponha a porta 8666 na rede.
- Para acesso remoto, use um túnel SSH/autenticado ou um proxy com autenticação; não abra a
  porta diretamente.
- A senha de `sudo` usada na instalação foi fornecida apenas para esta sessão e não é
  armazenada no repositório. Troque-a depois da configuração.
- O app usa o servidor de desenvolvimento do Flask; para uso local é suficiente. Para
  produção, coloque um WSGI (waitress/gunicorn) atrás de um proxy autenticado.

## Arquitetura do repositório

```
app.py                  backend Flask, serial, provisionamento, probe, APIs
templates/index.html    dashboard (tema, gráficos, terminal, arquivos)
tests/test_app.py       18 testes com serial falsa
reverse_tether_host.sh  fallback manual do PC
reverse_tether_cell.sh  fallback manual do celular (RNDIS)
requirements.txt        flask, pyserial
```

O serviço systemd e o dispatcher de NAT vivem fora do repositório, em `/etc`; os caminhos
estão documentados acima.

## Contexto do projeto

Bring-up de Linux nativo num Moto E7 Power com bootloader destravado: kernel stock + initramfs
Alpine na partição `boot_a` (rollback = `fastboot flash boot_a boot.img` stock). Descobertas
do bring-up (layout do boot image v2 com DTBO/DTB, major dinâmico 259 das partições, RESTART2
para fastboot remoto, limitações de ops de driver no boot) estão documentadas no repositório
do sistema.

## Licença

MIT — ver [LICENSE](LICENSE).
