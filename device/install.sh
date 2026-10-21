#!/bin/sh
# Instalador idempotente do Malta Desktop no aparelho (Alpine armv7, root).
#
# Instala um chroot Alpine persistente em /data/malta-desktop/root e deixa
# prontos o servidor VNC, o proxy socat e a sessão X. O rootfs do aparelho é
# um initramfs em RAM; nada aqui é instalado fora de /data.
#
# Uso (root):
#   sh /data/malta-desktop/install.sh
#   sh /data/malta-desktop/install.sh --remove --yes
#
# A instalação baixa pacotes e pode levar minutos, então o app deve chamá-la
# de forma assíncrona:
#   (nohup sh /data/malta-desktop/install.sh >/dev/null 2>&1 &)
# O progresso fica em /data/malta-desktop/run/install.status:
#   STATE=running|done|error
#   STEP=preflight|mounts|apk|config|session|vnc|verify|done
#   TS=<UTC ISO-8601>
#   ERROR=<mensagem ou vazio>
#
# A remoção exige as duas flags (--remove --yes) e apaga apenas
# /data/malta-desktop, depois de parar o desktop e desmontar proc/dev/sys.
#
# Versão do bundle: a fonte da verdade é /data/malta-desktop/version, que
# guarda o md5 do install.sh instalado (ver bundle_version). O PC lê esse
# arquivo e compara com o md5 do device/install.sh local; divergência (ou o
# formato numérico antigo) significa "precisa reinstalar". BUNDLE_LABEL é
# apenas o rótulo da mensagem de conclusão e não entra no contrato.

set -u

BASE=/data/malta-desktop
CHROOT=$BASE/root
RUN=$BASE/run
LOGS=$BASE/logs
CONFIG=$BASE/config
CACHE=$BASE/apk-cache
KEYS=$CHROOT/etc/apk/keys
LOG=$LOGS/install.log
STATUS=$RUN/install.status
LOCK=$RUN/install.lock
BUNDLE_LABEL=1
MIN_FREE_KB=204800
MIRROR=${MALTA_ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}
PACKAGES="alpine-baselayout busybox openbox tint2 jgmenu pcmanfm xterm font-dejavu hicolor-icon-theme tigervnc socat xset xsetroot setxkbmap xkeyboard-config xkbcomp"
REQUIRED_BINS="/bin/sh /usr/bin/Xvnc /usr/bin/vncpasswd /usr/bin/socat /usr/bin/openbox /usr/bin/tint2 /usr/bin/pcmanfm /usr/bin/xterm /usr/bin/jgmenu_run /usr/bin/xset /usr/bin/xsetroot /usr/bin/setxkbmap /usr/bin/xkbcomp"

STEP=start
BRANCH=

usage() {
    printf 'Uso: %s [--remove --yes]\n' "$0"
    printf '  sem argumentos     instala ou atualiza o chroot em %s\n' "$BASE"
    printf '  --remove --yes     para o desktop e remove %s por completo\n' "$BASE"
}

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG" 2>/dev/null
}

say() {
    printf '%s\n' "$*"
    log "$*"
}

# Versão do bundle: o md5 do install.sh instalado.
#
# A fonte da verdade é o próprio arquivo instalado: o PC calcula o md5 do
# device/install.sh local e compara com o valor de /data/malta-desktop/version.
# Divergência (ou o formato numérico antigo) faz o PC reinstalar o bundle.
bundle_version() {
    bv_file=${1:-}
    [ -n "$bv_file" ] || return 1
    [ -f "$bv_file" ] || return 1
    bv_sum=$(md5sum "$bv_file" 2>/dev/null | cut -d' ' -f1)
    [ -n "$bv_sum" ] || return 1
    printf '%s\n' "$bv_sum"
}

data_mounted() {
    grep -qs ' /data ' /proc/mounts
}

