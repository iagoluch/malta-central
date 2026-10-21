# malta-central v3.2

**Desktop web local para administrar o Alpine Linux nativo do Motorola Moto E7 Power
(XT2097-5, "malta")** — shell root, arquivos, diagnóstico, catálogo e um assistente de IA
que apenas sugere comandos.

O celular roda Alpine direto na partição `boot_a`, sem Android, e é acessado pelo **console
serial USB** (`/dev/ttyACM*`) e por um link **RNDIS com internet pelo cabo**: o PC é o
gateway/NAT (`10.55.0.1`), o celular é `10.55.0.2`. A central roda no PC e escuta **somente
em `127.0.0.1:8666`**.

Além do painel web, o projeto entrega o **Malta Desktop**: o desktop X real do celular
(Alpine ARMv7) numa **janela nativa do Windows**, sem navegador, sem WebView e sem
Electron — só um cliente RFB/VNC próprio desenhando o framebuffer num `Canvas` do Tk. Ver
[Malta Desktop](#malta-desktop--desktop-x-do-celular-numa-janela-do-windows).

> Esta versão entrega a interface desktop (shell, explorador/editor, ferramentas, catálogo e
> assistente) e o **Malta Desktop** (janela nativa do Windows com o desktop X do celular).
> Boot, kernel, initramfs, partições, bootloader e o mecanismo serial/RNDIS existente
> **não foram alterados**.

## Malta Desktop — desktop X do celular numa janela do Windows

O **Malta Desktop** é o desktop X real do Moto E7 Power (Alpine ARMv7) visto numa
**janela nativa do Windows**: uma `Toplevel` do Tk com o framebuffer desenhado num
`Canvas`, 1:1, sem redimensionamento. Não é navegador, não é WebView, não é Electron — o PC
apenas desenha os pixels que o celular produz. A imagem chega por **RFB 3.8 (VNC)** sobre o
próprio link USB RNDIS, e o teclado e o mouse do PC viajam como eventos X11 para o lado do
Alpine.

Quem roda o ambiente gráfico é o **celular**, não o PC. No aparelho, um `Xvnc` do TigerVNC
sustenta a sessão X e **escuta direto na interface USB** (`10.55.0.2:5901`): não há proxy
`socat` no meio.

### Como o fluxo funciona

```
┌─────────────────────── PC (Windows) ───────────────────────┐
│ malta_desktop.py (Tk)         malta_rfb.py (RFB 3.8)       │
│  └ malta_desktop_core.py        ├ VncAuth (DES d3des)      │
│     • estados do desktop        ├ RGB565 → Pillow          │
│     • upload do bundle          └ teclado/mouse → X11      │
│     • cliente da API da central                            │
│                                → Canvas Tk (1:1)           │
└───────────────┬─────────────────────────────┬──────────────┘
               │ HTTP 127.0.0.1:8666        │ TCP 10.55.0.2:5901
               │ (app.py é o dono da serial) │ (só pela interface USB)
               ▼                             ▼
┌───────────────┬─────────────────────────────┬────────────────┐
│ app.py (Flask) — dono da serial                            │
│   POST /api/cmd          ──▶ malta-desktop.sh start        │
│   POST /api/files/upload ──▶ bundle em /data/malta-desktop │
│                                                            │
│                             │                              │
│                             ▼                              │
│                    Xvnc :1 1024x600x16                      │
│                    (-interface 10.55.0.2:5901)              │
│                             │                              │
│                             ▼                              │
│          openbox · tint2 · pcmanfm · xterm · jgmenu        │
│          (chroot em /data/malta-desktop/root)              │
│                                                            │
│ /data (f2fs, ~20 GB) — única persistência                  │
└────────────────────────────────────────────────────────────┘
```

São **dois planos no mesmo cabo**:

- **Controle (serial).** O `app.py` continua sendo o **único dono da porta serial**. O app
  do Windows fala com ele por HTTP em `127.0.0.1:8666` (`GET /api/health`,
  `GET /api/status`, `POST /api/cmd`, `POST /api/files/upload`), e o aparelho executa
  `sh /data/malta-desktop/malta-desktop.sh start|stop|status|measure|doctor|log`.
- **Dados (RNDIS).** O framebuffer trafega direto do PC para `10.55.0.2:5901`, sem passar
  pela central e sem passar pela serial. É a mesma interface USB, outra rota.

**Repartição de papéis:** se a central já estiver rodando, o app do Windows **a
reaproveita**; se não estiver, sobe uma cópia oculta (`app.py` com `MC_OPEN_BROWSER=0`) e
a encerra ao sair — mas só encerra a central que ele mesmo subiu. O painel web continua
existindo como **diagnóstico secundário**, separado da janela do desktop (botão
**Abrir painel web**).

> **A central Flask é a dona da porta serial**, e a serial é um recurso único. **Não abra o
> painel web e o Malta Desktop ao mesmo tempo**: duas centrais disputam a mesma porta
> COM. O Malta Desktop **reaproveita** a central que estiver rodando e só inicia uma cópia
> oculta quando não há nenhuma.

### O que roda no celular

O rootfs do aparelho é um **initramfs em RAM** e a única persistência é `/data` (f2fs em
`/dev/mmcblk0p60`, ~20 GB). Por isso o ambiente gráfico **não** é instalado no rootfs: ele
vive num **chroot Alpine persistente em `/data/malta-desktop/root`**, criado com
`apk --root` usando a branch do próprio aparelho (`/etc/alpine-release`) e as chaves de
`/etc/apk/keys` do host — sem `--allow-untrusted`, sem repositório inventado.

| Componente | Versão | Função |
|------------|--------|--------|
| `tigervnc` (Xvnc) | 1.16.2 | servidor X com VNC embutido; guarda o framebuffer em RAM |
| `openbox` | 3.6.1 | gerenciador de janelas (WM) |
| `tint2` | 17.1.3 | painel do topo (relógio, tarefas, 3 launchers) |
| `jgmenu` | 4.5.0 | menu de aplicativos (painel e `Alt+F2`) |
| `pcmanfm` | 1.3.2 | área de trabalho (papel de parede + ícones) e gerenciador de arquivos |
| `xterm` | 410 | terminal |
| `font-dejavu` | 2.37 | fonte do painel, do menu e do terminal |
| `hicolor-icon-theme` | branch v3.24 | tema de ícones |
| `xset`, `xsetroot`, `setxkbmap` | branch v3.24 | teste do X, papel de parede e layout de teclado (`br`) |
| `xkeyboard-config`, `xkbcomp` | branch v3.24 | mapas de teclado do X |
| `busybox`, `alpine-baselayout` | branch v3.24 | base do chroot |

Os componentes com versão fixada na tabela foram conferidos um a um no APKINDEX e em APKs
reais de **armv7** (Alpine v3.24, `main` e `community`). Os demais seguem a branch do
aparelho (derivada de `/etc/alpine-release`) e não têm versão fixada no repositório.

O `socat` **saiu da lista**: o `Xvnc` escuta direto na interface USB e não há proxy no meio.
Com isso, o pacote sumiu do instalador, do `doctor` e do `measure` — a chave `SOCAT_PID`
continua no contrato do `status` (valendo sempre `0`) só para não quebrar a leitura do PC.

`lxpanel` **não existe** para armv7 — daí a escolha de `tint2` (painel) + `jgmenu` (menu)
em vez de uma suíte LXDE. Não há XFCE, GNOME, KDE, Electron ou Chromium em lugar nenhum.

**A sessão X**, com o que roda dentro do chroot:

```
Xvnc :1 -geometry 1024x600 -depth 16 -rfbport 5901 -rfbauth /root/.vnc/passwd -SecurityTypes VncAuth -interface 10.55.0.2
```

O `Xvnc` escuta **direto na interface USB** (`10.55.0.2:5901`): o VNC **nunca** aparece em
`0.0.0.0` nem na interface Wi-Fi, e **não há `socat`/proxy** no meio.

> **Por que `-interface` e não `-localhost`.** No TigerVNC 1.16, `-localhost` usa
> `createLocalTcpListeners()`, que **não devolve endereço/porta** neste aparelho: o `Xvnc`
> aborta com `vncExtInit: No path or port configured for incoming connections`. Com
> `-interface 10.55.0.2` (que usa `createTcpListeners()`) o mesmo `Xvnc` sobe normalmente.
> É uma limitação medida no Moto E7 Power, não uma preferência de estilo.

A senha VncAuth é um arquivo de **8 bytes** no formato d3des do TigerVNC, gerado pelo próprio
PC e reenviado ao aparelho a cada instalação.

A sessão (`device/session.sh`) sobe `openbox` + `tint2` (painel aubergine `#2c001e`,
relógio, tarefas e 3 launchers com ícones XPM próprios) + `pcmanfm --desktop` (papel de
parede `#300a24` e ícones na área de trabalho) + `xterm` (fonte DejaVu). O menu de
aplicativos é o **jgmenu** (botão do painel e `Alt+F2`). Atalhos: `Alt+Return` = terminal,
`Alt+F2` = menu de aplicativos, `Alt+F1` = menu raiz do openbox, `Alt+F4` = fecha a janela,
`Alt+Tab` = troca de janela, `Alt+espaço` = menu da janela em foco.

**No Windows** existem três arquivos:

- `malta_desktop.py` — a janela de controle (Tk, pt-BR);
- `malta_desktop_core.py` — máquina de estados, cliente da API da central e autostart;
- `malta_rfb.py` — o **cliente RFB 3.8 próprio**: handshake, VncAuth com DES d3des
  implementado sem dependências, encodings `Raw`/`CopyRect`/`DesktopSize`, render em Pillow
  e teclado/mouse com keysym X11.

Não há **nenhum binário externo**: o visualizador oficial do TigerVNC para Windows foi
descartado porque o download direto do SourceForge retornou HTTP 403 nesta rede, e porque o
formato do arquivo de senha do visualizador oficial varia por versão. Um cliente próprio
elimina a dependência, o processo extra e o problema do formato da senha.

### Instalação real no aparelho (medida)

O `install.sh` foi executado **no Moto E7 Power de verdade** (root, sem container). Números
medidos nessa execução:

| Dado | Valor medido |
|------|--------------|
| Branch do chroot | Alpine **v3.24**, `armv7` |
| Pacotes instalados | **163** (15 explícitos + dependências) |
| Tamanho do chroot | **309,2 MiB** em `/data/malta-desktop/root` |
| Tempo de instalação | **~90 s** |
| Persistência | `/data` — o rootfs do aparelho é initramfs em RAM |
| Versão do bundle | **md5 do `install.sh`** (fonte única, PC e aparelho) |

A lista explícita de pacotes é a variável `PACKAGES` do `device/install.sh`:

```sh
alpine-baselayout busybox openbox tint2 jgmenu pcmanfm xterm font-dejavu \
hicolor-icon-theme tigervnc xset xsetroot setxkbmap xkeyboard-config xkbcomp
```

O `socat` **não está** na lista: o VNC não passa por proxy.

O instalador é idempotente (rodar de novo reusa o chroot) e grava em
`/data/malta-desktop/version` o **md5 do `install.sh` instalado**. É esse o contrato de
versão entre o PC e o aparelho: o PC calcula o md5 do `device/install.sh` local e compara —
divergência significa "reinstalar", e os dois lados leem o mesmo arquivo, então não há
constante que possa divergir.

### Por que esta arquitetura minimiza RAM e CPU

- **Chroot em `/data`, não no rootfs.** O rootfs do aparelho é RAM: instalar o ambiente
  gráfico ali consumiria memória à toa, e a RAM é o recurso escasso de um Moto E7 Power. No
  chroot em `/data` (f2fs, ~20 GB) os binários ficam em disco e **só os processos em
  execução pesam na RAM** — depois de instalado, nada mais é baixado.
- **Componentes mínimos em vez de uma DE.** XFCE/GNOME/KDE trariam dezenas de processos
  (painel, gerenciador de arquivos, `dbus`, agente de notificações, daemon de rede…). Aqui a
  sessão inteira é **cinco processos**: `Xvnc`, a sessão (`sh`), `openbox`, `tint2` e
  `pcmanfm` — mais o `xterm`, só quando aberto. Não há proxy: o `socat` saiu da arquitetura.
- **Sem Electron/WebView/Chromium.** Um Electron no aparelho carregaria o runtime do
  Chromium inteiro; um Chromium headless, idem. Nenhum dos dois está aqui: o PC desenha.
- **Sem VNC no navegador (noVNC).** O noVNC é JS + canvas rodando no navegador do PC:
  gasta CPU do PC, acrescenta latência de canvas e ainda precisaria de um servidor no
  aparelho. O objetivo era um desktop real numa janela nativa, não uma página com um
  framebuffer.
- **Cliente RFB próprio em vez do visualizador TigerVNC para Windows.** Elimina o download
  bloqueado nesta rede, o instalador de terceiros, o processo extra e a incompatibilidade de
  formato de senha. A única dependência nova é o **Pillow**, que já é usado para decodificar
  o framebuffer.
- **1024×600×16.** O `depth 16` (RGB565) corta o framebuffer pela metade em relação a 32
  bpp: menos RAM no `Xvnc`, menos bytes por atualização e decodificação mais barata no PC.
  Uma atualização de tela cheia são ~1,2 MB; o modo incremental e o `CopyRect` mantêm o
  tráfego real bem abaixo disso.
- **`-interface 10.55.0.2` no Xvnc.** O VNC fica preso à interface USB: nada é exposto por
  engano na interface Wi-Fi, e não há processo extra (o proxy) no meio do caminho.

### Requisitos e instalação no Windows

1. **Python 3.10 ou superior**, instalado pelo [python.org](https://www.python.org/downloads/windows/)
   com o Python Launcher (`py`) marcado. O `Pillow` entra sozinho pelo `requirements.txt`
   (`flask`, `pyserial`, `pillow`).
2. O projeto em uma pasta gravável.
3. Dê duplo clique em **`run_desktop_windows.bat`**. Na primeira execução o script cria
   `.venv`, instala as dependências e abre a janela do Malta Desktop. O script também aceita
   argumentos: `run_desktop_windows.bat --check` imprime o estado do desktop em JSON e sai
   com código 0 **somente** quando o desktop está pronto.
4. *(Opcional)* Para o monitor do desktop subir junto com o Windows:

   ```bat
   tools\malta_desktop_autostart.bat -Install
   ```

   Isso grava um `.cmd` na pasta **Startup** do usuário
   (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Malta Desktop.cmd`) que chama
   `malta_desktop.py --autostart` (minimizado, com o monitor ligado). **Sem registro, sem
   serviço, sem tarefa agendada e sem elevação (UAC).** O mesmo efeito tem o checkbox
   **Iniciar o monitor com o Windows** na janela. Para desfazer:

   ```bat
   tools\malta_desktop_autostart.bat -Remove
   ```

   `-Status` mostra o estado sem alterar nada.

   O arquivo é gravado **com uma assinatura** (`rem malta-central - Malta Desktop (autostart
   do usuario)`), e o **app recusa sobrescrever um `Malta Desktop.cmd` que ele não criou**:
   se já existir um arquivo seu na pasta Startup, o app avisa para renomear ou remover em vez
   de perdê-lo em silêncio. O `-Remove` também só apaga o arquivo que o projeto criou. (O
   `tools\malta_desktop_autostart.bat -Install` é o caminho direto do prompt e **escreve o
   arquivo sem essa trava**.)

### Primeira conexão

1. **Conecte o celular por USB** (cabo de dados, não só de carga) e abra o Malta Desktop.
2. A janela de controle mostra **Celular: porta COMx · RNDIS 10.55.0.2**. Se aparecer
   *Celular sem /data ou sem RNDIS*, falta o passo 3.
3. **Rode `setup_windows_network.bat`** (o mesmo do painel web): ele pede elevação UAC e
   configura o IP estático **10.55.0.1/24** no PC, na interface RNDIS, e o NAT de saída.
   Rode-o **com a central em execução** — o script espera o adaptador RNDIS aparecer.
4. **Instalar/Reparar desktop.** O app envia o bundle (`device/**` mais o arquivo de senha
   de 8 bytes) por `POST /api/files/upload`, cria os diretórios antes e dispara o `install.sh`
   em segundo plano no aparelho. Acompanhe o progresso no log da janela: as etapas são
   `preflight → mounts → apk → config → session → vnc → verify → done`, lidas de
   `run/install.status`. **Esta é a única etapa que precisa de internet**: o `apk` baixa os
   pacotes do espelho Alpine pelo RNDIS. **No aparelho real a instalação levou ~90 s**
   (163 pacotes, 309,2 MiB); a própria tela avisa se o `apk` falhar por falta de rota/DNS.

   O **limite de upload não é um número fixo**: o app lê o limite efetivo da central em
   `GET /api/settings` (`transfer_upload_kb` — 32 KB por padrão, **64 KB de teto rígido**) e
   obedece ao que está em vigor. Se o bundle não couber no limite em vigor, o app **eleva o
   limite da central para 64 KB** e registra isso explicitamente no log
   (`limite de upload da central elevado para 64 KB para acomodar o bundle do desktop`);
   só falha quando nem o teto cabe. Na prática a elevação é necessária: o `device/malta-desktop.sh`
   tem ~32,8 KB e não passa no padrão de 32 KB.

   O aparelho grava em `/data/malta-desktop/version` o **md5 do `install.sh` que recebeu**, e o
   PC compara com o md5 do `device/install.sh` local. Os dois lados leem o mesmo arquivo: não
   há constante que possa divergir, e por isso o app **não entra em laço de reinstalação** a
   cada sondagem.
5. **Conectar desktop.** O app **não abre uma conexão TCP crua** no `10.55.0.2:5901` para
   checar se o desktop está de pé. A prontidão vem do **estado que o próprio aparelho
   publica**: `STATE=running` **e** `X_READY=1`. A presença do cabo não é confundida com
   desktop disponível, e nenhuma conexão descartável chega ao `Xvnc`. A janela
   **Alpine Linux — malta** abre com o framebuffer 1024×600.

   O `SOCAT_PID` **deixou de ser gate**: com o proxy fora do caminho ele vale sempre `0`, e um
   gate nele diria "desktop parado" mesmo com a sessão no ar. Aparelhos com o **script
   antigo** (que não publicam `X_READY` no `status`) continuam usando o critério antigo
   (`STATE=running` + `SOCAT_PID` ≠ `0`) e a sonda TCP de reserva.

Depois de instalada, a sessão funciona **sem internet**: o X, o painel e os programas são
locais e o VNC trafega só pela interface USB. A internet pelo cabo é necessária apenas
durante o `apk` da instalação (e para o assistente de IA, se você usá-lo).

### Uso

São **duas janelas**:

- **Janela de controle** (`Malta Desktop`): estado do aparelho, botões, checkboxes e o log.
- **Janela do desktop** (`Alpine Linux — malta`): o framebuffer do X, desenhado 1:1 num
  `Canvas`, com uma barra de status na parte de baixo.

| Controle | O que faz |
|----------|-----------|
| **Conectar desktop** | abre a sessão VNC; instala (quando o md5 do `device/install.sh` local difere do que o aparelho gravou em `/data/malta-desktop/version`) e inicia o desktop no aparelho quando ele ainda não está no ar |
| **Desconectar** | fecha a sessão VNC; o desktop **continua rodando** no aparelho |
| **Instalar/Reparar desktop** | reenvia o bundle, roda o `install.sh` e sobe o desktop |
| **Parar desktop** | encerra o `Xvnc` e a sessão no aparelho (e desliga a conexão automática); o `stop` **confirma que o Xvnc morreu** |
| **Medir memória** | mede PSS/RSS por processo (ver abaixo) |
| **Diagnóstico** | roda o `doctor` no aparelho e devolve linhas PASS/FAIL (ver abaixo) |
| **Abrir painel web** | abre o painel em `http://127.0.0.1:8666/` no navegador |
| *Conectar automaticamente quando o celular aparecer* | ligado por padrão; o monitor (a cada 3 s) reabre a sessão quando o desktop volta |
| *Iniciar o monitor com o Windows* | instala/remove o `.cmd` da pasta Startup |

O monitor **não abre conexão TCP** para decidir se o desktop está de pé: ele pergunta ao
aparelho (`STATE` e `X_READY`). A sonda TCP crua ficou só como **reserva rara** (no máximo
**1× a cada 30 s**) e só para aparelhos cujo `status` ainda não traz `X_READY` (script
antigo). Isso não é detalhe: cada conexão que morre antes do handshake chega direto ao
`Xvnc` e conta como **falha de login** para o TigerVNC, que bloqueia o endereço de origem
por 1200 s e derruba a sessão legítima logo depois.

**Teclado e mouse.** O `Canvas` do Tk captura `<KeyPress>`, `<KeyRelease>`, `<Motion>` e os
botões 1/2/3, e converte tudo em mensagens RFB: `KeyEvent` com keysym X11 e `PointerEvent`
com a máscara de botões. Os modificadores são enviados na ordem certa (o `Alt`/`Ctrl`/`Shift`
são pressionados e soltos em volta da tecla), e a roda do mouse vira os botões 4 e 5. O
ponteiro é desenhado no framebuffer pelo próprio X do Alpine.

**Logs.** A janela mostra as últimas 2.000 linhas em tempo real. O mesmo log fica em
`%LOCALAPPDATA%\malta-central\desktop.log` (é reiniciado quando passa de 1 MB). Ao lado,
`%LOCALAPPDATA%\malta-central\desktop.json` guarda a **senha VNC em texto claro** — ela é
gerada uma vez e reutilizada; é o arquivo a apagar na remoção. No Windows, o `chmod(0600)`
**não** protege esse arquivo (o ACL é o do perfil do usuário): ver
[Segurança](#segurança).

### Medir e diagnosticar

**Medir memória** (`malta-desktop.sh measure`) devolve uma linha de total e uma linha por
processo. No **Moto E7 Power real** (ARMv7, 1,78 GB de RAM):

```
RSS total 114,6 MiB · PSS total 73,5 MiB · memória <livre> de <total> · disponível <%>
  Xvnc: RSS 44,2 MiB · PSS 30,5 MiB
  pcmanfm: RSS 49,0 MiB · PSS 32,0 MiB
  openbox: RSS 10,9 MiB · PSS 5,9 MiB
  tint2: RSS 9,6 MiB · PSS 4,6 MiB
```

O script separa **PSS de RSS**, separa a memória do ambiente gráfico da memória basal do
sistema (`MEMTOTAL_KB`, `MEMAVAILABLE_KB`, `BASELINE_MEMAVAILABLE_KB`, `BASELINE_DELTA_KB`)
e lista uma linha `PROC=<nome> RSS_KB=<n> PSS_KB=<n>` por processo. PSS (soma de `Pss:` do
`smaps`) é a métrica honesta: com memória compartilhada, a soma das RSS de cada processo
conta os mesmos bytes várias vezes.

O `measure` lê o `smaps` de cada processo **uma única vez** e guarda em cache por PID. Não é
cosmético: o `/api/cmd` da central tem **timeout fixo de 15 s** e a serial é de **115200 baud**,
e o kernel 4.9 do aparelho não tem `smaps_rollup` — o fallback é o `smaps` inteiro, que
custou segundos por processo. Sem o cache, o mesmo arquivo era lido de novo para cada total e
para cada linha `PROC=`, e a medição estourava o timeout da serial.

**Diagnóstico** (`malta-desktop.sh doctor`) devolve uma linha por verificação e um resumo:

```
PASS root
PASS data
PASS base
PASS binarios
PASS configs
PASS vncpasswd
PASS mounts
PASS espaco
PASS rndis
PASS xvnc
PASS x
PASS vnc
PASS sessao
PASS versao
OK=14
FALHAS=0
RESULT=PASS
```

As verificações são: `root`, `data`, `base`, `binarios`, `configs`, `vncpasswd`, `mounts`,
`espaco`, `rndis`, `xvnc`, `x`, `vnc`, `sessao` e `versao` — **14 no total**. O `doctor`
rodado **no aparelho real** devolveu **14 PASS / 0 FAIL**. A verificação `vnc` confere a
porta 5901 escutando **em `10.55.0.2`** (o Xvnc escuta direto na interface USB: não há
proxy no meio).

**Logs no aparelho.** Pelo **Terminal** do painel de diagnóstico (a ferramenta secundária) ou
por qualquer shell root:

```sh
sh /data/malta-desktop/malta-desktop.sh log 40      # últimas linhas do desktop.log
cat /data/malta-desktop/run/install.status           # STATE/STEP/TS/ERROR do instalador
cat /data/malta-desktop/logs/install.log             # saída do install.sh
cat /data/malta-desktop/run/desktop.status           # estado persistido do desktop
cat /data/malta-desktop/root/tmp/session.log         # log da sessão X
```

### Memória medida — e o que ela não prova

Medido **no Moto E7 Power real** (Alpine 3.24 **armv7**, 1,78 GB de RAM) com o `measure`
real do projeto, com o desktop em repouso:

| Processo | PSS | RSS |
|----------|-----|-----|
| `Xvnc` | 30,5 MiB (31.188 KB) | 44,2 MiB (45.208 KB) |
| `pcmanfm` | 32,0 MiB (32.746 KB) | 49,0 MiB (50.208 KB) |
| `openbox` | 5,9 MiB (6.072 KB) | 10,9 MiB (11.120 KB) |
| `tint2` | 4,6 MiB (4.684 KB) | 9,6 MiB (9.844 KB) |
| **total** | **73,5 MiB (75.226 KB) PSS** | **114,6 MiB (117.408 KB) RSS** |

`MemAvailable` do sistema no momento da medição: **1.688.516 KB**. Com ~1,78 GB de RAM no
aparelho, o ambiente gráfico consome **~130 MB** — cerca de **7 %** da memória total.

A meta do projeto é **≤ ~150 MiB de RAM residente em repouso**: ela é **atingida**. O PSS
(73,5 MiB) fica bem abaixo e o RSS somado (114,6 MiB) também. PSS (soma de `Pss:` do
`smaps`) é a métrica honesta: com memória compartilhada, a soma das RSS de cada processo
conta os mesmos bytes várias vezes.

**Comparação honesta com o container.** A medição anterior, feita em **container Alpine
3.24 x86_64** (Docker + WSL2), dava **PSS ~99,5 MB** para o mesmo stack. No ARMv7 do celular
o mesmo stack sai **~24 MB mais leve** (73,5 MiB de PSS, sem proxy e com o `xterm` fechado).
Os dois números são reais, mas **o do ARMv7 é o válido**: o do container serve só como
referência de ordem de grandeza. O ARMv7 não é "provavelmente menor" — foi medido.

### Limitações conhecidas

- **Sem clipboard** (copiar/colar do host para o desktop não funciona) e **sem áudio**. O
  `ServerCutText` do RFB é drenado e descartado.
- `Alt+Tab`, `Ctrl+Alt+Del` e `Win+L` são **capturados pelo Windows** antes de chegarem ao
  Tk. Dentro do desktop Alpine o `Alt+Tab` funciona normalmente.
- O **botão direito na área de trabalho não abre o menu do openbox**: o `pcmanfm --desktop`
  ocupa a root window, e o clique direito nunca chega ao openbox. O menu de aplicativos é o
  **jgmenu** (botão do painel ou `Alt+F2`); o **menu raiz do openbox** continua acessível
  pelo `Alt+F1` (ou pelo botão do openbox na barra de título das janelas). O `Alt+espaço`
  abre o *client-menu* da janela em foco, que é outra coisa.
- Os **ícones do menu do jgmenu** dependem de um tema de ícones: o `hicolor` do GTK vem com
  `Hidden=true`. O painel mostra os ícones (XPM próprios do projeto); o menu pode ficar só
  com texto.
- `measure`/`doctor` leem `/proc` com **2 forks por PID**, mas a varredura é cacheada: **um
  único passe** por invocação do script, e o `smaps` de cada processo é lido **uma vez**. No
  Windows isso pesa na suíte de testes; no aparelho o custo real é o `smaps` inteiro (sem
  `smaps_rollup` no kernel 4.9), que é o motivo do cache.
- O cliente do PC é um VNC em Python: em framebuffer cheio (1024×600×16 bpp) trafegam
  **~1,2 MB por atualização**. O modo incremental e o `CopyRect` mantêm o tráfego baixo, mas
  é o principal custo de banda sobre o RNDIS.

### Bugs reais encontrados no caminho

Vale registrar porque explicam por que existe o gate de validação:

- O **DES do VncAuth estava errado**: o VNC usa d3des com os bits da chave invertidos, e o
  TigerVNC recusava **toda** a conexão e ainda blacklista o IP. Corrigido espelhando cada
  byte da chave; os vetores passaram a bater com o binário real do `vncpasswd`.
- O `stop` **deixava `Xvnc` (e o proxy do bundle antigo) vivos** (um `case` com alternância
  vinda de variável, que o shell não interpreta como alternância). O desktop continuava
  acessível depois de "Parar desktop" e o `measure` subcontava. Corrigido comparando padrão a
  padrão; hoje o `stop` ainda **confirma que o `Xvnc` morreu**.
- **`/dev/pts` e `/dev/shm` não montados no chroot** matavam o `xterm` ("not enough ptys").
- O **menu jgmenu nascia ilegível**: o CSV de aplicativos não existia, o `XDG_CONFIG_DIRS`
  não tinha `/etc/xdg` e a opção de cor `color_menu_fg` não existe no jgmenu 4.5 (é
  `color_norm_fg`) — um nome desconhecido é ignorado em silêncio e o texto saía quase
  invisível.
- O **redirecionamento de log do instalador quebrava em aparelho limpo**, porque o
  diretório era criado depois do redirecionamento.
- O app **sondava a porta 5901 com uma conexão TCP crua** a cada 3 s para decidir se o
  desktop estava de pé. Cada conexão que morre antes do handshake chega ao `Xvnc` e conta como
  **falha de login** do endereço de origem: o TigerVNC **blacklista o endereço de origem** e,
  expirado o bloqueio (1200 s ≈ 20 min), recusa também o cliente legítimo que chega pelo
  mesmo caminho. A prontidão passou a vir do **estado publicado pelo aparelho**
  (`STATE=running` + `X_READY=1`), e a conexão crua ficou só como reserva rara (1× a cada
  30 s) para aparelho com script antigo.
- A **versão do bundle** era uma constante ("1") escrita dos dois lados: qualquer divergência
  fazia o app **reinstalar o bundle a cada sondagem**. Agora o aparelho grava o **md5 do
  `install.sh` que recebeu** e o PC compara com o md5 do `device/install.sh` local — os dois
  lados leem o mesmo arquivo, então não há laço de reinstalação.
- A **janela do desktop** era dimensionada só pelo framebuffer: a barra de status ficava com
  **1 px** e as mensagens de erro ("conexão encerrada: …", "falha ao desenhar: …") ficavam
  invisíveis. A janela agora reserva a altura da barra.
- O **"Conectar desktop" travava 6 s por clique**: o sinal de prontidão só era disparado no
  fim da thread da sessão, ou seja, quando a sessão terminava — mesmo com a sessão já de pé.
- As **strings do protocolo RFB** (motivo da recusa, nome do desktop, texto da área de
  transferência) são `uint32` no protocolo: um `length` malicioso podia pedir **4 GB** de uma
  vez e derrubar o PC. Agora têm **teto de 1 MiB**.
- **Duas conexões simultâneas** podiam abrir **duas sessões VNC**: a perdedora ficava órfã
  (socket aberto, thread viva). A abertura de sessão agora é serializada e a perdedora é
  fechada.
- **Fechar a janela do desktop** deixava a **central oculta órfã** no Windows (o `Popen`
  sobrevive ao pai). O encerramento da janela agora segue a mesma ordem do fechar da janela
  de controle: monitor parado, central que o app subiu encerrada, janela destruída.
- **O `x_ready` travava a serial por ~2 minutos.** O `xset` sem prazo bloqueava indefinidamente
  quando sobrava um socket X obsoleto (servidor morto) no chroot, e a console ficava ocupada
  esse tempo todo. Aconteceu de verdade no aparelho. Agora o `xset` roda com `timeout 5`: a
  espera é cortada e a saída diferente de zero significa "o X não responde".
- **O PID do `Xvnc` era o do pai, não o do servidor.** O `setsid` faz *fork* quando o processo
  é líder de grupo (o shell do console é interativo), e o `$!` do spawn é o PID do pai — que
  sai na hora. O script declarava o `Xvnc` morto e subia outro, que falhava com "porta 5901 já
  em uso". Agora o PID é descoberto pelo `/proc`, varrendo o cmdline.
- **A porta 5901 ocupada falhava de forma obscura.** O `start` agora **falha com mensagem
  clara** ("a porta 5901 já está em uso; o desktop anterior pode não ter sido encerrado
  (rode stop)") em vez de deixar o `Xvnc` reclamar "address already in use".
- **O `stop` podia deixar o `Xvnc` órfão.** Ele é o processo que segura a porta 5901, e um
  `Xvnc` vivo fazia o próximo `start` falhar. O `stop` agora **confirma que o `Xvnc` morreu**
  (e que nenhum processo resistiu ao `KILL`) e devolve erro se sobrou algo vivo.
- **O Windows 11 não amarra o driver RNDIS se o ACM vier primeiro.** O "Remote NDIS based
  Internet Sharing Device" só é amarrado quando a função RNDIS é a **primeira** interface do
  gadget (`MI_00`). Com o ACM antes, o RNDIS nasce em `MI_02` e o driver falha com **Code 10**
  (`CM_PROB_FAILED_START`): nenhum adaptador de rede é criado e o PC não alcança o aparelho.
  Detalhe em [Solução de problemas do desktop](#solução-de-problemas-do-desktop).

### Solução de problemas do desktop

**O cabo não aparece / sem porta COM** — confira em **Gerenciador de Dispositivos → Portas
(COM e LPT)**. A central só descobre portas USB/serial reconhecíveis; se houver mais de
uma, fixe a porta com `MC_SERIAL_PORT` no ambiente do processo da central (por exemplo
`set "MC_SERIAL_PORT=COM5"` no `run_windows.bat` ou no `run_desktop_windows.bat`, conforme
qual dos dois subiu a central). O estado "Celular não conectado" (e a fase `no_device`)
significa que a serial não subiu.

**RNDIS sem IP** — o estado é "Celular sem /data ou sem RNDIS". Rode
`setup_windows_network.bat` com a central em execução. O `setup_windows_network.ps1` dá
`10.55.0.1/24` ao PC na interface RNDIS e cria o NAT; se o Windows já usar WinNAT (WSL,
Docker, Hyper-V), ele avisa do conflito em vez de apagar a configuração existente.

**RNDIS com "Code 10" no Windows (requisito do host Windows)** — este é o problema mais
traiçoeiro do projeto no PC. O Windows 11 **só amarra o driver "Remote NDIS based Internet
Sharing Device" quando a função RNDIS é a PRIMEIRA interface do gadget (`MI_00`)**. Se o
**ACM vier antes**, o RNDIS nasce em `MI_02` e o driver falha ao iniciar com **Code 10**
(`CM_PROB_FAILED_START`): **nenhum adaptador de rede é criado** e o PC não alcança o
aparelho. O sintoma no Gerenciador de Dispositivos é o device "Remote NDIS based Internet
Sharing Device" com **Code 10**, e o `setup_windows_network.bat` nunca acha o adaptador.

O que fazer:

- **Confirme a ordem pelo lado do PC** (o `app.py` faz isso sozinho, via `Get-PnpDevice`):
  o `InstanceId` do device RNDIS carrega a interface do gadget — `USB\VID_…&MI_00\…` é a
  **ordem certa**; `&MI_02\…` é a **ordem errada** (ACM primeiro):

  ```powershell
  Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -like '*NDIS*' } |
      Select-Object FriendlyName, InstanceId, Status
  ```

- **A correção é no aparelho**: o `app.py` desliga o UDC, deslinka as duas funções e
  religa na ordem `rndis → acm` (o `acm.gs0` do console **nunca** é removido — só o link é
  refeito, e é ele que devolve a serial). O `app.py` confere a ordem **no PC** e só toca no
  gadget quando ela está **errada**; se a consulta ao PnP falhar, o resultado é "unknown" e
  **nada** é tocado.
- **No Linux a ordem é indiferente**: o kernel nomeia as interfaces pelo driver, não pela
  ordem do link, e o `app.py` nunca toca no gadget fora do Windows.

> A ordem dos links nasce do `init` do aparelho, que cria o **ACM antes** do RNDIS — por isso
> a ordem errada **volta a cada reinício do celular**. O app corrige em runtime; o ciclo
> completo de boot ainda não foi validado (ver
> [O que ainda não foi provado](#o-que-ainda-não-foi-provado)).

**`/data` não montado** — o instalador recusa começar. Monte pelo painel web
(ação **Montar /data**) ou, no terminal:

```sh
mount -t f2fs /dev/mmcblk0p60 /data
```

**A instalação falhou** — leia o status e o log no aparelho:

```sh
cat /data/malta-desktop/run/install.status   # STATE=error · STEP=… · ERROR=…
cat /data/malta-desktop/logs/install.log
```

As etapas são `preflight → mounts → apk → config → session → vnc → verify`. As falhas mais
comuns são `apk` sem rota/DNS (falta internet pelo cabo) e espaço insuficiente em `/data`
(mínimo de 200 MB). O botão **Instalar/Reparar desktop** é idempotente: pode repetir.

**O bundle não cabe no limite de upload** — a central recusa o arquivo com HTTP 413
(`too_large`). O app já lê o limite efetivo (`GET /api/settings` → `transfer_upload_kb`) e o
**eleva para 64 KB** quando o bundle precisa; se a central recusou a elevação, o log do app
mostra `não foi possível elevar o limite de upload da central` ou `a central não aceitou o
novo limite de upload`. Ajuste o limite pela UI do painel web (**Configurações**, campo
**Upload**, teto de 64 KB) e repita **Instalar/Reparar desktop**.

**`10.55.0.2:5901` não responde** — o VNC não está no ar. Use **Diagnóstico**: `PASS xvnc`,
`PASS x` e `PASS vnc` dizem em qual degrau parou. `FAIL rndis` significa que o
`rndis0` do aparelho está sem `10.55.0.2`. O app **não** testa a porta para saber disso: ele
lê o `status` do aparelho (`STATE` e `X_READY`). Do lado do PC, o teste manual é:

```powershell
Test-NetConnection 10.55.0.2 -Port 5901
```

> Faça esse teste **uma vez**, e não em laço. Uma conexão TCP que morre antes do handshake
> conta como falha de login para o TigerVNC, que bloqueia o endereço de origem.

**Autenticação recusada** — o desktop tem uma senha diferente da que o PC conhece. É um erro
**tipado** (`RfbAuthError`) e o monitor **não tenta de novo sozinho**: a janela mostra
*"senha do desktop recusada pelo servidor VNC; use 'Instalar/Reparar desktop'"* e espera ação
do usuário.

**Repetir o botão não resolve o caso do endereço blacklisted.** Se a senha está certa — o
arquivo `/root/.vnc/passwd` (8 bytes) do chroot é reenviado a cada **Instalar/Reparar
desktop** —, o que o TigerVNC recusou foi o **endereço de origem**, não a senha. O TigerVNC
**blacklista** quem falha no login e o bloqueio **expira sozinho** (com o `BlacklistTimeout`
padrão do TigerVNC, **1200 s ≈ 20 min**). Enquanto o bloqueio estiver valendo, **nem a senha
certa passa**: repetir o botão só produz novas tentativas, e o **Instalar/Reparar desktop**
também não resolve — ele reinstala a senha, que já estava certa.

Na prática: esperar o bloqueio expirar, ou **parar e subir de novo o desktop no aparelho**
(**Parar desktop** → **Conectar desktop**), o que troca o processo `Xvnc` e descarta o
bloqueio guardado nele. O `doctor` acusa o arquivo de senha como `FAIL vncpasswd` quando ele
não tem exatamente 8 bytes.

**Desktop congelado depois de desconectar o cabo** — o monitor (a cada 3 s) detecta a queda
da serial e derruba a sessão, justamente para não deixar uma meia conexão travada. Se a
janela continuar congelada, feche-a e clique em **Conectar desktop** de novo. O `start` é
idempotente por componente e o `stop` não deixa órfãos, então não há servidor duplicado.

**Menu ilegível ou vazio** — confira `XDG_CONFIG_DIRS=/etc/xdg` (exportado pela sessão), o
arquivo `/etc/xdg/jgmenu/jgmenu-apps.csv` e as opções de cor do `jgmenurc` (no jgmenu 4.5
são `color_norm_fg`, `color_sel_fg`, … — um nome errado é ignorado em silêncio). O log da
sessão diz o que houve: `cat /data/malta-desktop/root/tmp/session.log`.

**O terminal não abre** — quase sempre é `/dev/pts` ou `/dev/shm` sem montar no chroot
(o `xterm` falha com "not enough ptys"). As montagens são feitas pelo `start`; o `doctor`
verifica `/proc`, `/dev` e `/sys` (`PASS mounts`), mas **não** verifica `/dev/pts` e
`/dev/shm` — confira na mão:

```sh
grep ' /data/malta-desktop/root/dev/pts ' /proc/mounts
grep ' /data/malta-desktop/root/dev/shm ' /proc/mounts
```

Se faltarem, pare o desktop (**Parar desktop**) e suba de novo (**Conectar desktop**), que
o `start` refaz as montagens.

**Reverter tudo** — veja [Remoção completa do Malta Desktop](#remoção-completa-do-malta-desktop).

## Estado validado

Testado ao vivo em 19–21/10/2026:

- Celular com Alpine, console ACM e RNDIS no mesmo gadget USB.
- MACs fixos: `dev_addr 02:00:00:00:00:02` / `host_addr 02:00:00:00:00:01`.
- PC vê sempre **`enx020000000001`** com `10.55.0.1/24`; celular recebe `10.55.0.2/24`.
- Internet pelo cabo: ping ao gateway, ping `8.8.8.8` e DNS por `1.1.1.1`.
- `/data` (f2fs em `/dev/mmcblk0p60`, ~20 GB) montado automaticamente.
- App sob `systemd` (`malta-central.service`), com reconexão serial automática.
- **Reboot do celular testado**: após reiniciar, o app recriou o RNDIS com os MACs fixos,
  reconfigurou IP/rota/DNS, montou `/data` e voltou a `prov=ok` sem intervenção.
- `sysrq b` é o reset que funciona; o `reboot` do BusyBox é ignorado pelo init do aparelho.
- **311 testes automatizados** passando em Linux e Windows (`unittest`, exit 0), com 1 skip
  pré-existente e alheio ao desktop. Contagem de 21/10/2026. Cobrem política de arquivos,
  confirmação de ações, catálogo, IA **e** o desktop: o DES d3des do VncAuth conferido contra
  o binário real do `vncpasswd` do TigerVNC, um servidor RFB falso, o contrato entre as
  chaves emitidas pelo aparelho e as lidas pelo PC e a sintaxe dos scripts (`bash -n`).

### Malta Desktop validado ponta a ponta (em container)

O Malta Desktop foi validado ponta a ponta em **container Alpine 3.24 (Docker + WSL2,
x86_64 — não é o ARMv7 do celular)**, com os scripts e pacotes reais:

- `install.sh` real executado **3×** (idempotente, `STATE=done` a cada vez), `session.sh`
  real, configs reais e `Xvnc` real do TigerVNC.
- O **cliente do projeto** conectando: autenticação VncAuth aceita (`SecurityResult=0`),
  framebuffer 1024×600 com **99,98 % de pixels não pretos** (cor dominante = a parede
  `#300a24` quantizada em RGB565, sem troca de canais).
- **Entrada comprovada por diff de pixels**: ponteiro (122 px alterados) e teclado (359 px
  alterados, com "malta" digitado no terminal).
- `stop` sem processos órfãos, `start` idempotente, autostart criado e removido e o limite
  de 500 caracteres do `/api/cmd` respeitado.

> Essa execução é do **conjunto anterior** (com o proxy `socat` no meio). O que mudou depois —
> o `Xvnc` com `-interface` e a ordem do RNDIS no Windows — foi validado no aparelho real,
> logo abaixo.

### Malta Desktop validado no aparelho real (Moto E7 Power)

O mesmo caminho foi validado **no hardware de verdade** — Moto E7 Power (Alpine 3.24
**armv7**) com o Windows 11 como host:

- **Instalação real**: `install.sh` executado no aparelho — **163 pacotes, 309,2 MiB** no
  chroot `/data/malta-desktop/root`, **~90 s**; versão do bundle = **md5 do `install.sh`**.
- **Sessão real**: `Xvnc` do TigerVNC 1.16 subindo **direto na interface USB**
  (`-interface 10.55.0.2:5901`), sem `socat` e sem `-localhost`.
- **O framebuffer real renderiza**: captura de 1024×600 pelo **cliente nativo do projeto**
  com **99,99 % dos pixels não pretos**; cores dominantes `(49,8,32)` = a parede aubergine
  `#300a24` quantizada em RGB565 e `(41,0,24)` = o painel tint2 `#2c001e`. O relógio e os
  **3 launchers** do painel aparecem, e o ponteiro do mouse é desenhado.
- **Autenticação VncAuth real** aceitada pelo `Xvnc` do aparelho (não por um servidor falso).
- **Exposição restrita**: o VNC está preso à interface USB — não aparece em `0.0.0.0` nem na
  interface Wi-Fi.
- **RAM real medida**: **PSS 73,5 MiB** e **RSS somado 114,6 MiB** em repouso (veja
  [Memória medida](#memória-medida--e-o-que-ela-não-prova)).
- **`doctor` no aparelho**: **14 PASS / 0 FAIL**.
- **Descoberta do host Windows**: o Windows 11 só amarra o driver "Remote NDIS based Internet
  Sharing Device" quando o RNDIS é a **primeira** interface do gadget (`MI_00`); com o ACM
  antes, o driver falha com **Code 10** e o PC não alcança o aparelho. O `app.py` passou a
  garantir a ordem dos links no aparelho e a **conferi-la pelo lado do PC**.

## O que ainda não foi provado

O Malta Desktop **roda no Moto E7 Power físico**, e o PC **se conecta a ele**: as duas pontas
estão unidas (veja [Malta Desktop validado no aparelho real](#malta-desktop-validado-no-aparelho-real-moto-e7-power)).
O que segue **continua pendente**:

- **O upload do bundle pelo caminho HTTP do app.** O `install.sh` **foi executado no aparelho
  real**, disparado pelo `/api/cmd` + serial — que é confiável, porque são comandos pequenos.
  O que é **frágil** é o *upload* do bundle pelo caminho do app (`POST /api/files/upload`): o
  arquivo vai em base64, **em blocos de 240 caracteres, um comando serial por bloco**, com
  timeout de 10 s por bloco, e a serial é de **115200 baud** (~11,5 KB/s). O
  `device/malta-desktop.sh` (32,8 KB) sozinho vira **~182 comandos**, e o bundle inteiro
  (~92 KB, 14 arquivos) vira **~513 comandos** — uma sequência longa de ida e volta, em que
  qualquer queda de cabo ou timeout deixa a gravação pela metade. **Contorno usado**: o
  bundle foi deixado no `/data` por outro caminho e só o `install.sh` foi disparado pelo
  `/api/cmd`. O caminho HTTP completo (bundle inteiro pelo app) **ainda não é confiável** em
  115200 baud.
- **O desempenho com o cliente real** e a **resposta do teclado `br`** (layout brasileiro): o
  mouse está comprovado (o ponteiro aparece desenhado no framebuffer real), mas a latência
  percebida com o cliente RFB próprio e o mapeamento do layout `br` **não foram medidos**.
- **O consumo de bateria** e o uso do aparelho como display com o VNC no ar.
- **O logon real do Windows com o autostart**: o `.cmd` da pasta Startup foi criado e removido
  por script, mas **nenhum logon de verdade** foi observado.
- **O RNDIS depois de um reinício do celular**: a ordem dos links nasce do `init` do aparelho,
  que cria o **ACM antes** do RNDIS — o RNDIS nasce em `MI_02` e o Windows falha com **Code
  10**. O `app.py` corrige a ordem **em runtime**, mas o **ciclo completo de boot**
  (reboot → ordem errada → correção → link de novo) ainda não foi validado ponta a ponta.
- **A estabilidade em longos períodos**: não há observação de horas de sessão contínua (o
  `Xvnc` no aparelho, o monitor do PC e a sessão RFB ao mesmo tempo).
- **O comportamento do blacklisting do TigerVNC** — quantas falhas de login cada conta
  permite e quanto dura a expiração. O mecanismo foi **evitado** (a sonda TCP crua saiu do
  caminho), não medido.

## Interface

A UI é um shell desktop em HTML/CSS/JS puro (sem React, sem CDN), com tema claro/escuro,
navegação por hash, paleta de comandos (`Ctrl+K`), atalhos `Alt+1..9` e statusbar.

| Área | Conteúdo |
|------|----------|
| **Visão geral** | Bateria, memória, `/data`, carga, uptime, rede e ações rápidas com confirmação |
| **Arquivos** | Explorador com navegação, upload/download, editor de texto, criar/renomear/excluir |
| **Terminal** | Shell root via serial, histórico local (120 comandos), código de saída e tempo |
| **Sistema** | Kernel, versões, CPU, memória, disco, montagens, ferramentas instaladas |
| **Rede** | Interfaces, rota padrão, DNS, portas em escuta, vizinhos, RNDIS e sonda de internet |
| **Catálogo** | 46 itens embutidos + itens locais, verificação honesta de disponibilidade |
| **Assistente** | Chat opcional via API OpenAI-compatible; sugere comandos, nunca executa |
| **Eventos** | Log da central com filtro por nível, busca e atualização ao vivo |
| **Configurações** | Tema, polling, automações e limites de transferência/editor |

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

> **Requisito do host Windows:** o Windows 11 só amarra o driver "Remote NDIS based Internet
> Sharing Device" quando a função RNDIS é a **primeira** interface do gadget (`MI_00`). Com o
> ACM antes, o RNDIS nasce em `MI_02` e o driver falha com **Code 10** — nenhum adaptador de
> rede é criado. O `init` do aparelho cria o ACM antes do RNDIS, então isso volta a cada
> reinício; o `app.py` corrige a ordem em runtime. Detalhes e o que fazer em
> [Solução de problemas do desktop](#solução-de-problemas-do-desktop).

## Windows 10/11 — iniciar a central no PC

O desktop web também roda no Windows. O telefone não precisa ser reflasheado nem ter sua
imagem Alpine alterada.

1. Instale Python 3.10 ou superior pelo [python.org](https://www.python.org/downloads/windows/)
   e mantenha o Python Launcher (`py`) habilitado no instalador.
2. Baixe/clone este repositório e extraia-o em uma pasta gravável.
3. Conecte o Moto E7 Power por USB.
4. Dê duplo clique em `run_windows.bat`. Na primeira execução, o script cria `.venv`,
   instala as dependências e abre o navegador em `http://127.0.0.1:8666`.
5. Para encerrar, volte à janela do terminal e pressione Ctrl+C.

O painel inicia mesmo que o telefone esteja desconectado; nesse caso, as telas continuam
acessíveis, mas os recursos que dependem do aparelho aparecem como offline.

> Para ver o **desktop X do celular numa janela nativa do Windows** (sem navegador), use
> `run_desktop_windows.bat` — veja [Malta Desktop](#malta-desktop--desktop-x-do-celular-numa-janela-do-windows).
> O app do desktop **reaproveita** a central que estiver rodando e só sobe uma cópia oculta
> quando não há nenhuma. **Não abra os dois ao mesmo tempo**: a serial é um recurso único e a
> central Flask é a dona dela.

### Porta serial no Windows

O Windows precisa enumerar o console USB do telefone como uma porta COM. Confira em
**Gerenciador de Dispositivos → Portas (COM e LPT)**. A central tenta descobrir portas USB/
serial reconhecíveis automaticamente. Se houver várias portas COM e a correta não for
identificada, edite `run_windows.bat` e defina a porta do console, por exemplo:

```bat
set "MC_SERIAL_PORT=COM5"
```

Substitua `COM5` pela porta que aparece no Gerenciador de Dispositivos. Se nenhuma porta
serial for criada pelo Windows, o painel ainda abre, mas será necessário resolver o driver/
enumeração USB antes de controlar o aparelho. Não escolha uma porta COM aleatória.

As configurações locais e a chave da IA ficam em
`%LOCALAPPDATA%\malta-central`, fora do clone do Git. As permissões de segredo usam as
ACLs do perfil de usuário do Windows; os modos POSIX `0600` não se aplicam ao NTFS.

### RNDIS e Internet pelo cabo USB no Windows

Depois que `run_windows.bat` estiver aberto e o telefone aparecer conectado na tela da Malta
Desktop, dê duplo clique em **`setup_windows_network.bat`**. O script solicita elevação UAC
e configura a interface RNDIS do próprio Windows:

- IP estático do PC `10.55.0.1/24`, sem gateway padrão nessa interface;
- encaminhamento IPv4 na interface RNDIS;
- NAT de saída para a sub-rede `10.55.0.0/24`, usando o cmdlet nativo `New-NetNat`;
- detecção do adaptador RNDIS com opção de seleção se houver mais de um.

O script espera o adaptador aparecer porque a central pode precisar provisionar o gadget RNDIS
pela serial primeiro. **Mantenha a central em execução e só então execute o configurador.**
Ele não altera adaptadores Wi-Fi/Ethernet de saída, não remove regras NAT existentes e recusa
continuar se detectar conflito de IP/rota com outra interface.

> **Se o adaptador RNDIS nunca aparece**, o motivo quase sempre é a **ordem das interfaces do
> gadget**: o Windows 11 só amarra o driver "Remote NDIS based Internet Sharing Device" quando
> o RNDIS é a **primeira** interface (`MI_00`); com o ACM antes, o driver falha com **Code 10**
> (`CM_PROB_FAILED_START`) e nenhum adaptador é criado. O `app.py` confere essa ordem pelo
> lado do PC (via `Get-PnpDevice`) e a corrige no aparelho quando ela está errada. Sintoma,
> diagnóstico e o que fazer em
> [Solução de problemas do desktop](#solução-de-problemas-do-desktop).

Se o Windows já estiver usando WinNAT por WSL, Docker, Hyper-V ou outra ferramenta, a criação
do NAT pode ser rejeitada pelo próprio Windows. Nesse caso o script informa o conflito sem
apagar a configuração existente; a conectividade NAT exige resolver esse conflito com cuidado.
O PC precisa ter uma rota padrão e acesso à Internet para compartilhar a conexão. Após configurar,
volte à tela **Rede** da Malta Desktop e atualize o diagnóstico.

O servidor web continua restrito a `127.0.0.1`; não o exponha diretamente à rede.

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

> **Ao atualizar o código, reinicie o processo/serviço.** Um processo antigo continua servindo
> a versão anterior mesmo com os arquivos novos no disco (`systemctl restart
> malta-central.service` ou mate/reinicie o `app.py` manual).

### Variáveis de ambiente

| Variável | Padrão | Efeito |
|----------|--------|--------|
| `MC_PORT` | `8666` | Porta do servidor (sempre em `127.0.0.1`) |
| `MC_DISABLE_THREADS` | — | `1` não sobe poller/provisionador (usado nos testes) |
| `MC_ALLOWED_HOSTS` | — | Hosts extras aceitos pelo guard, separados por vírgula |
| `MALTA_AI_DEVICE` | — | `1` liga o modo device: a chamada sai do Alpine, não do PC |
| `MALTA_AI_BASE_URL` | — | Base da API OpenAI-compatible (só no modo HTTP direto) |
| `MALTA_AI_MODEL` | — | Modelo (obrigatório para habilitar a IA em qualquer modo) |
| `MALTA_AI_API_KEY` | — | Chave (só no modo HTTP direto; no modo device fica em `/data/ai/nvidia.key`) |
| `MALTA_AI_TIMEOUT` | `60` | Timeout da chamada, 5–180 s |
| `MALTA_AI_MAX_TOKENS` | `900` | Máximo de tokens, 64–4096 |
| `MALTA_AI_TEMPERATURE` | `0.2` | Temperatura, 0.0–1.5 |

Copie `.env.example` para um arquivo local **não versionado** e carregue as variáveis no
ambiente do processo (o app não lê `.env` sozinho). Nunca coloque a chave no frontend, em
`settings.json` ou no repositório.

### Lado do PC (persistente)

```bash
nmcli connection show malta-rndis-fixed
ls -l /etc/NetworkManager/dispatcher.d/90-malta-nat
systemctl is-enabled NetworkManager-dispatcher
```

O dispatcher escolhe o uplink pela rota default no momento em que a interface sobe. Se a
Wi-Fi mudar, reconecte a interface RNDIS (ou rode o dispatcher manualmente) para recalcular.

### Fallback manual

`reverse_tether_host.sh` (PC) e `reverse_tether_cell.sh` (celular, via serial) são o caminho
manual de emergência. O caminho normal é o app. O gadget do malta **não tem ECM/NCM**; o
script do celular usa RNDIS.

## Explorador e editor de arquivos

Toda a política é aplicada **no backend**, não só no JavaScript:

- Caminhos precisam ser absolutos, normalizados e sem `..`.
- **Leitura bloqueada** em `/proc`, `/sys`, `/dev`, `/boot` e `/run` — use o terminal.
- **Arquivos sensíveis** (`/etc/passwd`, `/etc/shadow`, `/etc/sudoers`, `/etc/fstab`,
  `/etc/inittab`, entre outros) não são expostos no explorador, mesmo via link simbólico.
- Links são resolvidos no aparelho (`readlink -f`) e o alvo é revalidado; um link em `/data`
  não pode escapar para `/etc/shadow`.
- **Escrita permitida** apenas em `/data`, `/tmp`, `/etc`, `/root`, `/home`, `/var`, `/opt`,
  `/usr/local`, `/srv`, `/media` e `/mnt`.
- Escrita em `/etc`, `/root`, `/var`, `/usr`, `/opt`, `/home` e `/srv` exige `confirm: true`.
- Raízes de sistema (`/`, `/data`, `/etc`, `/usr`, …) não podem ser removidas ou renomeadas.
- Nomes e caminhos passam por `shlex.quote` antes de chegar ao shell.

Limites (configuráveis na UI, com teto rígido no backend):

| Operação | Padrão | Teto |
|----------|--------|------|
| Upload | 32 KB | 64 KB |
| Download | 16 KB | 48 KB |
| Editor de texto | 24 KB | 64 KB |

Uploads são enviados em base64 por blocos para `/tmp/.mc_up.b64` e gravados com `base64 -d`
+ `chmod`. Arquivos binários são recusados pelo editor; o download é sempre
`application/octet-stream`.

## Terminal

O terminal aceita até **500 caracteres por comando** e devolve, além de `ok`/`output`:

- `exit_code`: código de saída real do shell no celular;
- `success`: `true` somente com serial ok e código 0;
- `state`: `ok`, `failed` ou `error`;
- `elapsed_ms`: tempo do round-trip serial.

O protocolo envia `<cmd>; echo __MCXI$?__; echo __MC<n>__DONE__`, lê até o marcador em uma
linha própria e filtra os marcadores da saída. Um lock serializa as requisições.

O assistente **não executa** comandos: o botão “Usar no terminal” apenas preenche o campo,
e o usuário revisa e envia.

## Catálogo de apps e serviços

O catálogo mistura itens embutidos (rotas da central, documentação, ferramentas do celular,
serviços conhecidos) com itens cadastrados localmente. Cada item tem um estado honesto:

- `available` — verificado e respondendo;
- `unavailable` — verificado e indisponível;
- `unverified` — ainda não verificado (nada é presumido).

Verificações usam TCP para serviços e HTTP para links, com cache de 120 s. O catálogo
**não instala pacotes nem altera o boot**; itens locais aceitam apenas:

- `type: "link"` com URL `http://` ou `https://` (sem credenciais embutidas);
- `type: "service"` com `host` + `port` TCP.

Comandos não são aceitos em nenhum campo. O cadastro local fica em `catalog.local.json`
(máximo 100 itens, 64 KB), criado pela UI; veja `catalog.local.example.json`.

## Assistente de IA

Opcional e desligado por padrão. A configuração não secreta pode ser feita na UI
(aba **Assistente → Configuração**) e fica em `ai.json` (modo 600, gitignored). Ela tem
precedência sobre as variáveis de ambiente para os campos definidos; o ambiente continua
servindo de padrão e de fallback. Há dois modos:

- **device** (`MALTA_AI_DEVICE=1` ou modo device na UI): o pedido sai **do Alpine**, não do
  PC. Exige apenas o modelo; a chave da NVIDIA fica no aparelho, em
  `/data/ai/nvidia.key` (modo 600). A UI grava a chave pelo backend, que usa o serial com
  `stty -echo` e base64; o editor de arquivos genérico não deve ser usado para isso.
- **HTTP direto**: o Flask chama o provedor. Exige URL base + modelo (e uma chave para
  provedor remoto; loopback dispensa). A chave pode vir de `ai.key` (modo 600, gravado pela
  UI) ou de `MALTA_AI_API_KEY` (fallback).

A chave passa pelo navegador **uma vez**, no momento em que é digitada no campo
`type=password`; o servidor nunca a devolve, não a grava em log, não a inclui no contexto e
não a versiona. Ela não é persistida em `localStorage` nem em `sessionStorage`, e o campo é
limpo depois de salvar. Sem a configuração do modo escolhido, o endpoint responde
`503 ai_not_configured` e **nenhuma chamada externa é feita**.

Garantias:

- a chave nunca é devolvida por `/api/ai/status`, nunca entra no contexto e é removida de
  mensagens de erro (`[redacted]`);
- o contexto enviado é um resumo sanitizado (página, bateria, memória, `/data`, rede,
  último erro, versão) — sem arquivos, sem histórico do terminal, sem credenciais;
- a IA **não executa nada**: devolve texto; comandos aparecem em blocos de código para
  revisão humana;
- limites: 16 mensagens, 4000 caracteres por mensagem, 12000 no total.

### Configuração pela UI

A aba **Assistente** tem um formulário com modo, modelo, URL base (só no modo HTTP),
timeout (5–180 s), máximo de tokens (64–4096) e temperatura (0–1,5). Salvar grava `ai.json`
com modo 600. O campo de chave é separado: ele só fica habilitado depois que o modo do
formulário foi salvo e grava no destino correspondente:

| Modo | Destino da chave | Como o backend grava |
| --- | --- | --- |
| device | `/data/ai/nvidia.key` no Alpine | serial, `stty -echo`, base64, `chmod 600` |
| http | `ai.key` no PC | arquivo atômico, modo 600 |

API usada pela UI:

- `GET /api/ai/status` — estado efetivo, sem a chave; `?check_key=1` verifica a presença no
  aparelho (modo device) sem bloquear um chat em andamento;
- `POST /api/ai/config` — valida e grava `ai.json`;
- `POST /api/ai/key` — grava a chave no destino do modo salvo;
- `DELETE /api/ai/key` — remove a chave do destino (não remove `MALTA_AI_API_KEY` do
  ambiente).

### Modo device (NVIDIA chamada pelo Alpine)

O Alpine não tem Python nem cliente HTTPS funcional no BusyBox, então o cliente vive em
`/data` (persistente; o rootfs é RAM):

```
/data/ai/bin/curl        cliente TLS + /data/ai/lib/*.so
/data/ai/ca.pem          CA bundle
/data/ai/nvidia.sh       monta o header e chama a NVIDIA
/data/ai/nvidia.key      chave nvapi-..., modo 600 (nunca sai do aparelho)
```

O fluxo: a central serializa o pedido em JSON, envia em **base64** pelo serial em blocos de
400 caracteres (o texto do usuário nunca é interpolado no shell), o aparelho decodifica em
`/tmp/mc_ai_req.json`, e o script chama
`https://integrate.api.nvidia.com/v1/chat/completions` com `--max-time 120`. A resposta JSON
volta pelo serial e é validada pela central.

Configuração no drop-in do systemd:

```ini
[Service]
Environment=MALTA_AI_DEVICE=1
Environment=MALTA_AI_MODEL=nvidia/llama-3.1-nemotron-70b-instruct
Environment=MALTA_AI_TIMEOUT=120
Environment=MALTA_AI_MAX_TOKENS=900
```

A chave pode ser gravada pelo campo dedicado da UI (recomendado; o backend usa o serial com
eco desligado). Sem a UI, pare o serviço e use o helper local `/tmp/install_key.py` (não
versionado), que lê a chave sem eco e a envia direto ao console do Alpine:

```bash
sudo systemctl stop malta-central
/home/iagol/malta-central/venv/bin/python /tmp/install_key.py
sudo systemctl start malta-central
```

Não use o editor de arquivos genérico da central para a chave: ele é uma ferramenta de
arquivos e a chave deve passar apenas pelo campo dedicado ou pelo helper.

Notas:

- `meta/llama-3.1-70b-instruct` foi descontinuado em 2026-08-26 e responde `410 Gone`;
  consulte `GET https://integrate.api.nvidia.com/v1/models` para o catálogo atual.
- uma chave inválida responde `403 Authorization failed`; a central devolve isso como
  `502 provider_http`, sem repetir a chave.
- a chamada pode levar até 120 s no aparelho; o cliente do navegador espera 200 s.

### Provedor local (Ollama, alternativa no PC)

Validado nesta máquina com o Ollama em loopback — sem chave, sem tráfego externo:

```bash
# no unit do serviço (drop-in) ou no ambiente do processo
MALTA_AI_BASE_URL=http://127.0.0.1:11434/v1
MALTA_AI_MODEL=qwen3:1.7b-q4_K_M
MALTA_AI_TIMEOUT=180
MALTA_AI_MAX_TOKENS=400
```

Medido neste PC (4 núcleos, 3,7 GB de RAM): carga fria do modelo ≈ 45 s; geração a
≈ 6,7 tokens/s; uma resposta de 250 tokens levou ≈ 58 s. Consequências práticas:

- `MALTA_AI_MAX_TOKENS` acima de ~500 estoura o tempo hábil do navegador; use 400.
- modelos de raciocínio (família `qwen3`) gastam parte dos tokens em `reasoning`; respostas
  longas aparecem truncadas (`finish_reason: length`). Para respostas completas, prefira um
  modelo não-raciocinante ou um provedor remoto.
- `qwen3.5:2b` não carregou dentro de 120 s nesta máquina; o `qwen3:1.7b-q4_K_M` carrega e
  responde, com a latência acima.
- o cliente espera até 200 s, alinhado ao teto de 180 s do backend.

## API

| Método | Rota | Função |
|--------|------|--------|
| GET | `/` | Shell desktop |
| GET | `/api/status` | Telemetria, rede, `/data`, histórico, provisionamento e probe |
| GET | `/api/health` | Saúde do app, serial, RNDIS e versão |
| GET | `/api/actions` | Ações disponíveis (`requires_confirm` nas destrutivas) |
| POST | `/api/action` | Executa ação; `reboot`/`umountdata` exigem `{"confirm":true}` (senão `409`) |
| POST | `/api/cmd` | Terminal (`{"cmd":"uptime"}`) com `exit_code`/`state`/`elapsed_ms` |
| GET | `/api/network` | Interfaces, rotas, DNS e estado do RNDIS |
| POST | `/api/network/refresh` | Força novo probe de rede |
| GET | `/api/network/detail` | Interfaces, rotas, DNS, portas, vizinhos e RNDIS (cache 20 s) |
| GET | `/api/system` | Inventário do Alpine: kernel, memória, discos, ferramentas (cache 30 s) |
| GET | `/api/settings` | Tema, polling, automações e limites |
| POST | `/api/settings` | Grava configurações (valores fora da faixa são limitados) |
| GET | `/api/logs` | Eventos do app |
| GET | `/api/files/roots` | Raízes navegáveis, estado e limites efetivos |
| GET | `/api/files?path=...` | Lista diretório |
| GET | `/api/files/stat?path=...` | Metadados de um caminho |
| GET | `/api/files/read?path=...` | Lê texto (limite do editor) |
| POST | `/api/files/write` | Grava texto (`confirm` para áreas sensíveis) |
| POST | `/api/files/upload` | Upload base64 (`confirm` para áreas sensíveis) |
| GET | `/api/files/download?path=...` | Baixa arquivo (limite de download) |
| POST | `/api/files/mkdir` | Cria diretório |
| POST | `/api/files/rename` | Renomeia/move |
| POST | `/api/files/delete` | Exclui (`confirm: true`) |
| GET | `/api/catalog` | Catálogo embutido + local |
| POST | `/api/catalog/check` | Verifica itens (`{"ids": [...]}` ou todos) |
| GET/POST | `/api/catalog/local` | Lista/cadastra itens locais |
| DELETE | `/api/catalog/local` | Remove item local (`{"id":"local-..."}`) |
| GET | `/api/ai/status` | Estado da IA, sem expor a chave |
| POST | `/api/ai/chat` | Chat (`{"messages":[...], "context":{...}}`) |
| POST | `/api/upload` | Compatibilidade: upload base64 |
| GET | `/api/download?path=...` | Compatibilidade: download |

Exemplos:

```bash
curl -s http://127.0.0.1:8666/api/status
curl -s -X POST http://127.0.0.1:8666/api/cmd \
  -H 'Content-Type: application/json' -d '{"cmd":"uptime"}'
curl -s -X POST http://127.0.0.1:8666/api/action \
  -H 'Content-Type: application/json' -d '{"action":"df"}'
curl -s -X POST http://127.0.0.1:8666/api/action \
  -H 'Content-Type: application/json' -d '{"action":"reboot","confirm":true}'
curl -s 'http://127.0.0.1:8666/api/files?path=/data'
```

`provision` força a recriação da função RNDIS com os MACs fixos mesmo que o link já esteja
ativo; útil se a função antiga tiver ficado com MAC aleatório.

## Segurança

- O dashboard dá **shell root** no celular e não tem autenticação; ele escuta só em
  `127.0.0.1`. Não exponha a porta 8666 na rede.
- Guard de host contra DNS rebinding (`127.0.0.1`, `localhost`, `::1`, extensível por
  `MC_ALLOWED_HOSTS`) e checagem de `Origin` em POST/PUT/PATCH/DELETE.
- Cabeçalhos: CSP `script-src 'self'`, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- Corpo de requisição limitado a 2 MB; upload/download/editor com tetos próprios.
- Para acesso remoto, use um túnel SSH autenticado ou proxy com autenticação; não abra a
  porta diretamente.
- A chave da IA vive apenas no ambiente do processo. Não a coloque em arquivo versionado.
- No **Windows**, `os.chmod(0600)` **não protege o arquivo**: o NTFS só alterna o bit de
  leitura e o ACL que vale é o do perfil do usuário. Isso vale para o `desktop.json` do
  Malta Desktop, que guarda a **senha VNC em texto claro**: quem tem a sua conta do Windows
  tem a senha. Trate `%LOCALAPPDATA%\malta-central` como dado sensível — não o copie para
  um lugar que o mesmo usuário não leia, e apague o `desktop.json` junto com o desktop
  (ver [Remoção completa do Malta Desktop](#remoção-completa-do-malta-desktop)).
- O app usa o servidor de desenvolvimento do Flask; para uso local é suficiente. Para
  produção, coloque um WSGI (waitress/gunicorn) atrás de um proxy autenticado.

## Testes

```bash
cd malta-central
MC_DISABLE_THREADS=1 venv/bin/python -m unittest discover -s tests -v
```

`MC_DISABLE_THREADS=1` evita subir poller/provisionador durante os testes; a serial é falsa e
o celular não é necessário. São **311 testes** (43 em `test_app.py`, 54 em `test_v3.py`, 7 em
`test_windows.py`, 95 em `test_desktop.py`, 85 em `test_device_scripts.py` e 27 em
`test_rfb.py`), com **1 skip** pré-existente e alheio ao desktop
(`test_v3.ApiV3Tests.test_read_ai_key_rejects_symlink_and_non_regular`, pulado porque
`mkfifo` não existe no Windows). Contagem de 21/10/2026 — o comando acima é a fonte da
verdade.

A suíte cobre os contratos legados e a v3: política de arquivos e path traversal, links
simbólicos, confirmação de ações destrutivas, catálogo local, IA sem configuração/redaction,
guard de host/Origin, cabeçalhos e extensão do contrato de `/api/cmd`.

O Malta Desktop acrescenta estes blocos:

| Arquivo | O que cobre |
|---------|-------------|
| `tests/test_rfb.py` | o DES d3des do VncAuth (bits da chave invertidos) conferido contra o binário real do `vncpasswd` do TigerVNC, o formato de pixel RGB565, o handshake RFB 3.8, `CopyRect`/`DesktopSize` e a codificação de teclado e ponteiro |
| `tests/test_desktop.py` | a máquina de estados (`no_device` → `desktop_ready`), o monitor (uma única sessão, reconexão, queda do cabo), o contrato com a API da central, o limite de 500 caracteres do `/api/cmd`, o bundle, a senha, o autostart e o log |
| `tests/test_device_scripts.py` | a sintaxe dos scripts (`bash -n`), a ausência de bashismos, o contrato de chaves `KEY=VALOR` entre o aparelho e o PC, o `install.sh` (branch derivada de `/etc/alpine-release`, chroot com as chaves do host, `STATE`/`STEP` do instalador, nada de partições) e as configs de openbox, tint2, pcmanfm e jgmenu |
| `tests/test_app.py` | a **ordem das interfaces do gadget**: a checagem é feita **no PC** (`Get-PnpDevice`, `&MI_00` × `&MI_02`), o `MI_00` não toca no aparelho, o `MI_02` dispara a correção `rndis → acm`, PnP indisponível não toca em nada, no Linux não há correção, e a correção não entra em laço quando o aparelho volta |

No Windows, o mesmo comando roda direto no `.venv`:

```bat
set MC_DISABLE_THREADS=1
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Checagem de sintaxe do frontend (sem dependências):

```bash
for f in static/js/*.js static/js/views/*.js; do node --input-type=module --check < "$f" || exit 1; done
```

## Solução de problemas

**App não responde**

```bash
systemctl status malta-central.service
journalctl -u malta-central.service -n 50 --no-pager
curl -s http://127.0.0.1:8666/api/health
```

**UI antiga depois da atualização** — há um processo servindo o código anterior:

```bash
ss -ltnp | grep 8666        # veja o PID
systemctl restart malta-central.service
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

Se o MAC estiver aleatório, clique em **Provisionar RNDIS**. Se o log mostrar falha ao
remover a função, verifique se `rmdir` da função é suportado; o app também sabe criar a
função quando ela não existe (caso normal após reboot).

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

**Arquivo não aparece no explorador**

Pode estar em um pseudossistema bloqueado (`/proc`, `/sys`, `/dev`, `/boot`, `/run`) ou ser um
arquivo sensível. Use o terminal. O editor também recusa binários e arquivos acima do limite.

**IA desabilitada**

Confira `MALTA_AI_DEVICE=1` + `MALTA_AI_MODEL` (modo device) ou `MALTA_AI_BASE_URL` +
`MALTA_AI_MODEL` (modo HTTP) no ambiente **do processo** (não só no seu shell) e reinicie o
serviço. `/api/ai/status` lista o que falta, sem mostrar a chave.

**Malta Desktop não conecta** — os sintomas e as causas estão em
[Solução de problemas do desktop](#solução-de-problemas-do-desktop), dentro da seção do
Malta Desktop.

## Remoção completa do Malta Desktop

A remoção é feita nesta ordem, do PC para o aparelho. Nada é removido às cegas: o
`install.sh --remove` recusa qualquer caminho fora de `/data/malta-desktop`.

1. **Desligar o início automático.** Na janela do Malta Desktop, desmarque **Iniciar o
   monitor com o Windows**; ou, no prompt:

   ```bat
   tools\malta_desktop_autostart.bat -Remove
   ```

2. **Fechar o app.** Feche a janela **Malta Desktop** (e a janela do desktop, se estiver
   aberta). Se o app subiu uma central oculta, ela é encerrada junto — uma central que já
   estava rodando antes é mantida.

3. **Apagar o estado local do desktop.** A senha VNC (em texto claro) e o log do desktop
   ficam em dois arquivos:

   ```bat
   del "%LOCALAPPDATA%\malta-central\desktop.json"
   del "%LOCALAPPDATA%\malta-central\desktop.log"
   ```

   A pasta `%LOCALAPPDATA%\malta-central` é **compartilhada** com o painel web — lá também
   ficam `settings.json`, `catalog.local.json`, `ai.json` e `ai.key`. Apagar a pasta
   inteira remove a configuração do painel e a chave da IA junto; faça isso só se quiser
   zerar o estado local do projeto inteiro:

   ```bat
   rmdir /s /q "%LOCALAPPDATA%\malta-central"
   ```

4. **No aparelho, remover o desktop.** O comando roda **no aparelho** (root), não no PC:
   pelo **Terminal** do painel de diagnóstico (a ferramenta secundária) ou por qualquer
   shell root:

   ```sh
   sh /data/malta-desktop/install.sh --remove --yes
   ```

   As **duas flags são obrigatórias** (`--remove --yes`): `--yes` sozinho é recusado, e o
   instalador se recusa a operar em qualquer caminho diferente de `/data/malta-desktop`.

   O instalador **para o desktop** primeiro, **desmonta** `/dev/pts`, `/dev/shm`, `/dev`,
   `/proc` e `/sys` do chroot (as submontagens de `/dev` antes do pai, senão ficam
   penduradas) e só então apaga `/data/malta-desktop` por completo — os ~309 MiB do chroot
   voltam para o `/data`. É **idempotente**: se `/data` não estiver montado ou o diretório
   não existir, ele responde "nada a remover" e sai com código 0.

   É reversível: basta rodar **Instalar/Reparar desktop** de novo para recriar tudo.

5. **Confirmar o que não foi tocado.** Nada de **boot**, **partição**, **bootloader** ou
   **firmware** foi alterado em nenhum momento — nem na instalação, nem na remoção. O
   instalador só escreve em `/data/malta-desktop`; o `remove` só apaga esse mesmo diretório
   e se recusa a operar em qualquer outro caminho. O rootfs initramfs, a partição `boot_a` e
   o mecanismo serial/RNDIS continuam exatamente como estavam.

## Arquitetura do repositório

```
app.py                         backend Flask: serial, provisionamento, probe, APIs e guard;
                               também confere/corrige a ordem das interfaces do gadget
                               (RNDIS antes do ACM) pelo lado do Windows
malta_files.py                 política de caminhos, leitura/escrita/transferência
malta_diagnostics.py           inventário do sistema e da rede
malta_catalog.py               catálogo embutido/local e verificação de estados
malta_ai.py                    configuração, sanitização, redaction e chat OpenAI-compatible
malta_desktop.py               janela de controle do Malta Desktop (Tk, pt-BR)
malta_desktop_core.py          estados do desktop, cliente da API da central, autostart e
                               limite de upload efetivo
malta_rfb.py                   cliente RFB 3.8 próprio (VncAuth, framebuffer, teclado/mouse)
device/malta-desktop.sh        start/stop/restart/status/measure/doctor/log no aparelho
device/install.sh              instalador idempotente do chroot em /data (+ --remove --yes)
device/session.sh              sessão X do chroot (openbox, tint2, pcmanfm) com supervisão
device/config/*                rc do openbox, tint2rc, pcmanfm.conf, jgmenurc e CSV
device/config/applications/*   launchers próprios (.desktop) do pcmanfm, xterm e jgmenu
device/config/icons/*          os três XPM próprios (~8 KB) do painel e do menu
templates/index.html           shell desktop (carrega apenas /static/js/app.js)
static/css/desktop.css         tokens, shell, componentes e responsivo
static/js/api.js               cliente HTTP e contratos
static/js/ui.js                helpers, modal, toast, sparkline
static/js/store.js             estado compartilhado e polling
static/js/router.js            rotas por hash e ciclo de vida das views
static/js/app.js               navegação, chips, statusbar, paleta e atalhos
static/js/views/*.js           home, files, terminal, system, network, apps, assistant,
                               events, settings
tests/test_app.py              43 testes legados (serial falsa) + ordem das interfaces do
                               gadget (MI_00 × MI_02, Get-PnpDevice)
tests/test_v3.py               54 testes da v3 (política, catálogo, IA, segurança)
tests/test_windows.py          7 testes da descoberta de porta COM no Windows
tests/test_desktop.py          95 testes do app Windows (estados, monitor, autostart, senha)
tests/test_device_scripts.py   85 testes dos scripts e das configs do aparelho
tests/test_rfb.py              27 testes do cliente RFB (DES d3des, pixel, handshake, entrada)
run_windows.bat                lançador do painel web no Windows (cria o .venv)
run_desktop_windows.bat        lançador do Malta Desktop no Windows (cria o .venv)
tools/malta_desktop_autostart.bat  atalho para o .ps1 (-Install | -Remove | -Status)
tools/malta_desktop_autostart.ps1  .cmd na pasta Startup do usuário (sem registro, sem UAC)
reverse_tether_host.sh         fallback manual do PC Linux
setup_windows_network.bat      configurador RNDIS + NAT Windows
setup_windows_network.ps1      IP 10.55.0.1 e WinNAT no adaptador RNDIS
reverse_tether_cell.sh         fallback manual do celular (RNDIS)
.github/workflows/tests.yml    CI: unittest em Linux e Windows + sintaxe dos .ps1
.env.example                   variáveis de ambiente, sem credenciais
catalog.local.example.json     exemplo de catálogo local (link e serviço TCP)
requirements.txt               flask, pyserial, pillow
```

`settings.json` e `catalog.local.json` são estado local, criados em tempo de execução e
ignorados pelo git. O serviço systemd e o dispatcher de NAT vivem fora do repositório, em
`/etc`. O Malta Desktop grava o estado local dele em `%LOCALAPPDATA%\malta-central`
(`desktop.json` com a senha VNC e `desktop.log`), também fora do clone do Git.

## Contexto do projeto

Bring-up de Linux nativo num Moto E7 Power com bootloader destravado: kernel stock + initramfs
Alpine na partição `boot_a` (rollback = `fastboot flash boot_a boot.img` stock). Descobertas
do bring-up (layout do boot image v2 com DTBO/DTB, major dinâmico 259 das partições, RESTART2
para fastboot remoto, limitações de ops de driver no boot) estão documentadas no repositório
do sistema.

## Licença

MIT — ver [LICENSE](LICENSE).
