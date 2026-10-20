#!/bin/busybox sh
# ============================================================================
# reverse_tether_cell.sh — fallback manual do lado do celular (malta)
#
# O caminho normal é o app malta-central no PC: ele cria/recria o RNDIS,
# configura IP, rota e DNS sozinho e monta /data. Use este script apenas
# quando estiver no shell serial sem o app disponível.
#
# O gadget do malta não tem ECM/NCM; a função viável é rndis.usb0.
# MACs fixos => o PC vê sempre enx020000000001.
# ============================================================================
BB=/bin/busybox
G=/sys/kernel/config/usb_gadget/g1
U=$($BB cat "$G/UDC" 2>/dev/null)
[ -n "$U" ] || U=musb-hdrc

$BB mount -t configfs none /sys/kernel/config 2>/dev/null || true

# Desanexa para poder criar/alterar a função.
echo "" > "$G/UDC"
$BB sleep 1

if [ ! -d "$G/functions/rndis.usb0" ]; then
    $BB mkdir "$G/functions/rndis.usb0"
    echo 02:00:00:00:00:02 > "$G/functions/rndis.usb0/dev_addr"
    echo 02:00:00:00:00:01 > "$G/functions/rndis.usb0/host_addr"
    # ifname é read-only neste kernel; o default do usb_f_rndis já é rndis0.
fi

[ -L "$G/configs/c.1/rndis.usb0" ] || \
    ( cd "$G" && $BB ln -s functions/rndis.usb0 configs/c.1/rndis.usb0 )

$BB sleep 1
echo "$U" > "$G/UDC"
$BB sleep 3

ip addr add 10.55.0.2/24 dev rndis0 2>/dev/null || true
ip link set rndis0 up
ip route add default via 10.55.0.1 2>/dev/null || true
printf "nameserver 1.1.1.1\nnameserver 8.8.8.8\n" > /etc/resolv.conf

echo "RNDIS pronto:"
ip -4 addr show rndis0 | grep inet
ip route
