#!/bin/sh
# Controle do Malta Desktop no aparelho (Alpine armv7, root).
#
# Sobe o Xvnc em :1 escutando direto na interface USB (10.55.0.2:5901) e
# mantém a sessão X (openbox, tint2, pcmanfm) no chroot /data/malta-desktop/root.
#
# O Xvnc usa -interface $RNDIS_IP, e nao -localhost: neste aparelho (TigerVNC
# 1.16) o -localhost aborta com "No path or port configured for incoming
# connections". Sem proxy: o VNC fica preso a interface USB.
#
# Os processos nascem dentro do chroot; a identificação usa /proc/<pid>/root
# comparado com $CHROOT, nunca "pkill -f $CHROOT/...".
#
# start, restart, stop, status, measure e doctor imprimem chaves KEY=VALOR
# (contrato lido pelo app via /api/cmd); o estado persistido fica em
# /data/malta-desktop/run/desktop.status (STATE/TS/ERROR).
#
# Contrato do status: STATE ∈ {stopped, starting, running, degraded, error};
# INSTALLED, X_READY, DATA e RNDIS são booleanos 1|0. SOCAT_PID continua no
# contrato e vale sempre 0 (nao ha proxy).
#
# Contrato da versão: VERSION é o md5 do install.sh instalado (gravado em
# /data/malta-desktop/version); o PC compara e reinstala quando divergem.
#
# Contrato do measure: uma linha "PROC=<nome> RSS_KB=<n> PSS_KB=<n>" por
# processo do desktop; processo que não existe não imprime linha.
#
# A senha VNC fica em /data/malta-desktop/vncpasswd (8 bytes, VncAuth) e é
# sincronizada para /root/.vnc/passwd do chroot a cada start.
#
# O menu de aplicativos é o jgmenu (painel e Alt+F2); o menu do openbox
# (menu.xml) é aberto por Alt+F1 — o clique direito não chega ao openbox,
# porque o pcmanfm ocupa a root window.

set -u

# Base do bundle no aparelho (a variavel de ambiente deixa os testes
# montarem uma raiz falsa).
BASE=${MALTA_DESKTOP_BASE:-/data/malta-desktop}
CHROOT=$BASE/root
RUN=$BASE/run
LOGS=$BASE/logs
LOG=$LOGS/desktop.log
LOCK=$RUN/desktop.lock
STATUS=$RUN/desktop.status
XVNC_PID_FILE=$RUN/xvnc.pid
SESSION_PID_FILE=$RUN/session.pid
DESKTOP_PID_FILE=$RUN/desktop.pid
BASELINE=$RUN/mem.baseline
VNC_PASSWD_SRC=$BASE/vncpasswd
VNC_PASSWD_DST=$CHROOT/root/.vnc/passwd

# Resolução única da sessão (geometria e profundidade derivadas).
RESOLUTION=1024x600x16
X_GEOMETRY=${RESOLUTION%x*}
X_DEPTH=${RESOLUTION##*x}
DISPLAY_NUM=:1
RNDIS_DEV=rndis0
RNDIS_IP=10.55.0.2
VNC_PORT=5901
X_WAIT=8
XSET_TIMEOUT=5
# Argumentos do Xvnc: escuta na interface USB (-interface), nao em loopback.
XVNC_ARGS="-geometry $X_GEOMETRY -depth $X_DEPTH -rfbport $VNC_PORT -rfbauth /root/.vnc/passwd -SecurityTypes VncAuth -interface $RNDIS_IP"
STOP_WAIT=3
SESSION_WAIT=5
MIN_FREE_KB=20480

# Vocabulario de STATE do contrato (malta_desktop_core.py): stopped,
# starting, running, degraded e error (erro persistido em desktop.status).
ST_STOPPED=stopped
ST_STARTING=starting
ST_RUNNING=running
ST_DEGRADED=degraded
ST_ERROR=error

# Booleanos do contrato: o app compara com "1" (e nao com "yes"/"no").
BOOL_ON=1
BOOL_OFF=0

# O Xvnc e os programas do chroot usam /root como HOME.
HOME=/root
export HOME

# Padrões de cmdline aceitos: a lista é separada por ESPAÇO e percorrida
# padrão a padrão (um "|" não vira alternância do case). O socat fica na
# lista para o stop derrubar o proxy do bundle antigo.
DESKTOP_PATTERNS='/usr/bin/Xvnc* /usr/bin/socat* *malta-session* /usr/bin/openbox* /usr/bin/tint2* /usr/bin/pcmanfm* /usr/bin/xterm* /usr/bin/xsetroot* /usr/bin/setxkbmap* /usr/bin/xkbcomp*'

REQUIRED_BINS="/usr/bin/Xvnc /usr/local/bin/malta-session /usr/bin/openbox /usr/bin/tint2 /usr/bin/pcmanfm /usr/bin/xterm /usr/bin/jgmenu_run"
REQUIRED_CONFIGS="/etc/xdg/openbox/rc.xml /etc/xdg/openbox/menu.xml /etc/xdg/tint2/tint2rc /etc/xdg/pcmanfm/default/pcmanfm.conf /etc/xdg/jgmenu/jgmenurc"

usage() {
    printf 'Uso: %s <comando> [args]\n' "$0"
    printf '  start                sobe Xvnc e a sessao (idempotente)\n'
    printf '  stop                 encerra tudo e limpa socket/lock\n'
    printf '  restart              stop seguido de start\n'
    printf '  status               estado atual em chaves KEY=VALOR\n'
    printf '  measure [--baseline] PSS/RSS dos componentes e memória do sistema\n'
    printf '  doctor               diagnóstico em linhas PASS/FAIL\n'
    printf '  log [n]              últimas n linhas do log (padrão 40)\n'
}

now() {
    date +%s
}

# busybox pode ou não aceitar fração de segundo; o fallback mantém o prazo.
nap() {
    sleep 0.2 >/dev/null 2>&1 || sleep 1
}

log() {
    mkdir -p "$LOGS" 2>/dev/null
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG" 2>/dev/null
}

say() {
    printf '%s\n' "$*"
    log "$*"
}

die() {
    printf 'ERRO: %s\n' "$*" >&2
    exit 1
}

require_root() {
    [ "$(id -u)" = 0 ] || die "execute como root"
}

data_mounted() {
    dm_found=
    while IFS= read -r dm_line; do
        case "$dm_line" in
            *" /data "*) dm_found=1; break ;;
        esac
    done 2>/dev/null </proc/mounts
    [ -n "$dm_found" ]
}