set_status() {
    state=$1
    step=$2
    err=${3:-}
    tmp=$STATUS.tmp
    {
        printf 'STATE=%s\n' "$state"
        printf 'STEP=%s\n' "$step"
        printf 'TS=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'ERROR=%s\n' "$err"
    } >"$tmp" 2>/dev/null || return 0
    mv "$tmp" "$STATUS" 2>/dev/null || return 0
}

fail() {
    set_status error "$STEP" "$1"
    log "ERRO [$STEP]: $1"
    printf 'STATE=error\nSTEP=%s\nERROR=%s\n' "$STEP" "$1"
    exit 1
}

release_lock() {
    pid=$(cat "$LOCK/pid" 2>/dev/null)
    if [ "$pid" = "$$" ]; then
        rm -rf "$LOCK"
    fi
}

acquire_lock() {
    i=0
    while [ "$i" -lt 5 ]; do
        if mkdir "$LOCK" 2>/dev/null; then
            printf '%s\n' "$$" >"$LOCK/pid"
            return 0
        fi
        pid=$(cat "$LOCK/pid" 2>/dev/null)
        case "$pid" in
            ''|*[!0-9]*) pid=0 ;;
        esac
        if [ "$pid" -gt 0 ] && kill -0 "$pid" 2>/dev/null; then
            return 1
        fi
        rm -rf "$LOCK"
        i=$((i + 1))
    done
    return 1
}

mount_pseudo() {
    src=$1
    dst=$2
    if grep -qs " $dst " /proc/mounts; then
        return 0
    fi
    mkdir -p "$dst"
    mount -o bind "$src" "$dst" >>"$LOG" 2>&1
}

umount_if_mounted() {
    if grep -qs " $1 " /proc/mounts; then
        umount "$1" >>"$LOG" 2>&1 || return 1
    fi
    return 0
}

run_apk() {
    apk_noscripts=$1
    apk_initdb=
    [ -f "$CHROOT/etc/apk/world" ] || apk_initdb=--initdb
    if [ "$apk_noscripts" = "1" ]; then
        apk --root "$CHROOT" --arch armv7 --keys-dir "$KEYS" --cache-dir "$CACHE" \
            add --no-progress $apk_initdb --no-scripts $PACKAGES >>"$LOG" 2>&1
    else
        apk --root "$CHROOT" --arch armv7 --keys-dir "$KEYS" --cache-dir "$CACHE" \
            add --no-progress $apk_initdb $PACKAGES >>"$LOG" 2>&1
    fi
}

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

