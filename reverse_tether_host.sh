#!/bin/bash
# ============================================================================
# reverse_tether_host.sh
# Compartilha a internet do PC com o Motorola Moto E7 Power (malta) via USB.
#
# Requisitos no PC:
#   - Interface USB do celular detectada (rndis => enx020000000001)
#   - Interface com internet ativa (wlan0, eth0, etc.)
#   - sudo para ip/iptables/sysctl
#
# O caminho automatizado e persistente e o perfil NetworkManager
# "malta-rndis-fixed" + dispatcher /etc/NetworkManager/dispatcher.d/90-malta-nat.
# Este script e apenas um fallback manual.
#
# Uso:
#   sudo ./reverse_tether_host.sh
#
# Depois, no celular rode o reverse_tether_cell.sh (via terminal remoto ou serial).
# ============================================================================
set -e

# --- Detectar interface USB do celular --------------------------------------
USB_IF=$(ip -br link 2>/dev/null | awk '/usb|enx|enp.*usb/ {print $1; exit}')
if [ -z "$USB_IF" ]; then
    echo "ERRO: Interface USB do celular não detectada."
    echo "Primeiro ative a função USB Ethernet no celular (reverse_tether_cell.sh)"
    echo "e conecte o cabo."
    exit 1
fi

# --- Detectar interface com internet no PC ----------------------------------
NET_IF=$(ip route | grep default | head -1 | awk '{print $5}')
if [ -z "$NET_IF" ]; then
    echo "ERRO: Nenhuma interface com rota default (internet) encontrada no PC."
    exit 1
fi

echo "================== REVERSE TETHERING HOST =================="
echo "Interface USB do celular: $USB_IF"
echo "Interface com internet no PC: $NET_IF"
echo ""

# --- Configurar IP na interface USB (lado do PC) ----------------------------
sudo ip addr add 10.55.0.1/24 dev "$USB_IF" 2>/dev/null || true
sudo ip link set "$USB_IF" up

# --- Ativar IP forwarding ---------------------------------------------------
sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null

# --- NAT: compartilhar a internet -------------------------------------------
sudo iptables -t nat -A POSTROUTING -o "$NET_IF" -j MASQUERADE 2>/dev/null || true
sudo iptables -A FORWARD -i "$USB_IF" -j ACCEPT 2>/dev/null || true
sudo iptables -A FORWARD -o "$USB_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true

echo "================== ATIVO =================="
echo "PC gateway: 10.55.0.1  (via $USB_IF)"
echo "Internet saindo por: $NET_IF"
echo ""
echo "No celular, execute:"
echo "  cat > /data/reverse_tether_cell.sh << 'CELL_EOF'"
echo "  <cole aqui o conteúdo de reverse_tether_cell.sh>"
echo "  CELL_EOF"
echo "  chmod +x /data/reverse_tether_cell.sh"
echo "  /data/reverse_tether_cell.sh"
echo ""
echo "Depois teste no celular:"
echo "  ping -c 3 8.8.8.8"
echo ""
echo "Para desfazer:"
echo "  sudo iptables -t nat -D POSTROUTING -o $NET_IF -j MASQUERADE"
echo "  sudo iptables -D FORWARD -i $USB_IF -j ACCEPT"
echo "  sudo iptables -D FORWARD -o $USB_IF -m state --state RELATED,ESTABLISHED -j ACCEPT"
echo "  sudo ip addr del 10.55.0.1/24 dev $USB_IF"