set_status() {
    ss_state=$1
    ss_err=${2:-}
    mkdir -p "$RUN" 2>/dev/null
    {
        printf 'STATE=%s\n' "$ss_state"
        printf 'TS=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'ERROR=%s\n' "$ss_err"
    } >"$STATUS.tmp" 2>/dev/null || return 0
    mv "$STATUS.tmp" "$STATUS" 2>/dev/null || return 0
}

status_field() {
    sf_val=
    while IFS= read -r sf_line; do
        case "$sf_line" in
            "$1="*) sf_val=${sf_line#*=}; break ;;
        esac
    done 2>/dev/null <"$STATUS"
    printf '%s\n' "$sf_val"
}

fail() {
    set_status "$ST_ERROR" "$1"
    log "ERRO: $1"
    printf 'STATE=%s\nERROR=%s\n' "$ST_ERROR" "$1"
    exit 1
}

release_lock() {
    rl_pid=$(cat "$LOCK/pid" 2>/dev/null)
    if [ "$rl_pid" = "$$" ]; then
        rm -rf "$LOCK"
    fi
    return 0
}

# Lock por diretório: mkdir é atômico; lock de processo morto é removido.
acquire_lock() {
    al_i=0
    while [ "$al_i" -lt 5 ]; do
        if mkdir "$LOCK" 2>/dev/null; then
            printf '%s\n' "$$" >"$LOCK/pid"
            return 0
        fi
        al_pid=$(cat "$LOCK/pid" 2>/dev/null)
        case "$al_pid" in
            ''|*[!0-9]*) al_pid=0 ;;
        esac
        if [ "$al_pid" -gt 0 ] && kill -0 "$al_pid" 2>/dev/null; then
            return 1
        fi
        rm -rf "$LOCK"
        al_i=$((al_i + 1))
    done
    return 1
}

pid_alive() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ -r "/proc/$1/stat" ] || return 1
    # O "read" do shell evita um processo extra por consulta.
    pa_line=
    read -r pa_line <"/proc/$1/stat" 2>/dev/null
    [ -n "$pa_line" ] || return 1
    pa_rest=${pa_line##*) }
    [ "${pa_rest%% *}" != "Z" ]
}

pid_in_chroot() {
    pi_root=$(readlink "/proc/${1:-0}/root" 2>/dev/null)
    [ "$pi_root" = "$CHROOT" ]
}

pid_cmdline() {
    tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null
}

# Casa o cmdline com a lista de padrões (mesma regra do scan_pids).
pid_matches() {
    pm_cmd=$(pid_cmdline "$1")
    [ -n "$pm_cmd" ] || return 1
    set -f
    for pm_pat in $2; do
        case "$pm_cmd" in
            $pm_pat) set +f; return 0 ;;
        esac
    done
    set +f
    return 1
}

# Cache da varredura de /proc: um único passe por invocação.
SCAN_CACHE=
SCAN_READY=0

scan_all() {
    [ "$SCAN_READY" = "1" ] && return 0
    SCAN_CACHE=$(
        for sp_dir in /proc/[0-9]*; do
            sp_pid=${sp_dir#/proc/}
            [ "$(readlink "$sp_dir/root" 2>/dev/null)" = "$CHROOT" ] || continue
            sp_cmd=$(tr '\0' ' ' <"$sp_dir/cmdline" 2>/dev/null)
            [ -n "$sp_cmd" ] || continue
            printf '%s %s\n' "$sp_pid" "$sp_cmd"
        done
    )
    SCAN_READY=1
    return 0
}

# Varredura sem /proc/<pid>/cmdline prefixado: só casa o que está no chroot.
# "set -f" no laço: os curingas da lista são padrões do case, não nomes de
# arquivo (sem isso "/usr/bin/socat*" seria expandido e sumiria da lista).
scan_pids() {
    scan_all
    set -f
    printf '%s\n' "$SCAN_CACHE" | while read -r sp_pid sp_cmd; do
        [ -n "$sp_pid" ] || continue
        for sp_pat in $1; do
            case "$sp_cmd" in
                $sp_pat) printf '%s\n' "$sp_pid"; break ;;
            esac
        done
    done
    set +f
}