# Gera /etc/xdg/openbox/menu.xml a partir dos .desktop instalados. O item
# fixo chama o jgmenu; os demais vêm de Name= e Exec= (sem códigos de campo).
#
# O menu de aplicativos do desktop e o jgmenu (botao do painel e Alt+F2). Este
# arquivo e o menu do openbox: ele e aberto por Alt+F1 (keybind A-F1 no
# rc.xml) e pelo botao do openbox na barra de titulo das janelas — o clique
# direito no desktop nao chega ao openbox porque o pcmanfm ocupa a root
# window.
write_openbox_menu() {
    menu=$CHROOT/etc/xdg/openbox/menu.xml
    {
        printf '<?xml version="1.0" encoding="UTF-8"?>\n'
        printf '<openbox_menu xmlns="http://openbox.org/3.4/menu">\n'
        printf '  <menu id="root-menu" label="Malta">\n'
        printf '    <item label="Aplicativos (jgmenu)">\n'
        printf '      <action name="Execute"><command>jgmenu_run</command></action>\n'
        printf '    </item>\n'
        printf '    <separator />\n'
    } >"$menu"
    for file in "$CHROOT"/usr/share/applications/*.desktop; do
        [ -f "$file" ] || continue
        name=$(sed -n 's/^Name=//p' "$file" | head -n 1)
        exec_line=$(sed -n 's/^Exec=//p' "$file" | head -n 1)
        terminal=$(sed -n 's/^Terminal=//p' "$file" | head -n 1)
        nodisplay=$(sed -n 's/^NoDisplay=//p' "$file" | head -n 1)
        hidden=$(sed -n 's/^Hidden=//p' "$file" | head -n 1)
        [ -n "$name" ] || continue
        [ -n "$exec_line" ] || continue
        [ "$nodisplay" = "true" ] && continue
        [ "$hidden" = "true" ] && continue
        exec_line=$(printf '%s\n' "$exec_line" | sed -e 's/%[fFuUdDnNickvm]//g' -e 's/%%/%/g')
        if [ "$terminal" = "true" ]; then
            exec_line="xterm -e $exec_line"
        fi
        printf '    <item label="%s"><action name="Execute"><command>%s</command></action></item>\n' \
            "$(xml_escape "$name")" "$(xml_escape "$exec_line")" >>"$menu"
    done
    printf '  </menu>\n' >>"$menu"
    printf '</openbox_menu>\n' >>"$menu"
}

remove_base() {
    say "parando o desktop antes da remoção"
    if [ -f "$BASE/malta-desktop.sh" ]; then
        sh "$BASE/malta-desktop.sh" stop >>"$LOG" 2>&1 || true
    fi

    umount_if_mounted "$CHROOT/sys" || fail "não foi possível desmontar $CHROOT/sys"
    # As submontagens de /dev saem antes do /dev: desmontar o pai primeiro
    # deixaria as filhas penduradas.
    umount_if_mounted "$CHROOT/dev/pts" || fail "não foi possível desmontar $CHROOT/dev/pts"
    umount_if_mounted "$CHROOT/dev/shm" || fail "não foi possível desmontar $CHROOT/dev/shm"
    umount_if_mounted "$CHROOT/dev" || fail "não foi possível desmontar $CHROOT/dev"
    umount_if_mounted "$CHROOT/proc" || fail "não foi possível desmontar $CHROOT/proc"
    if grep -qs " $CHROOT/proc " /proc/mounts; then
        fail "chroot ainda tem /proc montado; pare o desktop e tente de novo"
    fi

    rm -rf "$BASE" || fail "não foi possível remover $BASE"
    say "remoção concluída: $BASE foi apagado"
}

REMOVE=0
CONFIRM=0
for arg in "$@"; do
    case "$arg" in
        --remove) REMOVE=1 ;;
        --yes) CONFIRM=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "argumento desconhecido: $arg" ;;
    esac
done

if [ "$CONFIRM" = "1" ] && [ "$REMOVE" = "0" ]; then
    usage >&2
    die "--yes só faz sentido junto de --remove"
fi

[ "$(id -u)" = "0" ] || die "execute como root"

if [ "$REMOVE" = "1" ]; then
    if [ "$CONFIRM" != "1" ]; then
        usage >&2
        die "remoção exige --remove --yes"
    fi
    case "$BASE" in
        /data/malta-desktop) ;;
        *) die "recuso remover caminho inesperado: $BASE" ;;
    esac
    if ! data_mounted; then
        say "nada a remover: /data não está montado"
        exit 0
    fi
    if [ ! -d "$BASE" ]; then
        say "nada a remover: $BASE não existe"
        exit 0
    fi
    mkdir -p "$RUN" "$LOGS" || die "não foi possível criar $RUN"
    trap 'release_lock' 0
    if ! acquire_lock; then
        die "operação em andamento (lock $LOCK)"
    fi
    say "removendo o Malta Desktop de $BASE"
    remove_base
    exit 0
fi

data_mounted || die "/data não está montado; use a ação Montar /data no app antes de instalar"

mkdir -p "$BASE" "$RUN" "$LOGS" "$CONFIG" "$CACHE" "$CHROOT" || die "não foi possível criar $BASE"
trap 'release_lock' 0
if ! acquire_lock; then
    die "instalação já em andamento (lock $LOCK)"
fi

STEP=preflight
set_status running "$STEP"
log "instalação iniciada em $BASE"

command -v apk >/dev/null 2>&1 || fail "apk não encontrado no host"
command -v chroot >/dev/null 2>&1 || fail "chroot não encontrado no host"
[ -r /etc/alpine-release ] || fail "/etc/alpine-release ausente: não é possível derivar a branch do Alpine"

BRANCH=$(cut -d. -f1,2 /etc/alpine-release)
case "$BRANCH" in
    [0-9]*.[0-9]*) ;;
    *) fail "branch Alpine inválida em /etc/alpine-release: $BRANCH" ;;
esac

case "$MIRROR" in
    */) MIRROR=${MIRROR%/} ;;
