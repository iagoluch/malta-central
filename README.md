# 📱 malta-central

**Central de controle Linux para o Motorola Moto E7 Power (XT2097-5, "malta")** — o celular que roda
[Alpine Linux nativo](https://alpinelinux.org) direto na partição `boot_a`, sem Android.

Dashboard web que fala com o celular pelo **console serial USB** (`/dev/ttyACM*`) — o mesmo canal
que um humano usaria com `cat /dev/ttyACM0`, mas com interface gráfica, telemetria ao vivo e
terminal remoto. Use o celular **como se fosse um WSL**: shell root completo direto do PC.

## O que mostra

| Painel | Dados |
|--------|-------|
| 🔋 Bateria | Tensão (V), corrente (±mA), temperatura, estado de carga + **gráfico ao vivo** |
| ⚙️ Sistema | Kernel, uptime, load average, uso de memória, partições eMMC |
| 💾 Persistência | `/data` (f2fs da userdata, 20 GB) montado?, uso, livre |
| 🌐 Rede | Todas as interfaces (`ccmni*` do modem, `lo`, etc.) com estado UP/DOWN |
| ⌨️ Terminal | Shell root remoto completo — o celular vira um "WSL de bolso" |

## Ações de um clique

- **Trigger mmc** — monta `/dev/mmcblk0p60` (f2fs) em `/data` (bloco dormante do init)
- **Trigger net** — sobe as interfaces `ccmni0-3` do modem
- **Trigger fb** — ativa o framebuffer `/dev/fb0`
- **dmesg** — últimas 40 linhas do kernel do celular
- **Reboot** — reinicia (o sistema volta sozinho em ~15s)

## Como usar

```bash
git clone https://github.com/iagoluch/malta-central.git
cd malta-central
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py
# abre http://localhost:8666
```

Requisitos do lado do celular (o "malta Linux"):
- Alpine 3.24 + kernel stock 4.9.190 bootando da `boot_a`
- Shell root interativo no gadget serial (`ttyGS0` → `/dev/ttyACM0` no PC)
- init com blocos dormantes acionáveis via `/tmp/GO` ( mmc | fb | net )

O app **auto-detecta** `/dev/ttyACM0` ou `/dev/ttyACM1` e **reconecta sozinho** quando o cabo
USB é desconectado/reconectado — sem reiniciar nada: no máximo um ciclo de polling (3s) depois
do replug o painel volta ao vivo. Também sobrevive ao celular rebootar (o serial volta com ele).

## Arquitetura

```
PC (malta-central)                    Celular (malta)
┌──────────────────┐   USB serial    ┌──────────────────────────┐
│ Flask :8666      │◀──ttyACM0──────▶│ busybox sh root @ ttyGS0 │
│  └ poller 3s     │   115200 baud   │  kernel 4.9.190 stock    │
│  └ /api/cmd      │                 │  /data f2fs (20GB)       │
│  └ /api/status   │                 │  ccmni0-20 (modem)       │
└──────────────────┘                 └──────────────────────────┘
```

Protocolo serial: envia `<cmd>; echo __MC<n>__DONE__`, lê até o marcador, limpa o eco.
Um lock serializa o acesso — uma requisição por vez.

## Contexto do projeto

Bring-up de Linux nativo num Moto E7 Power com bootloader destravado: kernel stock + initramfs
Alpine na partição `boot_a` (rollback = `fastboot flash boot_a boot.img` stock). As descobertas
do bring-up (layout do boot image v2 com DTBO/DTB, major dinâmico 259 das partições, RESTART2
para fastboot remoto) estão documentadas no repositório do sistema.

## Licença

MIT — ver [LICENSE](LICENSE).