pidfile_pid() {
    pf_pid=
    read -r pf_pid 2>/dev/null <"$1"
    pid_alive "${pf_pid:-}" || return 1
    pid_in_chroot "$pf_pid" || return 1
    pid_matches "$pf_pid" "$2" || return 1
    printf '%s\n' "$pf_pid"
}

# pidfile só vale se o processo ainda for o mesmo; senão a varredura decide.
component_pid() {
    cp_pid=$(pidfile_pid "$1" "$2") && {
        printf '%s\n' "$cp_pid"
        return 0
    }
    cp_pid=$(scan_pids "$2" | head -n 1)
    [ -n "$cp_pid" ] || return 1
    printf '%s\n' "$cp_pid"
}

# Descobre o pid real do Xvnc: o setsid faz fork quando o processo é líder de
# grupo, e o "$!" do spawn é o pid do pai, que sai na hora.
xvnc_pid() {
    xp_fallback=${1:-}
    xp_try=0
    while [ "$xp_try" -lt 2 ]; do
        SCAN_READY=0
        xp_pid=$(scan_pids '/usr/bin/Xvnc*' | head -n 1)
        [ -n "$xp_pid" ] && {
            printf '%s\n' "$xp_pid"
            return 0
        }
        xp_try=$((xp_try + 1))
        [ "$xp_try" -lt 2 ] && nap
    done
    [ -n "$xp_fallback" ] || return 1
    printf '%s\n' "$xp_fallback"
}

# Publica o processo em segundo plano e imprime o PID (stdin /dev/null).
spawn() {
    if command -v setsid >/dev/null 2>&1; then
        setsid "$@" </dev/null >>"$LOG" 2>&1 &
    else
        "$@" </dev/null >>"$LOG" 2>&1 &
    fi
    # O processo novo ainda não existe no cache de /proc.
    SCAN_READY=0
    printf '%s\n' "$!"
}

desktop_installed() {
    [ -x "$CHROOT/usr/bin/Xvnc" ] &&
        [ -f "$CHROOT/usr/local/bin/malta-session" ]
}

rndis_up() {
    command -v ip >/dev/null 2>&1 || return 1
    ip -4 addr show dev "$RNDIS_DEV" 2>/dev/null | grep -q "inet $RNDIS_IP/"
}

mount_if_needed() {
    mi_dst=$2
    grep -qs " $mi_dst " /proc/mounts && return 0
    mkdir -p "$mi_dst" || return 1
    mount -o bind "$1" "$mi_dst" >>"$LOG" 2>&1
}

# devpts: instancia nova de pseudoterminais no chroot. O bind do /dev do host
# nao traz /dev/pts (sem devpts no host, traz um diretorio vazio) e o xterm
# falha com "get_pty: not enough ptys".
mount_devpts() {
    md_dst=$1
    grep -qs " $md_dst " /proc/mounts && return 0
    mkdir -p "$md_dst" || return 1
    mount -t devpts devpts "$md_dst" >>"$LOG" 2>&1
}

# tmpfs em /dev/shm: e onde o X coloca a memoria compartilhada.
mount_shm() {
    ms_dst=$1
    grep -qs " $ms_dst " /proc/mounts && return 0
    mkdir -p "$ms_dst" || return 1
    mount -t tmpfs -o mode=1777 tmpfs "$ms_dst" >>"$LOG" 2>&1
}

# /dev/ptmx e um symlink para pts/ptmx: o mknod do /dev nao o cria, e sem ele
# o xterm nao abre pty nenhum.
ensure_ptmx() {
    if [ -L "$CHROOT/dev/ptmx" ] && [ "$(readlink "$CHROOT/dev/ptmx")" = "pts/ptmx" ]; then
        return 0
    fi
    ln -sf pts/ptmx "$CHROOT/dev/ptmx" >>"$LOG" 2>&1
}

ensure_mounts() {
    mount_if_needed /proc "$CHROOT/proc" || fail "não foi possível montar /proc em $CHROOT/proc"
    mount_if_needed /dev "$CHROOT/dev" || fail "não foi possível montar /dev em $CHROOT/dev"
    mount_if_needed /sys "$CHROOT/sys" || fail "não foi possível montar /sys em $CHROOT/sys"
    # /dev/pts e /dev/shm são montagens separadas: um bind de /dev não as traz
    # junto, e sem elas o xterm do painel não abre um pty ("not enough ptys")
    # e o X perde o transporte de memória compartilhada. Kernel sem devpts não
    # aborta o start: o aviso fica no log e o doctor acusa (FAIL devpts).
    mount_devpts "$CHROOT/dev/pts" ||
        say "aviso: devpts não montado em $CHROOT/dev/pts; o terminal pode não abrir"
    mount_shm "$CHROOT/dev/shm" ||
        say "aviso: tmpfs não montado em $CHROOT/dev/shm"
    ensure_ptmx ||
        say "aviso: symlink $CHROOT/dev/ptmx -> pts/ptmx não criado"
}

cleanup_x_socket() {
    rm -f "$CHROOT/tmp/.X${DISPLAY_NUM#:}-lock" \
        "$CHROOT/tmp/.X11-unix/X${DISPLAY_NUM#:}" \
        "$CHROOT/tmp/.X11-unix/X${DISPLAY_NUM#:}.lock"
}

