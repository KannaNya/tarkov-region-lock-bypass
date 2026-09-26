from __future__ import annotations

import base64
import csv
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

import support  # noqa: F401

from tarkov_cis.catalog import openvpn_tcp_ports, parse_vpngate_csv, rc4, read_native_catalog

HEADER = [
    "#HostName", "IP", "Score", "Ping", "Speed", "CountryLong", "CountryShort", "NumVpnSessions",
    "Uptime", "TotalUsers", "TotalTraffic", "LogType", "Operator", "Message", "OpenVPN_ConfigData_Base64",
]


def encoded(profile: str) -> str:
    return base64.b64encode(profile.encode()).decode()


def csv_text(rows: list[list[str]]) -> str:
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["*vpn_servers"])
    writer.writerow(HEADER)
    writer.writerows(rows)
    writer.writerow(["*"])
    return output.getvalue()


def row(host: str, ip: str, country: str, profile: str) -> list[str]:
    return [host, ip, "100", "20", "50000000", country, country, "2", "1", "1", "1", "", "", "a,b", encoded(profile)]


class HttpsCatalogTests(unittest.TestCase):
    def test_only_cis_relays_with_explicit_tcp_ports(self):
        parsed = parse_vpngate_csv(csv_text([
            row("ru", "192.0.2.10", "RU", "client\nproto tcp-client\nremote 192.0.2.10 443\n"),
            row("ua-udp", "192.0.2.20", "UA", "client\nproto udp\nremote 192.0.2.20 1194\n"),
            row("kz", "192.0.2.30", "KZ", "client\nproto udp\nremote 192.0.2.30 992 tcp\n"),
            row("by-ambiguous", "192.0.2.40", "BY", "client\nremote 192.0.2.40 5555\n"),
            row("jp", "192.0.2.50", "JP", "client\nproto tcp\nremote 192.0.2.50 443\n"),
        ]))
        self.assertEqual([r.endpoint for r in parsed], ["192.0.2.10:443", "192.0.2.30:992"])
        self.assertEqual(parsed[0].speed_mbps, 50.0)
        self.assertEqual(parsed[0].source, "HttpsApi")

    def test_connection_blocks_inherit_or_override_proto(self):
        profile = (
            "client\nproto tcp-client\n<connection>\nremote 192.0.2.1 443\n</connection>\n"
            "<connection>\nproto udp\nremote 192.0.2.1 1194\nremote 192.0.2.1 992 tcp\n</connection>\n"
        )
        self.assertEqual(openvpn_tcp_ports(encoded(profile)), (443, 992))

    def test_missing_header_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_vpngate_csv("garbage\n")


def pack(elements: list[tuple[str, int, list]]) -> bytes:
    out = bytearray(struct.pack(">I", len(elements)))
    for name, kind, values in elements:
        out += struct.pack(">I", len(name) + 1) + name.encode() + struct.pack(">II", kind, len(values))
        for value in values:
            if kind == 0:
                out += struct.pack(">I", value)
            elif kind == 1:
                out += struct.pack(">I", len(value)) + value
            elif kind in (2, 3):
                data = str(value).encode() + (b"\0" if kind == 3 else b"")
                out += struct.pack(">I", len(data)) + data
            else:
                out += struct.pack(">Q", value)
    return bytes(out)


def native_file(stamp: datetime, *, compressed: bool) -> bytes:
    inner = pack([
        ("CountryShort", 2, ["RU", "JP"]),
        ("Fqdn", 2, ["vpn-ru.opengw.net", "vpn-jp.opengw.net"]),
        ("IP", 2, ["192.0.2.10", "192.0.2.20"]),
        ("SslPorts", 2, ["443 992 invalid 70000", "5555"]),
        ("UdpPort", 0, [2061, 0]),
        ("PingToJapan", 0, [88, 12]),
        ("SpeedToJapan", 4, [52_000_000, 100_000_000]),
        ("Score", 4, [900_001, 900_002]),
        ("NumClients", 0, [3, 4]),
    ])
    payload = struct.pack(">I", len(inner)) + zlib.compress(inner) if compressed else inner
    outer = pack([
        ("data", 1, [payload]),
        ("data_size", 0, [len(payload)]),
        ("sign", 1, [bytes(128)]),
        ("compressed", 0, [int(compressed)]),
    ])
    header = bytearray(0xF0)
    text = f"[VPNGate Data File]\r\n{stamp.strftime('%Y%m%d_%H%M%S.%f')[:-3]}\r\n\r\n".encode()
    header[: len(text)] = text
    seed = bytes(range(1, 21))
    return bytes(header) + seed + rc4(outer, hashlib.sha1(seed).digest())


class NativeCatalogTests(unittest.TestCase):
    NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    def _read(self, data: bytes, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VPNGate.dat"
            path.write_bytes(data)
            return read_native_catalog(path, now=self.NOW, **kwargs)

    def test_ssl_and_udp_ports_with_and_without_compression(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                relays = self._read(native_file(self.NOW - timedelta(minutes=5), compressed=compressed), max_age_hours=1)
                self.assertEqual(
                    sorted(r.endpoint for r in relays),
                    ["192.0.2.10:443", "192.0.2.10:992", "udp://192.0.2.10:2061"],
                )
                self.assertTrue(all(r.source == "NativeCatalog" and r.speed_mbps == 52.0 for r in relays))

    def test_stale_catalog_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            self._read(native_file(self.NOW - timedelta(hours=30), compressed=True), max_age_hours=24)

    def test_bad_header_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "header"):
            self._read(b"x" * 400, max_age_hours=24)


if __name__ == "__main__":
    unittest.main()
