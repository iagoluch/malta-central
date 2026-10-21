#!/bin/sh
# Sessao X do Malta Desktop (roda dentro do chroot Alpine armv7).
#
# Chamado por malta-desktop.sh:
#   chroot /data/malta-desktop/root /bin/sh /usr/local/bin/malta-session
#
# Contrato com malta-desktop.sh:
#   - publica /tmp/malta-session.pids com a linha "desktop=<pid do pcmanfm>".
#     Esse pid e o marco de "desktop pronto" lido pelo start.
#   - grava o log da sessao em /tmp/session.log (dentro do chroot); e o
#     arquivo que o start aponta quando a sessao falha.
#   - ao receber TERM/INT, encerra os filhos, apaga o pids file e sai com 0.
#
# A sessao fica em foreground e supervisiona os filhos: cada filho morto e
# reiniciado; se um mesmo componente morrer 5 vezes em menos de 1 minuto, a
# sessao registra o erro, encerra os filhos e sai com status 1, sem deixar
# processos orfaos.
#
# POSIX sh (busybox ash): sem bashismos, sem arrays, sem teste duplo.

set -u

DISPLAY_NUM=${MALTA_DISPLAY_NUM:-:1}
HOME_DIR=${MALTA_HOME:-/root}
SESSION_LOG=/tmp/session.log
PID_FILE=/tmp/malta-session.pids
OPENBOX_CONF=/etc/xdg/openbox/rc.xml
TINT2_CONF=/etc/xdg/tint2/tint2rc
WALLPAPER='#300a24'
KEYBOARD=br
X_WAIT=10
SUPERVISE_INTERVAL=5
RESTART_LIMIT=5
RESTART_WINDOW=60
STOP_GRACE=3

export HOME=$HOME_DIR
export LANG=C.UTF-8
export DISPLAY=$DISPLAY_NUM
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
# Sem XDG_CONFIG_DIRS o jgmenu (e o GTK) nao acham /etc/xdg: a configuracao
# do menu em /etc/xdg/jgmenu/jgmenurc seria ignorada e o menu apareceria com
# o tema padrao e sem os aplicativos do bundle.
export XDG_CONFIG_DIRS=/etc/xdg
export XDG_DATA_DIRS=/usr/share

OPENBOX_PID=
TINT2_PID=
PCMANFM_PID=
OPENBOX_FAILS=
TINT2_FAILS=
PCMANFM_FAILS=
STOPPING=0

# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------

now_ts() {
    date +%s
}

log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$SESSION_LOG" 2>/dev/null
}

# Um processo que virou zumbi continua com PID valido, entao kill -0 nao
# basta: o estado no /proc e o que separa "morreu" de "vivo".
pid_alive() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ -r "/proc/$1/stat" ] || return 1
    pa_state=$(awk '{print $3}' "/proc/$1/stat" 2>/dev/null)
    [ "$pa_state" != "Z" ]
}

# ----------------------------------------------------------------------
# Componentes do desktop
# ----------------------------------------------------------------------

start_openbox() {
    /usr/bin/openbox --config-file "$OPENBOX_CONF" >>"$SESSION_LOG" 2>&1 &
    OPENBOX_PID=$!
}

start_tint2() {
    /usr/bin/tint2 -c "$TINT2_CONF" >>"$SESSION_LOG" 2>&1 &
    TINT2_PID=$!
}

start_pcmanfm() {
    /usr/bin/pcmanfm --desktop >>"$SESSION_LOG" 2>&1 &
    PCMANFM_PID=$!
}

# ----------------------------------------------------------------------
# Arquivo de PIDs publicado
# ----------------------------------------------------------------------

write_pids() {
    wp_tmp=$PID_FILE.tmp
    {
        printf 'session=%s\n' "$$"
        printf 'openbox=%s\n' "${OPENBOX_PID:-}"
        printf 'tint2=%s\n' "${TINT2_PID:-}"
        printf 'pcmanfm=%s\n' "${PCMANFM_PID:-}"
        printf 'desktop=%s\n' "${PCMANFM_PID:-}"
    } >"$wp_tmp" 2>/dev/null || return 0
    mv "$wp_tmp" "$PID_FILE" 2>/dev/null || rm -f "$wp_tmp" 2>/dev/null
    return 0
}

cleanup() {
    rm -f "$PID_FILE" "$PID_FILE.tmp" 2>/dev/null
    return 0
}

# ----------------------------------------------------------------------
# Inicializacao
# ----------------------------------------------------------------------

# O X precisa responder antes de qualquer componente abrir conexao.
wait_x() {
    wx_end=$(( $(now_ts) + X_WAIT ))
    while :; do
        if /usr/bin/xset -display "$DISPLAY_NUM" q >/dev/null 2>&1; then
            return 0
        fi
        [ "$(now_ts)" -lt "$wx_end" ] || return 1
        sleep 1
    done
}