sync_vncpasswd() {
    [ -f "$VNC_PASSWD_SRC" ] || fail "senha VNC ausente: $VNC_PASSWD_SRC (reenvie pelo app)"
    sv_size=$(wc -c <"$VNC_PASSWD_SRC" 2>/dev/null | tr -d ' ')
    [ "$sv_size" = "8" ] || fail "senha VNC inválida: ${sv_size:-0} bytes (esperado 8); reenvie pelo app"
    mkdir -p "${VNC_PASSWD_DST%/*}" || fail "não foi possível criar /root/.vnc no chroot"
    cp "$VNC_PASSWD_SRC" "$VNC_PASSWD_DST" || fail "não foi possível copiar a senha VNC para o chroot"
    chmod 600 "$VNC_PASSWD_DST"
}

x_ready() {
    xr_wait=${1:-$XSET_TIMEOUT}
    [ "$xr_wait" -gt "$XSET_TIMEOUT" ] && xr_wait=$XSET_TIMEOUT
    # O xset sem prazo trava a serial (socket X obsoleto bloqueia por
    # minutos). O timeout corta a espera; saida != 0 = "o X nao responde".
    timeout "$xr_wait" chroot "$CHROOT" /usr/bin/xset -display "$DISPLAY_NUM" q \
        >/dev/null 2>&1
}

# Espera o X responder por xset, no máximo X_WAIT segundos. Se o Xvnc morrer,
# devolve 1 na hora para o chamador decidir o retry.
wait_x() {
    wx_pid=${1:-}
    wx_end=$(( $(now) + X_WAIT ))
    while :; do
        wx_left=$(( wx_end - $(now) ))
        [ "$wx_left" -gt 0 ] || return 1
        x_ready "$wx_left" && return 0
        pid_in_chroot "$wx_pid" || return 1
        nap
    done
}

# A porta 5901 ocupada: o desktop anterior não foi encerrado.
vnc_port_busy() {
    tcp_listening "$RNDIS_IP" "$VNC_PORT" && return 0
    tcp_listening "127.0.0.1" "$VNC_PORT" && return 0
    tcp_listening "0.0.0.0" "$VNC_PORT" && return 0
    return 1
}

start_xvnc() {
    sx_pid=$(component_pid "$XVNC_PID_FILE" '/usr/bin/Xvnc*') && {
        say "Xvnc já ativo (pid $sx_pid)"
        return 0
    }
    rm -f "$XVNC_PID_FILE"
    if vnc_port_busy; then
        fail "a porta $VNC_PORT já está em uso; o desktop anterior pode não ter sido encerrado (rode stop)"
    fi
    cleanup_x_socket
    sx_spawn=$(spawn chroot "$CHROOT" /usr/bin/Xvnc "$DISPLAY_NUM" $XVNC_ARGS)
    sx_pid=$(xvnc_pid "$sx_spawn")
    printf '%s\n' "$sx_pid" >"$XVNC_PID_FILE"
    if wait_x "$sx_pid"; then
        say "Xvnc ativo (pid $sx_pid) em $DISPLAY_NUM $X_GEOMETRY (VNC em $RNDIS_IP:$VNC_PORT)"
        return 0
    fi
    if pid_in_chroot "$sx_pid"; then
        fail "Xvnc está vivo (pid $sx_pid) mas não respondeu em $DISPLAY_NUM; veja $LOG"
    fi
    if vnc_port_busy; then
        fail "a porta $VNC_PORT já está em uso; o desktop anterior pode não ter sido encerrado (rode stop)"
    fi
    say "Xvnc encerrou na primeira tentativa; repetindo com -extension GLX"
    cleanup_x_socket
    sx_spawn=$(spawn chroot "$CHROOT" /usr/bin/Xvnc "$DISPLAY_NUM" $XVNC_ARGS -extension GLX)
    sx_pid=$(xvnc_pid "$sx_spawn")
    printf '%s\n' "$sx_pid" >"$XVNC_PID_FILE"
    wait_x "$sx_pid" || fail "Xvnc não respondeu em $DISPLAY_NUM após duas tentativas; veja $LOG"
    say "Xvnc ativo (pid $sx_pid) em $DISPLAY_NUM $X_GEOMETRY (GLX desativado)"
}

session_pid() {
    component_pid "$SESSION_PID_FILE" '*malta-session*'
}

# A sessão publica os PIDs em /tmp/malta-session.pids; o pcmanfm é o marco de
# "desktop pronto".
wait_session() {
    ws_pid=$1
    ws_end=$(( $(now) + SESSION_WAIT ))
    while [ "$(now)" -lt "$ws_end" ]; do
        ws_desk=$(sed -n 's/^desktop=//p' "$CHROOT/tmp/malta-session.pids" 2>/dev/null | head -n 1)
        if pid_alive "${ws_desk:-}"; then
            printf '%s\n' "$ws_desk" >"$DESKTOP_PID_FILE"
            say "sessão ativa (pid $ws_pid, desktop pid $ws_desk)"
            return 0
        fi
        pid_in_chroot "$ws_pid" || fail "sessão encerrou durante a inicialização; veja $CHROOT/tmp/session.log"
        nap
    done
    say "aviso: sessão iniciada (pid $ws_pid) mas o desktop ainda não publicou o pid; veja $CHROOT/tmp/session.log"
}

start_session() {
    ses_pid=$(session_pid) && {
        say "sessão já ativa (pid $ses_pid)"
        return 0
    }
    rm -f "$SESSION_PID_FILE" "$DESKTOP_PID_FILE" "$CHROOT/tmp/malta-session.pids"
    ses_pid=$(spawn chroot "$CHROOT" /bin/sh /usr/local/bin/malta-session)
    printf '%s\n' "$ses_pid" >"$SESSION_PID_FILE"
    wait_session "$ses_pid"
}

