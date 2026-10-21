# malta-central v3.1

**Desktop web local para administrar o Alpine Linux nativo do Motorola Moto E7 Power
(XT2097-5, "malta")** — shell root, arquivos, diagnóstico, catálogo e um assistente de IA
que apenas sugere comandos.

O celular roda Alpine direto na partição `boot_a`, sem Android, e é acessado pelo **console
serial USB** (`/dev/ttyACM*`) e por um link **RNDIS com internet pelo cabo**: o PC é o
gateway/NAT (`10.55.0.1`), o celular é `10.55.0.2`. A central roda no PC e escuta **somente
em `127.0.0.1:8666`**.

> Esta versão entrega a interface desktop (shell, explorador/editor, ferramentas, catálogo e
> assistente). Boot, kernel, initramfs, partições, bootloader e o mecanismo serial/RNDIS
> existente **não foram alterados**.

## Estado validado

Testado ao vivo em 19–20/10/2026:

- Celular com Alpine, console ACM e RNDIS no mesmo gadget USB.
- MACs fixos: `dev_addr 02:00:00:00:00:02` / `host_addr 02:00:00:00:00:01`.
- PC vê sempre **`enx020000000001`** com `10.55.0.1/24`; celular recebe `10.55.0.2/24`.
- Internet pelo cabo: ping ao gateway, ping `8.8.8.8` e DNS por `1.1.1.1`.
- `/data` (f2fs em `/dev/mmcblk0p60`, ~20 GB) montado automaticamente.
- App sob `systemd` (`malta-central.service`), com reconexão serial automática.
- **Reboot do celular testado**: após reiniciar, o app recriou o RNDIS com os MACs fixos,
  reconfigurou IP/rota/DNS, montou `/data` e voltou a `prov=ok` sem intervenção.
- `sysrq b` é o reset que funciona; o `reboot` do BusyBox é ignorado pelo init do aparelho.
- **50 testes automatizados** passando (`unittest`), incluindo política de arquivos,
  confirmação de ações, catálogo e IA.

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
`%LOCALAPPDATA%\\malta-central`, fora do clone do Git. As permissões de segredo usam as
ACLs do perfil de usuário do Windows; os modos POSIX `0600` não se aplicam ao NTFS.

### Limite da compatibilidade Windows

O launcher configura o aplicativo web e a conexão serial. Ele **não configura automaticamente
o compartilhamento de internet/NAT do Windows para a interface RNDIS**. Essa parte do
procedimento documentado — NetworkManager, dispatcher, `iptables` e serviço `systemd` —
é específica do host Linux. No Windows, as funções locais da central e o console serial podem
ser usados sem isso; testes de rede e acesso a serviços em `10.55.0.2` dependem de o adaptador
RNDIS do Windows ter a configuração IP/rota apropriada. O servidor web continua restrito a
`127.0.0.1` e não deve ser exposto diretamente à rede.

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
- O app usa o servidor de desenvolvimento do Flask; para uso local é suficiente. Para
  produção, coloque um WSGI (waitress/gunicorn) atrás de um proxy autenticado.

## Testes

```bash
cd malta-central
MC_DISABLE_THREADS=1 venv/bin/python -m unittest discover -s tests -v
```

`MC_DISABLE_THREADS=1` evita subir poller/provisionador durante os testes; a serial é falsa e
o celular não é necessário. A suíte cobre os contratos legados e a v3: política de arquivos e
path traversal, links simbólicos, confirmação de ações destrutivas, catálogo local, IA sem
configuração/redaction, guard de host/Origin, cabeçalhos e extensão do contrato de `/api/cmd`.

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

## Arquitetura do repositório

```
app.py                         backend Flask: serial, provisionamento, probe, APIs e guard
malta_files.py                 política de caminhos, leitura/escrita/transferência
malta_diagnostics.py           inventário do sistema e da rede
malta_catalog.py               catálogo embutido/local e verificação de estados
malta_ai.py                    configuração, sanitização, redaction e chat OpenAI-compatible
templates/index.html           shell desktop (carrega apenas /static/js/app.js)
static/css/desktop.css         tokens, shell, componentes e responsivo
static/js/api.js               cliente HTTP e contratos
static/js/ui.js                helpers, modal, toast, sparkline
static/js/store.js             estado compartilhado e polling
static/js/router.js            rotas por hash e ciclo de vida das views
static/js/app.js               navegação, chips, statusbar, paleta e atalhos
static/js/views/*.js           home, files, terminal, system, network, apps, assistant,
                               events, settings
tests/test_app.py              18 testes legados (serial falsa)
tests/test_v3.py               32 testes da v3 (política, catálogo, IA, segurança)
reverse_tether_host.sh         fallback manual do PC
reverse_tether_cell.sh         fallback manual do celular (RNDIS)
.env.example                   variáveis de ambiente, sem credenciais
catalog.local.example.json     exemplo de catálogo local (link e serviço TCP)
requirements.txt               flask, pyserial
```

`settings.json` e `catalog.local.json` são estado local, criados em tempo de execução e
ignorados pelo git. O serviço systemd e o dispatcher de NAT vivem fora do repositório, em
`/etc`.

## Contexto do projeto

Bring-up de Linux nativo num Moto E7 Power com bootloader destravado: kernel stock + initramfs
Alpine na partição `boot_a` (rollback = `fastboot flash boot_a boot.img` stock). Descobertas
do bring-up (layout do boot image v2 com DTBO/DTB, major dinâmico 259 das partições, RESTART2
para fastboot remoto, limitações de ops de driver no boot) estão documentadas no repositório
do sistema.

## Licença

MIT — ver [LICENSE](LICENSE).