esac

if ! command -v ip >/dev/null 2>&1 || ! ip route 2>/dev/null | grep -q '^default'; then
    say "aviso: sem rota padrão; o apk pode falhar. Confira o RNDIS (10.55.0.2) e a internet pelo cabo."
fi

FREE_KB=$(df -Pk /data 2>/dev/null | awk 'NR==2 {print $4}')
case "$FREE_KB" in
    ''|*[!0-9]*) FREE_KB=0 ;;
esac
if [ "$FREE_KB" -lt "$MIN_FREE_KB" ]; then
    fail "espaço livre insuficiente em /data: $((FREE_KB / 1024)) MB (mínimo $((MIN_FREE_KB / 1024)) MB)"
fi

[ -d /etc/apk/keys ] || fail "chaves do apk ausentes em /etc/apk/keys"

STEP=mounts
set_status running "$STEP"
mkdir -p "$CHROOT/proc" "$CHROOT/dev" "$CHROOT/sys" "$CHROOT/tmp" \
    "$CHROOT/root/.vnc" "$CHROOT/usr/local/bin" \
    "$CHROOT/etc/apk/keys" "$CHROOT/etc/xdg/openbox" "$CHROOT/etc/xdg/tint2" \
    "$CHROOT/etc/xdg/pcmanfm/default" "$CHROOT/etc/xdg/jgmenu"
chmod 1777 "$CHROOT/tmp"
chmod 700 "$CHROOT/root/.vnc"

mount_pseudo /proc "$CHROOT/proc" || fail "não foi possível montar /proc em $CHROOT/proc"
mount_pseudo /dev "$CHROOT/dev" || fail "não foi possível montar /dev em $CHROOT/dev"
mount_pseudo /sys "$CHROOT/sys" || fail "não foi possível montar /sys em $CHROOT/sys"
# /dev/pts e /dev/shm são montagens próprias: o bind de /dev não as traz
# junto. Sem /dev/pts o xterm do chroot falha com "not enough ptys".
mount_pseudo /dev/pts "$CHROOT/dev/pts" || fail "não foi possível montar /dev/pts em $CHROOT/dev/pts"
mount_pseudo /dev/shm "$CHROOT/dev/shm" || fail "não foi possível montar /dev/shm em $CHROOT/dev/shm"

if [ -r /etc/resolv.conf ]; then
    cp /etc/resolv.conf "$CHROOT/etc/resolv.conf" 2>/dev/null || say "aviso: não foi possível copiar /etc/resolv.conf"
fi