cmd_start() {
    require_root
    mkdir -p "$RUN" "$LOGS" || die "não foi possível criar $RUN"
    data_mounted || fail "/data não está montado; use a ação Montar /data no app"
    desktop_installed || fail "Malta Desktop não instalado em $BASE; rode o install.sh pelo app"
    acquire_lock || fail "operação em andamento (lock $LOCK)"
    log "start solicitado"
    set_status "$ST_STARTING" ""

    sync_vncpasswd
    ensure_mounts
    rndis_up || fail "$RNDIS_DEV sem $RNDIS_IP: configure o USB no app antes de iniciar o desktop"

    scan_all
    start_xvnc
    # O Xvnc nasceu depois da varredura: o cache de /proc ficou velho.
    SCAN_READY=0
    start_session
    SCAN_READY=0

    set_status "$ST_RUNNING" ""
    say "Malta Desktop ativo: VNC em $RNDIS_IP:$VNC_PORT ($RESOLUTION)"
    release_lock
    printf 'STATE=%s\nGEOMETRY=%s\nVNC=%s:%s\n' "$ST_RUNNING" "$X_GEOMETRY" "$RNDIS_IP" "$VNC_PORT"
}

# Sessão primeiro (o trap dela encerra os filhos), depois o restante.
desktop_pids() {
    dp_first=$(session_pid) || dp_first=
    if [ -n "${dp_first:-}" ]; then
        printf '%s\n' "$dp_first"
    fi
    for dp_pid in $(scan_pids "$DESKTOP_PATTERNS"); do
        [ "$dp_pid" = "${dp_first:-}" ] && continue
        printf '%s\n' "$dp_pid"
    done
}

stop_components() {
    sc_pids=$(desktop_pids)
    if [ -z "$sc_pids" ]; then
        say "nada em execução"
        return 0
    fi
    say "encerrando:$sc_pids"
    for sc_pid in $sc_pids; do
        kill -TERM "$sc_pid" 2>/dev/null || true
    done
    sc_left=$sc_pids
    sc_i=0
    while [ "$sc_i" -lt "$STOP_WAIT" ]; do
        sc_live=
        for sc_pid in $sc_left; do
            pid_alive "$sc_pid" && sc_live="$sc_live $sc_pid"
        done
        [ -z "$sc_live" ] && return 0
        sc_left=$sc_live
        sc_i=$((sc_i + 1))
        sleep 1
    done
    say "forçando término:$sc_left"
    for sc_pid in $sc_left; do
        kill -KILL "$sc_pid" 2>/dev/null || true
    done
    sleep 1
    sc_live=
    for sc_pid in $sc_left; do
        pid_alive "$sc_pid" && sc_live="$sc_live $sc_pid"
    done
    # O stop é a garantia de que nenhum processo do desktop fica órfão.
    SCAN_READY=0
    sx_left=
    for sx_pid in $(scan_pids '/usr/bin/Xvnc*'); do
        pid_alive "$sx_pid" && sx_left="$sx_left $sx_pid"
    done
    [ -z "$sx_left" ] || fail "o Xvnc sobreviveu ao stop:$sx_left"
    [ -z "$sc_live" ] || fail "processos resistiram ao KILL:$sc_live"
    return 0
}

meminfo_kb() {
    mi_val=
    while read -r mi_key mi_value _mi_rest; do
        case "$mi_key" in
            "$1":) mi_val=$mi_value; break ;;
        esac
    done </proc/meminfo 2>/dev/null
    printf '%s\n' "$mi_val"
}

mem_available_kb() {
    ma_val=$(meminfo_kb MemAvailable)
    case "$ma_val" in
        ''|*[!0-9]*) ma_val=$(meminfo_kb MemFree) ;;
    esac
    case "$ma_val" in
        ''|*[!0-9]*) ma_val=0 ;;
    esac
    printf '%s\n' "$ma_val"
}

# Referência de memória com o desktop parado; measure compara com ela.
record_baseline() {
    rb_now=$(mem_available_kb)
    [ "$rb_now" -gt 0 ] || return 0
    printf '%s\n' "$rb_now" >"$BASELINE" 2>/dev/null || true
}