startup() {
    if ! wait_x; then
        log "ERRO: o X nao respondeu em $DISPLAY_NUM apos ${X_WAIT}s"
        return 1
    fi
    log "X pronto em $DISPLAY_NUM"

    if ! /usr/bin/setxkbmap -layout "$KEYBOARD" >>"$SESSION_LOG" 2>&1; then
        log "aviso: setxkbmap -layout $KEYBOARD falhou; o teclado mantem o layout anterior"
    fi
    if ! /usr/bin/xsetroot -solid "$WALLPAPER" >>"$SESSION_LOG" 2>&1; then
        log "aviso: xsetroot -solid $WALLPAPER falhou"
    fi

    start_openbox
    log "openbox iniciado (pid $OPENBOX_PID)"
    sleep 1

    start_tint2
    log "tint2 iniciado (pid $TINT2_PID)"

    start_pcmanfm
    log "pcmanfm iniciado (pid $PCMANFM_PID)"
    write_pids
    return 0
}

# ----------------------------------------------------------------------
# Supervisao
# ----------------------------------------------------------------------

# Registro as falhas por componente: se o mesmo componente morrer RESTART_LIMIT
# vezes dentro de RESTART_WINDOW segundos, a sessao esta perdida.
component_failed() {
    cf_name=$1
    cf_now=$(now_ts)
    case "$cf_name" in
        openbox) cf_fails=$OPENBOX_FAILS ;;
        tint2) cf_fails=$TINT2_FAILS ;;
        pcmanfm) cf_fails=$PCMANFM_FAILS ;;
        *) cf_fails= ;;
    esac
    cf_kept=
    for cf_ts in $cf_fails; do
        if [ $((cf_now - cf_ts)) -lt "$RESTART_WINDOW" ]; then
            cf_kept="$cf_kept $cf_ts"
        fi
    done
    cf_kept="$cf_kept $cf_now"
    cf_count=0
    for cf_ts in $cf_kept; do
        cf_count=$((cf_count + 1))
    done
    if [ "$cf_count" -ge "$RESTART_LIMIT" ]; then
        log "ERRO: $cf_name falhou $cf_count vezes em menos de ${RESTART_WINDOW}s; encerrando a sessao"
        return 1
    fi
    case "$cf_name" in
        openbox) OPENBOX_FAILS=$cf_kept ;;
        tint2) TINT2_FAILS=$cf_kept ;;
        pcmanfm) PCMANFM_FAILS=$cf_kept ;;
    esac
    return 0
}

# Verifica um componente; devolve 1 quando ele esgotou as tentativas.
check_component() {
    cc_name=$1
    case "$cc_name" in
        openbox) cc_pid=$OPENBOX_PID ;;
        tint2) cc_pid=$TINT2_PID ;;
        pcmanfm) cc_pid=$PCMANFM_PID ;;
        *) cc_pid= ;;
    esac
    if [ -n "$cc_pid" ] && pid_alive "$cc_pid"; then
        return 0
    fi
    if [ -n "$cc_pid" ]; then
        wait "$cc_pid" 2>/dev/null
        log "$cc_name encerrou (pid $cc_pid)"
    else
        log "$cc_name nunca foi iniciado"
    fi
    if ! component_failed "$cc_name"; then
        return 1
    fi
    case "$cc_name" in
        openbox) start_openbox; cc_new=$OPENBOX_PID ;;
        tint2) start_tint2; cc_new=$TINT2_PID ;;
        pcmanfm) start_pcmanfm; cc_new=$PCMANFM_PID ;;
        *) cc_new= ;;
    esac
    log "$cc_name reiniciado (pid $cc_new)"
    write_pids
    return 0
}

# Espera em pedacos de 1 s: um sinal chega rapido e o trap roda em seguida.
tick() {
    tk_i=0
    while [ "$tk_i" -lt "$SUPERVISE_INTERVAL" ]; do
        sleep 1
        tk_i=$((tk_i + 1))
        [ "$STOPPING" = "0" ] || return 0
    done
    return 0
}

# ----------------------------------------------------------------------
# Encerramento
# ----------------------------------------------------------------------

stop_children() {
    sc_pids="$OPENBOX_PID $TINT2_PID $PCMANFM_PID"
    for sc_pid in $sc_pids; do
        [ -n "$sc_pid" ] || continue
        kill -TERM "$sc_pid" 2>/dev/null
    done
    sc_i=0
    while [ "$sc_i" -lt "$STOP_GRACE" ]; do
        sc_live=
        for sc_pid in $sc_pids; do
            [ -n "$sc_pid" ] || continue
            if pid_alive "$sc_pid"; then
                sc_live="$sc_live $sc_pid"
            fi
        done
        [ -z "$sc_live" ] && break
        sleep 1
        sc_i=$((sc_i + 1))
    done
    for sc_pid in $sc_pids; do
        [ -n "$sc_pid" ] || continue
        kill -KILL "$sc_pid" 2>/dev/null
    done
    return 0
}

on_term() {
    STOPPING=1
    log "sessao encerrada por sinal"
    stop_children
    cleanup
    exit 0
}

fail_session() {
    log "ERRO: sessao encerrada por falha repetida de componente"
    stop_children
    cleanup
    exit 1
}

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

main() {
    trap on_term TERM INT
    : >"$SESSION_LOG" 2>/dev/null
    log "sessao iniciada (pid $$, display $DISPLAY_NUM, home $HOME_DIR)"

    if ! startup; then
        log "ERRO: a sessao nao subiu; encerramento"
        cleanup
        exit 1
    fi

    while [ "$STOPPING" = "0" ]; do
        tick
        [ "$STOPPING" = "0" ] || break
        check_component openbox || fail_session
        check_component tint2 || fail_session
        check_component pcmanfm || fail_session
    done

    stop_children
    cleanup
    exit 0
}

main
