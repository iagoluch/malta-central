"""Testes de descoberta de portas seriais nos sistemas host suportados."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ["MC_DISABLE_THREADS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as mc  # noqa: E402


class SerialPortDiscoveryTests(unittest.TestCase):
    def port(self, device, description="", hwid="", manufacturer=""):
        return SimpleNamespace(
            device=device, description=description, hwid=hwid, manufacturer=manufacturer
        )

    def test_explicit_port_wins_and_disables_guessing(self):
        ports = [
            self.port("COM3", "USB Serial Device"),
            self.port("COM7", "Bluetooth Serial"),
        ]
        self.assertEqual(
            mc.serial_port_candidates("nt", ports=ports, explicit="COM7"),
            ["COM7"],
        )

    def test_windows_detects_usb_serial_port(self):
        ports = [
            self.port("COM3", "Standard Serial over Bluetooth link"),
            self.port("COM8", "USB Serial Device", "USB VID:PID=22B8:0001"),
        ]
        self.assertEqual(mc.serial_port_candidates("nt", ports=ports, explicit=""), ["COM8"])

    def test_windows_uses_single_com_when_description_is_generic(self):
        ports = [self.port("COM12", "Communications Port")]
        self.assertEqual(mc.serial_port_candidates("nt", ports=ports, explicit=""), ["COM12"])

    def test_windows_does_not_guess_between_unrelated_com_ports(self):
        ports = [
            self.port("COM3", "Standard Serial over Bluetooth link"),
            self.port("COM8", "Communications Port"),
        ]
        self.assertEqual(mc.serial_port_candidates("nt", ports=ports, explicit=""), [])

    def test_windows_com_ports_are_sorted_numerically(self):
        ports = [
            self.port("COM12", "USB serial adapter"),
            self.port("COM3", "USB serial adapter"),
        ]
        self.assertEqual(
            mc.serial_port_candidates("nt", ports=ports, explicit=""),
            ["COM3", "COM12"],
        )

    def test_linux_keeps_existing_fallback_candidates(self):
        ports = [self.port("/dev/ttyUSB0", "USB Serial")]
        result = mc.serial_port_candidates("posix", ports=ports, explicit="")
        self.assertEqual(result[0], "/dev/ttyUSB0")
        for item in mc.PORT_CANDIDATES:
            self.assertIn(item, result)

    def test_environment_can_select_com_port(self):
        with mock.patch.dict(os.environ, {"MC_SERIAL_PORT": "COM11"}):
            self.assertEqual(
                mc.serial_port_candidates("nt", ports=[], explicit=None), ["COM11"]
            )


if __name__ == "__main__":
    unittest.main()