cmd_stop() {
    require_root
    mkdir -p "$RUN" || die "não foi possível criar $RUN"
    acquire_lock || die "operação em andamento (lock $LOCK)"
    log "stop solicitado"
    scan_all
    stop_components
    SCAN_READY=0
    cleanup_x_socket
    rm -f "$XVNC_PID_FILE" "$SESSION_PID_FILE" "$DESKTOP_PID_FILE" \
        "$CHROOT/tmp/malta-session.pids"
    record_baseline
    set_status "$ST_STOPPED" ""
    say "Malta Desktop parado"
    release_lock
    printf 'STATE=%s\n' "$ST_STOPPED"
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_status() {
    scan_all
    st_installed=$BOOL_OFF
    desktop_installed && st_installed=$BOOL_ON
    st_data=$BOOL_OFF
    data_mounted && st_data=$BOOL_ON
    st_rndis=$BOOL_OFF
    rndis_up && st_rndis=$BOOL_ON
    st_xvnc=0
    st_xready=$BOOL_OFF
    if [ "$st_installed" = "$BOOL_ON" ]; then
        st_xvnc=$(component_pid "$XVNC_PID_FILE" '/usr/bin/Xvnc*') || st_xvnc=0
        x_ready && st_xready=$BOOL_ON
    fi
    st_persisted=$(status_field STATE)
    st_perr=$(status_field ERROR)
    if [ "$st_installed" != "$BOOL_ON" ]; then
        # Sem o desktop instalado o estado e "stopped" (INSTALLED=0).
        st_state=$ST_STOPPED
        st_err=
    elif [ "$st_persisted" = "$ST_ERROR" ]; then
        # "error" fica reservado ao erro persistido em run/desktop.status.
        st_state=$ST_ERROR
        st_err=$st_perr
    elif [ "$st_xvnc" != 0 ] && [ "$st_xready" = "$BOOL_ON" ]; then
        st_state=$ST_RUNNING
        st_err=
    elif [ "$st_xvnc" = 0 ]; then
        st_state=$ST_STOPPED
        st_err=
    else
        # Componentes parciais: o app le "degraded". ERROR fica vazio de
        # proposito: um ERROR preenchido vira a fase "error" no PC.
        st_state=$ST_DEGRADED
        st_err=
    fi
    # A versao do bundle e o md5 do install.sh instalado (32 hex); o PC trata
    # o formato numerico antigo como divergencia (reinstalar).
    st_version=0
    if [ -f "$BASE/version" ]; then
        st_version=$(head -n 1 "$BASE/version" 2>/dev/null)
        case "$st_version" in
            ''|*[!0-9a-zA-Z._-]*) st_version=0 ;;
        esac
    fi
    printf 'STATE=%s\n' "$st_state"
    printf 'INSTALLED=%s\n' "$st_installed"
    printf 'DATA=%s\n' "$st_data"
    printf 'RNDIS=%s\n' "$st_rndis"
    printf 'XVNC_PID=%s\n' "$st_xvnc"
    # O proxy saiu: a chave continua no contrato e vale sempre 0.
    printf 'SOCAT_PID=0\n'
    printf 'X_READY=%s\n' "$st_xready"
    printf 'GEOMETRY=%s\n' "$X_GEOMETRY"
    printf 'VERSION=%s\n' "$st_version"
    printf 'ERROR=%s\n' "$st_err"
}

# Memoria dos processos: lida uma vez por PID e guardada em cache
# ("pid:rss:pss"). O smaps custa segundos no aparelho (kernel 4.9).
MEM_CACHE=
PM_RSS=0
PM_PSS=0

# Le o smaps e soma o PSS (KB): o smaps_rollup e preferido (uma linha so).
read_smaps_pss() {
    rp_pid=$1
    if [ -r "/proc/$rp_pid/smaps_rollup" ]; then
        rp_file="/proc/$rp_pid/smaps_rollup"
    else
        rp_file="/proc/$rp_pid/smaps"
    fi
    [ -r "$rp_file" ] || return 1
    PM_PSS=0
    while read -r rp_key rp_value _rp_rest; do
        [ "$rp_key" = "Pss:" ] || continue
        case "$rp_value" in
            ''|*[!0-9]*) continue ;;
        esac
        PM_PSS=$((PM_PSS + rp_value))
    done <"$rp_file"
    return 0
}

# Le o status de um processo e pega o RSS (KB).
read_status_rss() {
    rr_pid=$1
    [ -r "/proc/$rr_pid/status" ] || return 1
    PM_RSS=0
    while read -r rr_key rr_value _rr_rest; do
        [ "$rr_key" = "VmRSS:" ] || continue
        case "$rr_value" in
            ''|*[!0-9]*) return 0 ;;
        esac
        PM_RSS=$rr_value
        return 0
    done <"/proc/$rr_pid/status"
    return 0
}

# RSS e PSS de um processo, lidos uma unica vez por PID (o cache vive no
# shell: um subshell perderia o cache).
mem_proc_kb() {
    mp_pid=${1:-}
    case "$mp_pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    case " $MEM_CACHE " in
        *" $mp_pid:"*)
            mp_rest=${MEM_CACHE#*" $mp_pid:"}
            PM_RSS=${mp_rest%%:*}
            mp_rest=${mp_rest#*:}
            PM_PSS=${mp_rest%% *}
            return 0
            ;;
    esac
    PM_RSS=0
    PM_PSS=0
    mp_found=0
    read_smaps_pss "$mp_pid" && mp_found=1
    read_status_rss "$mp_pid" && mp_found=1
    [ "$mp_found" = 1 ] || return 1
    MEM_CACHE="$MEM_CACHE $mp_pid:$PM_RSS:$PM_PSS"
    return 0
}

# PID de um componente publicado pela sessao em malta-session.pids.
session_pid_field() {
    spf_val=
    while IFS= read -r spf_line; do
        case "$spf_line" in
            "$1="*) spf_val=${spf_line#*=}; break ;;
        esac
    done 2>/dev/null <"$CHROOT/tmp/malta-session.pids"
    printf '%s\n' "$spf_val"
}

# Uma linha por processo do desktop; processo ausente nao emite linha.
emit_proc() {
    ep_name=$1
    ep_pid=$2
    case "$ep_pid" in
        ''|*[!0-9]*) return 0 ;;
    esac
    pid_alive "$ep_pid" || return 0
    mem_proc_kb "$ep_pid" || return 0
    printf 'PROC=%s RSS_KB=%s PSS_KB=%s\n' "$ep_name" "$PM_RSS" "$PM_PSS"
}

