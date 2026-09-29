"""VPN Gate relay catalogs: the official HTTPS CSV and SoftEther's VPNGate.dat."""

from __future__ import annotations

import base64
import csv
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
import ipaddress
from pathlib import Path
import re
import secrets
import shlex
import struct
from urllib.request import Request, urlopen
import zlib

from .models import COUNTRY_PRIORITY, Relay

VPNGATE_API = "https://www.vpngate.net/api/iphone/"
# The endpoint the SoftEther VPN Gate plug-in itself downloads VPNGate.dat
# from; %c is a random letter used for load balancing.
VPNGATE_NATIVE_API = "http://x{0}.x{0}.client.api.vpngate2.jp/api/"
MAX_CATALOG_BYTES = 32 * 1024 * 1024
CIS = frozenset(COUNTRY_PRIORITY)


# --- Official HTTPS API -----------------------------------------------------


def download_https_catalog(timeout: float) -> tuple[Relay, ...]:
    request = Request(
        f"{VPNGATE_API}?t={secrets.token_hex(8)}",
        headers={"User-Agent": "Tarkov-CIS/0.3", "Cache-Control": "no-cache"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_CATALOG_BYTES + 1)
    if len(payload) > MAX_CATALOG_BYTES:
        raise ValueError("VPN Gate catalog exceeded the 32 MiB safety limit")
    return parse_vpngate_csv(payload.decode("utf-8-sig"))


def parse_vpngate_csv(content: str) -> tuple[Relay, ...]:
    """CIS relays whose OpenVPN profile explicitly declares a TCP port.

    SoftEther accepts TCP on the same SSL ports; OpenVPN UDP ports are not
    SoftEther ports and are skipped.
    """

    rows = list(csv.reader(StringIO(content.lstrip("﻿"))))
    header_at = next((i for i, row in enumerate(rows) if row and row[0] == "#HostName"), None)
    if header_at is None:
        raise ValueError("VPN Gate CSV header was not found")
    header = [rows[header_at][0].lstrip("#"), *rows[header_at][1:]]
    relays: list[Relay] = []
    for values in rows[header_at + 1 :]:
        if not values or values[0].startswith("*"):
            continue
        row = dict(zip(header, values))
        country = row.get("CountryShort", "").strip().upper()
        if country not in CIS:
            continue
        try:
            ipaddress.IPv4Address(row.get("IP", "").strip())
            ports = openvpn_tcp_ports(row.get("OpenVPN_ConfigData_Base64", ""))
        except ValueError:
            continue
        for port in ports:
            relays.append(
                Relay(
                    ip=row["IP"],
                    port=port,
                    country=country,
                    host_name=row.get("HostName", "").strip(),
                    speed_mbps=round(_int(row.get("Speed")) / 1_000_000, 1),
                    ping=_int(row.get("Ping"), 9999),
                    score=_int(row.get("Score")),
                    sessions=_int(row.get("NumVpnSessions")),
                    source="HttpsApi",
                )
            )
    return tuple(relays)


def openvpn_tcp_ports(encoded: str) -> tuple[int, ...]:
    """Ports of ``remote`` lines whose effective proto is TCP."""

    try:
        text = base64.b64decode("".join(encoded.split()), validate=True).decode("utf-8-sig")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("OpenVPN profile is not valid Base64 UTF-8") from exc
    ports: list[int] = []
    global_proto: str | None = None
    block: list[list[str]] | None = None
    top: list[list[str]] = []
    blocks: list[list[list[str]]] = []
    for line in text.splitlines():
        marker = line.strip().lower()
        if marker == "<connection>":
            block = []
            continue
        if marker == "</connection>":
            if block is not None:
                blocks.append(block)
            block = None
            continue
        tokens = _tokens(line)
        if tokens:
            (top if block is None else block).append(tokens)
    for tokens in top:
        if tokens[0].lower() == "proto" and len(tokens) > 1:
            global_proto = tokens[1].lower()
    for directives, inherited in [(top, global_proto), *[(b, global_proto) for b in blocks]]:
        protos = [t[1].lower() for t in directives if t[0].lower() == "proto" and len(t) > 1]
        context = protos[-1] if protos else inherited
        for tokens in directives:
            if tokens[0].lower() != "remote" or len(tokens) < 3 or not tokens[2].isdigit():
                continue
            port = int(tokens[2])
            proto = tokens[3].lower() if len(tokens) > 3 else context
            if 1 <= port <= 65535 and proto in ("tcp", "tcp-client"):
                ports.append(port)
    return tuple(dict.fromkeys(ports))


def _tokens(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped or stripped[0] in "#;":
        return []
    try:
        return shlex.split(re.split(r"\s+[;#]", stripped, maxsplit=1)[0])
    except ValueError:
        return []


def _int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return default


# --- SoftEther VPN Gate native list (VPNGate.dat) ---------------------------
#
# The plug-in's list is far larger than the HTTPS CSV and includes UDP (NAT-T)
# relays.  It is an RC4-obfuscated SoftEther PACK with a zlib-compressed inner
# PACK.  Only the presence of the 128-byte signature is checked; SoftEther's
# public-key verification is not reproduced here.  The relays themselves are
# untrusted volunteers either way; game traffic inside the tunnel stays TLS.


def download_native_catalog(timeout: float, cache: Path) -> bytes:
    """Fetch VPNGate.dat like the plug-in does and keep a copy in *cache*."""

    request = Request(
        VPNGATE_NATIVE_API.format(secrets.choice("abcdefghijklmnopqrstuvwxyz")),
        headers={"User-Agent": "Tarkov-CIS/0.3"},
    )
    with urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_CATALOG_BYTES + 1)
    _check_native_envelope(data)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(cache)
    return data


def read_native_catalog(path: Path, *, max_age_hours: int, now: datetime | None = None) -> tuple[Relay, ...]:
    data = path.read_bytes()
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return parse_native_catalog(data, max_age_hours=max_age_hours, now=now, fallback_time=modified)


def _check_native_envelope(data: bytes) -> None:
    if not 0x104 < len(data) <= 16 * 1024 * 1024:
        raise ValueError("VPN Gate native catalog size is invalid")
    if not data[:32].decode("ascii", errors="ignore").startswith("[VPNGate Data File]"):
        raise ValueError("VPN Gate native catalog header is invalid")


def parse_native_catalog(
    data: bytes, *, max_age_hours: int, now: datetime | None = None, fallback_time: datetime | None = None
) -> tuple[Relay, ...]:
    _check_native_envelope(data)
    age = (now or datetime.now(timezone.utc)) - _native_timestamp(data, fallback_time)
    if age < timedelta(hours=-1):
        raise ValueError("VPN Gate native catalog timestamp is in the future")
    if max_age_hours > 0 and age > timedelta(hours=max_age_hours):
        raise ValueError(f"VPN Gate native catalog is stale ({age.total_seconds() / 3600:.1f} h)")

    outer = _read_pack(rc4(data[0x104:], hashlib.sha1(data[0xF0:0x104]).digest()), 64, 16, MAX_CATALOG_BYTES // 2)
    payload = _value(outer, "data")
    signature = _value(outer, "sign")
    if not isinstance(payload, bytes) or len(payload) < 4:
        raise ValueError("VPN Gate native catalog contains no server payload")
    if not isinstance(signature, bytes) or len(signature) != 128:
        raise ValueError("VPN Gate native catalog signature marker is invalid")
    declared = int(_value(outer, "data_size", default=0))
    if declared and declared != len(payload):
        raise ValueError("VPN Gate native catalog payload size mismatch")
    if int(_value(outer, "compressed", default=0)):
        payload = _inflate(payload)

    inner = _read_pack(payload, 128, 4096, 4 * 1024 * 1024)
    missing = {"CountryShort", "IP", "SslPorts"} - inner.keys()
    if missing:
        raise ValueError(f"VPN Gate native catalog is missing {', '.join(sorted(missing))}")
    relays: list[Relay] = []
    for i in range(len(inner["CountryShort"])):
        country = str(_value(inner, "CountryShort", i, "")).strip().upper()
        ip = str(_value(inner, "IP", i, "")).strip()
        if country not in CIS:
            continue
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        common = dict(
            ip=ip,
            country=country,
            host_name=str(_value(inner, "Fqdn", i, "")).strip(),
            speed_mbps=round(_int(_value(inner, "SpeedToJapan", i, 0)) / 1_000_000, 1),
            ping=min(_int(_value(inner, "PingToJapan", i, 9999), 9999), 9999),
            score=_int(_value(inner, "Score", i, 0)),
            sessions=_int(_value(inner, "NumClients", i, 0)),
            source="NativeCatalog",
        )
        udp_port = _int(_value(inner, "UdpPort", i, 0))
        if 1 <= udp_port <= 65535:
            relays.append(Relay(port=udp_port, transport="udp", **common))
        ssl_ports = str(_value(inner, "SslPorts", i, ""))
        for port in sorted({int(m) for m in re.findall(r"(?<!\d)\d{1,5}(?!\d)", ssl_ports)}):
            if 1 <= port <= 65535:
                relays.append(Relay(port=port, **common))
    return tuple(relays)


def rc4(data: bytes, key: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    out = bytearray(len(data))
    x = j = 0
    for position, value in enumerate(data):
        x = (x + 1) & 0xFF
        j = (j + state[x]) & 0xFF
        state[x], state[j] = state[j], state[x]
        out[position] = value ^ state[(state[x] + state[j]) & 0xFF]
    return bytes(out)


def _native_timestamp(data: bytes, fallback: datetime | None) -> datetime:
    match = re.search(r"(?m)^(\d{8}_\d{6}\.\d{3})\r?$", data[:240].decode("ascii", errors="ignore"))
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S.%f").replace(tzinfo=timezone.utc)
    if fallback is None:
        raise ValueError("VPN Gate native catalog has no timestamp")
    return fallback


def _inflate(payload: bytes) -> bytes:
    if len(payload) < 11:
        raise ValueError("compressed VPN Gate payload is too short")
    expected = struct.unpack(">I", payload[:4])[0]
    if not 1 <= expected <= MAX_CATALOG_BYTES:
        raise ValueError("compressed VPN Gate payload length is invalid")
    try:
        # A 4-byte length, then a zlib stream; skip its 2-byte header and
        # 4-byte checksum and inflate the raw DEFLATE body like SoftEther does.
        expanded = zlib.decompress(payload[6:-4], wbits=-zlib.MAX_WBITS)
    except zlib.error as exc:
        raise ValueError("compressed VPN Gate payload is not valid DEFLATE") from exc
    if len(expanded) != expected:
        raise ValueError("compressed VPN Gate payload length mismatch")
    return expanded


def _read_pack(payload: bytes, max_elements: int, max_values: int, max_bytes: int) -> dict[str, tuple]:
    """Decode a SoftEther PACK: big-endian element list of typed value arrays."""

    position = 0

    def take(count: int) -> bytes:
        nonlocal position
        if count < 0 or position + count > len(payload):
            raise ValueError("SoftEther PACK is truncated")
        chunk = payload[position : position + count]
        position += count
        return chunk

    def u32() -> int:
        return struct.unpack(">I", take(4))[0]

    def blob() -> bytes:
        length = u32()
        if length > max_bytes:
            raise ValueError("SoftEther PACK value is too large")
        return take(length)

    count = u32()
    if not 1 <= count <= max_elements:
        raise ValueError("SoftEther PACK element count is invalid")
    result: dict[str, tuple] = {}
    for _ in range(count):
        name_length = u32()
        if not 2 <= name_length <= 64:
            raise ValueError("SoftEther PACK element name is invalid")
        name = take(name_length - 1).decode("ascii", errors="strict")
        kind, value_count = u32(), u32()
        if name in result or kind > 4 or not 1 <= value_count <= max_values:
            raise ValueError(f"SoftEther PACK element {name!r} is invalid")
        values: list[object] = []
        for _ in range(value_count):
            if kind == 0:
                values.append(u32())
            elif kind == 1:
                values.append(blob())
            elif kind in (2, 3):
                text = blob().decode("utf-8")
                values.append(text.rstrip("\0") if kind == 3 else text)
            else:
                values.append(struct.unpack(">Q", take(8))[0])
        result[name] = tuple(values)
    return result


def _value(pack: dict[str, tuple], name: str, index: int = 0, default: object = None) -> object:
    values = pack.get(name)
    if values is None or not 0 <= index < len(values):
        return default
    return values[index]