cp /etc/apk/keys/* "$KEYS"/ 2>/dev/null || true
if ! ls "$KEYS"/*.rsa.pub >/dev/null 2>&1; then
    fail "não há chaves apk em $KEYS"
fi

printf '%s/v%s/main\n%s/v%s/community\n' "$MIRROR" "$BRANCH" "$MIRROR" "$BRANCH" >"$CHROOT/etc/apk/repositories" \
    || fail "não foi possível escrever $CHROOT/etc/apk/repositories"
printf 'armv7\n' >"$CHROOT/etc/apk/arch" || fail "não foi possível escrever $CHROOT/etc/apk/arch"

STEP=apk
set_status running "$STEP"
say "instalando pacotes Alpine v$BRANCH (armv7) em $CHROOT; isso pode levar alguns minutos"
if ! run_apk 0; then
    log "apk falhou; repetindo uma vez com --no-scripts"
    say "aviso: apk falhou; tentando novamente com --no-scripts"
    if ! run_apk 1; then
        fail "apk falhou: verifique RNDIS/internet (10.55.0.2), DNS e espaço em /data"
    fi
fi

STEP=config
set_status running "$STEP"
for name in openbox-rc.xml tint2rc pcmanfm.conf jgmenurc; do
    [ -f "$CONFIG/$name" ] || fail "config ausente: $CONFIG/$name (reenvie pelo app)"
done
cp "$CONFIG/openbox-rc.xml" "$CHROOT/etc/xdg/openbox/rc.xml" || fail "não foi possível copiar openbox-rc.xml"
cp "$CONFIG/tint2rc" "$CHROOT/etc/xdg/tint2/tint2rc" || fail "não foi possível copiar tint2rc"
cp "$CONFIG/pcmanfm.conf" "$CHROOT/etc/xdg/pcmanfm/default/pcmanfm.conf" || fail "não foi possível copiar pcmanfm.conf"
cp "$CONFIG/jgmenurc" "$CHROOT/etc/xdg/jgmenu/jgmenurc" || fail "não foi possível copiar jgmenurc"
cp "$CONFIG/jgmenu-apps.csv" "$CHROOT/etc/xdg/jgmenu/jgmenu-apps.csv" ||
    fail "não foi possível copiar jgmenu-apps.csv"

# Icones e launchers proprios. Os .desktop dos pacotes apontam para icones que
# nao vem no hicolor-icon-theme (ex.: system-file-manager), e tanto o painel do
# tint2 quanto o menu do openbox (showIcons=yes) mostrariam um quadrado branco
# no lugar do icone. Os tres launchers abaixo sobrescrevem os dos pacotes com
# os icones do projeto: tres XPM (~8 KB no total) em vez do adwaita-icon-theme
# (~13 MB).
mkdir -p "$CHROOT/usr/share/icons/hicolor/48x48/apps" || fail "não foi possível criar o diretório de ícones"
for icon in malta-files malta-terminal malta-menu; do
    [ -f "$CONFIG/icons/$icon.xpm" ] ||
        fail "ícone ausente: $CONFIG/icons/$icon.xpm (reenvie pelo app)"
    cp "$CONFIG/icons/$icon.xpm" "$CHROOT/usr/share/icons/hicolor/48x48/apps/$icon.xpm" ||
        fail "não foi possível copiar o ícone $icon.xpm"
    chmod 644 "$CHROOT/usr/share/icons/hicolor/48x48/apps/$icon.xpm"
done
for launcher in pcmanfm xterm jgmenu; do
    [ -f "$CONFIG/applications/$launcher.desktop" ] ||
        fail "launcher ausente: $CONFIG/applications/$launcher.desktop (reenvie pelo app)"
    cp "$CONFIG/applications/$launcher.desktop" "$CHROOT/usr/share/applications/$launcher.desktop" ||
        fail "não foi possível copiar o launcher $launcher.desktop"
    chmod 644 "$CHROOT/usr/share/applications/$launcher.desktop"
done
write_openbox_menu

# Fonte do terminal: o xterm pede a fonte "misc-fixed", que nao vem no chroot
# (ele abriria com "cannot load font"). O DejaVu ja esta instalado e serve,
# sem trazer outro pacote.
{
    printf 'XTerm*faceName: DejaVu Sans Mono\n'
    printf 'XTerm*faceSize: 9\n'
} >"$CHROOT/root/.Xdefaults" || fail "não foi possível escrever $CHROOT/root/.Xdefaults"
chmod 600 "$CHROOT/root/.Xdefaults"

STEP=session
set_status running "$STEP"
[ -f "$BASE/session.sh" ] || fail "sessão ausente: $BASE/session.sh (reenvie pelo app)"
cp "$BASE/session.sh" "$CHROOT/usr/local/bin/malta-session" || fail "não foi possível instalar session.sh"
chmod 755 "$CHROOT/usr/local/bin/malta-session"
[ -f "$BASE/install.sh" ] && chmod 755 "$BASE/install.sh"
[ -f "$BASE/malta-desktop.sh" ] && chmod 755 "$BASE/malta-desktop.sh"

STEP=vnc
set_status running "$STEP"
[ -f "$BASE/vncpasswd" ] || fail "senha VNC ausente: reenvie pelo app"
VNC_SIZE=$(wc -c <"$BASE/vncpasswd" 2>/dev/null | tr -d ' ')
[ "$VNC_SIZE" = "8" ] || fail "senha VNC inválida: ${VNC_SIZE:-0} bytes (esperado 8); reenvie pelo app"
cp "$BASE/vncpasswd" "$CHROOT/root/.vnc/passwd" || fail "não foi possível copiar vncpasswd para o chroot"
chmod 600 "$BASE/vncpasswd" "$CHROOT/root/.vnc/passwd"

if [ -x "$CHROOT/usr/bin/fc-cache" ]; then
    HOME=/root chroot "$CHROOT" /usr/bin/fc-cache -f >>"$LOG" 2>&1 || say "aviso: fc-cache falhou (fontes podem ficar sem cache)"
else
    say "aviso: fc-cache ausente no chroot"
fi

STEP=verify
set_status running "$STEP"
MISSING=
for bin in $REQUIRED_BINS; do
    [ -x "$CHROOT$bin" ] || MISSING="$MISSING $bin"
done
[ -n "$MISSING" ] && fail "binários ausentes no chroot:$MISSING"

for dest in /usr/bin/Xvnc /usr/bin/socat /usr/local/bin/malta-session /etc/xdg/openbox/rc.xml \
    /etc/xdg/openbox/menu.xml /etc/xdg/tint2/tint2rc /etc/xdg/pcmanfm/default/pcmanfm.conf \
    /etc/xdg/jgmenu/jgmenurc /etc/xdg/jgmenu/jgmenu-apps.csv \
    /usr/share/icons/hicolor/48x48/apps/malta-files.xpm \
    /usr/share/icons/hicolor/48x48/apps/malta-terminal.xpm \
    /usr/share/icons/hicolor/48x48/apps/malta-menu.xpm \
    /usr/share/applications/pcmanfm.desktop \
    /usr/share/applications/xterm.desktop \
    /usr/share/applications/jgmenu.desktop /root/.Xdefaults; do
    [ -s "$CHROOT$dest" ] || fail "arquivo ausente ou vazio no chroot: $dest"
done

[ -f "$CHROOT/root/.vnc/passwd" ] || fail "senha VNC ausente no chroot: reenvie pelo app"
VNC_SIZE=$(wc -c <"$CHROOT/root/.vnc/passwd" 2>/dev/null | tr -d ' ')
[ "$VNC_SIZE" = "8" ] || fail "senha VNC inválida no chroot: ${VNC_SIZE:-0} bytes; reenvie pelo app"

# A versão do bundle é o md5 do install.sh instalado: é o que o PC compara
# com o device/install.sh local para saber se precisa reinstalar.
BUNDLE_MD5=$(bundle_version "$BASE/install.sh") ||
    fail "não foi possível calcular o md5 de $BASE/install.sh"
printf '%s\n' "$BUNDLE_MD5" >"$BASE/version" || fail "não foi possível escrever $BASE/version"

STEP=done
set_status done "$STEP"
say "instalação concluída (Alpine v$BRANCH, bundle $BUNDLE_LABEL)"
printf 'STATE=done\nSTEP=done\nVERSION=%s\n' "$BUNDLE_MD5"