cmd_measure() {
    scan_all
    mm_save=0
    for mm_arg in "$@"; do
        case "$mm_arg" in
            --baseline) mm_save=1 ;;
            *)
                usage >&2
                die "argumento desconhecido: $mm_arg"
                ;;
        esac
    done
    if [ "$mm_save" = 1 ]; then
        mm_now=$(mem_available_kb)
        [ "$mm_now" -gt 0 ] && printf '%s\n' "$mm_now" >"$BASELINE" 2>/dev/null
    fi
    mm_xvnc=$(component_pid "$XVNC_PID_FILE" '/usr/bin/Xvnc*') || mm_xvnc=0
    mm_sess=$(session_pid) || mm_sess=0
    mm_others=
    for mm_pid in $(desktop_pids); do
        [ "$mm_pid" = "$mm_xvnc" ] && continue
        mm_others="$mm_others $mm_pid"
    done
    mm_total=$(meminfo_kb MemTotal)
    case "$mm_total" in
        ''|*[!0-9]*) mm_total=0 ;;
    esac
    mm_avail=$(mem_available_kb)
    mm_base=0
    if [ -f "$BASELINE" ]; then
        mm_base=$(head -n 1 "$BASELINE" 2>/dev/null)
    fi
    case "$mm_base" in
        ''|*[!0-9]*) mm_base=0 ;;
    esac
    mm_delta=$((mm_base - mm_avail))
    [ "$mm_delta" -lt 0 ] && mm_delta=0
    # Memoria de cada processo: uma leitura por PID, no shell do comando.
    mm_xvnc_rss=0
    mm_xvnc_pss=0
    mm_sess_rss=0
    mm_sess_pss=0
    for mm_pid in $mm_xvnc $mm_others; do
        mem_proc_kb "$mm_pid" || continue
        case "$mm_pid" in
            "$mm_xvnc")
                mm_xvnc_rss=$PM_RSS
                mm_xvnc_pss=$PM_PSS
                ;;
            *)
                mm_sess_rss=$((mm_sess_rss + PM_RSS))
                mm_sess_pss=$((mm_sess_pss + PM_PSS))
                ;;
        esac
    done
    mm_total_rss=$((mm_xvnc_rss + mm_sess_rss))
    mm_total_pss=$((mm_xvnc_pss + mm_sess_pss))
    printf 'MEMTOTAL_KB=%s\n' "$mm_total"
    printf 'MEMAVAILABLE_KB=%s\n' "$mm_avail"
    printf 'BASELINE_MEMAVAILABLE_KB=%s\n' "$mm_base"
    printf 'BASELINE_DELTA_KB=%s\n' "$mm_delta"
    printf 'XVNC_PID=%s\n' "$mm_xvnc"
    printf 'XVNC_PSS_KB=%s\n' "$mm_xvnc_pss"
    printf 'XVNC_RSS_KB=%s\n' "$mm_xvnc_rss"
    printf 'SESSION_PID=%s\n' "$mm_sess"
    printf 'SESSION_PSS_KB=%s\n' "$mm_sess_pss"
    printf 'SESSION_RSS_KB=%s\n' "$mm_sess_rss"
    printf 'TOTAL_PSS_KB=%s\n' "$mm_total_pss"
    printf 'TOTAL_RSS_KB=%s\n' "$mm_total_rss"
    emit_proc Xvnc "$mm_xvnc"
    emit_proc openbox "$(session_pid_field openbox)"
    emit_proc tint2 "$(session_pid_field tint2)"
    emit_proc pcmanfm "$(session_pid_field pcmanfm)"
    emit_proc xterm "$(scan_pids '/usr/bin/xterm*' | head -n 1)"
}

hex_addr() {
    printf '%s\n' "$1" | awk -F. '{ printf "%02X%02X%02X%02X\n", $4, $3, $2, $1 }'
}

# Lê /proc/net/tcp (estado 0A = LISTEN) para não depender de netstat/ss.
tcp_listening() {
    tl_want=$(hex_addr "$1"):$(printf '%04X\n' "$2")
    awk -v want="$tl_want" '$4 == "0A" { if (toupper($2) == want) found = 1 } END { exit(found ? 0 : 1) }' \
        /proc/net/tcp 2>/dev/null
}

DR_OK=0
DR_FAIL=0

dr_pass() {
    DR_OK=$((DR_OK + 1))
    printf 'PASS %s\n' "$1"
}

dr_fail() {
    DR_FAIL=$((DR_FAIL + 1))
    printf 'FAIL %s: %s\n' "$1" "$2"
}

cmd_doctor() {
    scan_all
    DR_OK=0
    DR_FAIL=0
    if [ "$(id -u)" = 0 ]; then
        dr_pass root
    else
        dr_fail root "execute como root para diagnóstico completo"
    fi
    if data_mounted; then
        dr_pass data
    else
        dr_fail data "/data não está montado"
    fi
    if [ -d "$BASE" ]; then
        dr_pass base
    else
        dr_fail base "ausente: $BASE"
    fi

    dr_missing=
    for dr_bin in $REQUIRED_BINS; do
        [ -x "$CHROOT$dr_bin" ] || dr_missing="$dr_missing $dr_bin"
    done
    if [ -z "$dr_missing" ]; then
        dr_pass binarios
    else
        dr_fail binarios "ausentes no chroot:$dr_missing"
    fi

    dr_missing=
    for dr_cfg in $REQUIRED_CONFIGS; do
        [ -s "$CHROOT$dr_cfg" ] || dr_missing="$dr_missing $dr_cfg"
    done
    if [ -z "$dr_missing" ]; then
        dr_pass configs
    else
        dr_fail configs "ausentes ou vazios:$dr_missing"
    fi

    dr_size=$(wc -c <"$VNC_PASSWD_DST" 2>/dev/null | tr -d ' ')
    if [ "${dr_size:-0}" = "8" ]; then
        dr_pass vncpasswd
    else
        dr_fail vncpasswd "esperado 8 bytes em $VNC_PASSWD_DST; reenvie a senha pelo app"
    fi

    dr_mounts=
    for dr_mnt in proc dev sys; do
        grep -qs " $CHROOT/$dr_mnt " /proc/mounts || dr_mounts="$dr_mounts $dr_mnt"
    done
    if [ -z "$dr_mounts" ]; then
        dr_pass mounts
    else
        dr_fail mounts "não montados:$dr_mounts"
    fi

    # As tres pecas do terminal: sem devpts o xterm nao abre pty ("not enough
    # ptys"), sem o symlink ptmx o mesmo xterm falha, e sem shm o X perde a
    # memoria compartilhada.
    if grep -qs " $CHROOT/dev/pts devpts " /proc/mounts; then
        dr_pass devpts
    else
        dr_fail devpts "devpts não montado em $CHROOT/dev/pts"
    fi

    if [ -L "$CHROOT/dev/ptmx" ] && [ "$(readlink "$CHROOT/dev/ptmx")" = "pts/ptmx" ]; then
        dr_pass ptmx
    else
        dr_fail ptmx "symlink ausente ou errado: $CHROOT/dev/ptmx -> pts/ptmx"
    fi

    if grep -qs " $CHROOT/dev/shm tmpfs " /proc/mounts; then
        dr_pass shm
    else
        dr_fail shm "tmpfs não montado em $CHROOT/dev/shm"
    fi

    dr_free=$(df -Pk "$BASE" 2>/dev/null | awk 'NR==2 {print $4}')
    case "$dr_free" in
        ''|*[!0-9]*) dr_free=0 ;;
    esac
    if [ "$dr_free" -ge "$MIN_FREE_KB" ]; then
        dr_pass espaco
    else
        dr_fail espaco "$((dr_free / 1024)) MB livres em $BASE (mínimo $((MIN_FREE_KB / 1024)) MB)"
    fi

    if rndis_up; then
        dr_pass rndis
    else
        dr_fail rndis "$RNDIS_DEV sem $RNDIS_IP"
    fi

    dr_xvnc=$(component_pid "$XVNC_PID_FILE" '/usr/bin/Xvnc*') || dr_xvnc=0
    if [ "$dr_xvnc" != 0 ]; then
        dr_pass xvnc
    else
        dr_fail xvnc "não está ativo (use start)"
    fi

    if x_ready; then
        dr_pass x
    else
        dr_fail x "$DISPLAY_NUM não responde a xset"
    fi

    # O VNC e o proprio Xvnc: o teste e a porta 5901 em $RNDIS_IP.
    if tcp_listening "$RNDIS_IP" "$VNC_PORT"; then
        dr_pass vnc
    else
        dr_fail vnc "ninguém escutando em $RNDIS_IP:$VNC_PORT"
    fi

    dr_sess=$(session_pid) || dr_sess=0
    if [ "$dr_sess" != 0 ]; then
        dr_pass sessao
    else
        dr_fail sessao "sessão não está ativa (use start)"
    fi

    dr_version=
    [ -f "$BASE/version" ] && dr_version=$(head -n 1 "$BASE/version" 2>/dev/null)
    # A versao do bundle e o md5 do install.sh instalado: 32 hex.
    case "$dr_version" in
        *[!0-9a-f]*) dr_version= ;;
    esac
    if [ "${#dr_version}" -eq 32 ]; then
        dr_pass versao
    else
        dr_fail versao "arquivo $BASE/version ausente ou invalido (esperado md5 do install.sh)"
    fi

    printf 'OK=%s\n' "$DR_OK"
    printf 'FALHAS=%s\n' "$DR_FAIL"
    if [ "$DR_FAIL" -eq 0 ]; then
        printf 'RESULT=PASS\n'
    else
        printf 'RESULT=FAIL\n'
    fi
    [ "$DR_FAIL" -eq 0 ]
}

cmd_log() {
    lg_n=${1:-40}
    case "$lg_n" in
        ''|*[!0-9]*) die "número de linhas inválido: $lg_n" ;;
    esac
    [ "$lg_n" -gt 0 ] || die "número de linhas inválido: $lg_n"
    [ -f "$LOG" ] || die "sem log em $LOG"
    tail -n "$lg_n" "$LOG"
}

CMD=${1:-}
[ "$#" -gt 0 ] && shift

trap 'release_lock' 0

case "$CMD" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    restart) cmd_restart ;;
    status) cmd_status ;;
    measure) cmd_measure "$@" ;;
    doctor) cmd_doctor ;;
    log) cmd_log "$@" ;;
    ''|-h|--help|help) usage ;;
    *)
        usage >&2
        die "comando desconhecido: $CMD"
        ;;
esac
