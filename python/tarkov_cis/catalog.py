"""Parse the official VPN Gate HTTPS CSV without trusting stale IP lists."""

from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
from ipaddress import IPv4Address, ip_address
from pathlib import Path
import re
import shlex
import struct
import time
from typing import Iterable
import zlib

from .models import COUNTRY_PRIORITIES, Relay


CIS_COUNTRY_PRIORITY = COUNTRY_PRIORITIES


@dataclass(frozen=True, slots=True)
class NativeCatalog:
    """Decoded local plugin catalog.

    ``signature_marker_present`` means only that the expected 128-byte field
    exists.  It deliberately does not claim cryptographic signature
    verification because SoftEther's public-key verification is not reproduced
    by this compatibility reader.
    """

    path: Path
    timestamp_utc: datetime
    age_hours: float
    signature_marker_present: bool
    compressed: bool
    source_row_count: int
    relays: tuple[Relay, ...]


def rc4_transform(data: bytes, key: bytes) -> bytes:
    """SoftEther-compatible RC4 transform used by the local catalog envelope."""

    if not key:
        raise ValueError("RC4 key cannot be empty")
    state = list(range(256))
    j = 0
    for index in range(256):
        j = (j + state[index] + key[index % len(key)]) & 0xFF
        state[index], state[j] = state[j], state[index]
    output = bytearray(len(data))
    x = 0
    j = 0
    for position, value in enumerate(data):
        x = (x + 1) & 0xFF
        j = (j + state[x]) & 0xFF
        state[x], state[j] = state[j], state[x]
        output[position] = value ^ state[(state[x] + state[j]) & 0xFF]
    return bytes(output)


class _PackReader:
    def __init__(
        self,
        payload: bytes,
        *,
        maximum_elements: int,
        maximum_values: int,
        maximum_value_bytes: int,
    ) -> None:
        self._payload = payload
        self._maximum_elements = maximum_elements
        self._maximum_values = maximum_values
        self._maximum_value_bytes = maximum_value_bytes
        self._position = 0

    def _take(self, count: int, context: str) -> bytes:
        if count < 0 or self._position + count > len(self._payload):
            raise ValueError(f"SoftEther PACK ended while reading {context}")
        start = self._position
        self._position += count
        return self._payload[start : start + count]

    def _u32(self, context: str) -> int:
        return struct.unpack(">I", self._take(4, context))[0]

    def _u64(self, context: str) -> int:
        return struct.unpack(">Q", self._take(8, context))[0]

    def _string(self, context: str, *, trim_terminator: bool) -> str:
        length = self._u32(f"{context} length")
        if length > self._maximum_value_bytes:
            raise ValueError(f"SoftEther PACK {context} is too large")
        try:
            value = self._take(length, context).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"SoftEther PACK {context} is not UTF-8") from exc
        return value.rstrip("\0") if trim_terminator else value

    def read(self) -> dict[str, tuple[int, tuple[object, ...]]]:
        element_count = self._u32("element count")
        if not 1 <= element_count <= self._maximum_elements:
            raise ValueError("SoftEther PACK element count is invalid")
        result: dict[str, tuple[int, tuple[object, ...]]] = {}
        for _ in range(element_count):
            encoded_name_length = self._u32("element name length")
            if not 2 <= encoded_name_length <= 64:
                raise ValueError("SoftEther PACK element name length is invalid")
            try:
                name = self._take(encoded_name_length - 1, "element name").decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("SoftEther PACK element name is not ASCII") from exc
            if not name or name in result:
                raise ValueError("SoftEther PACK contains an invalid or duplicate element name")
            element_type = self._u32(f"{name} type")
            value_count = self._u32(f"{name} value count")
            if element_type > 4:
                raise ValueError("SoftEther PACK element has an unsupported type")
            if not 1 <= value_count <= self._maximum_values:
                raise ValueError("SoftEther PACK element has an invalid value count")
            values: list[object] = []
            for _ in range(value_count):
                if element_type == 0:
                    values.append(self._u32(name))
                elif element_type == 1:
                    data_length = self._u32(f"{name} data length")
                    if data_length > self._maximum_value_bytes:
                        raise ValueError("SoftEther PACK data element is too large")
                    values.append(self._take(data_length, name))
                elif element_type == 2:
                    values.append(self._string(name, trim_terminator=False))
                elif element_type == 3:
                    values.append(self._string(name, trim_terminator=True))
                else:
                    values.append(self._u64(name))
            result[name] = (element_type, tuple(values))
        return result


def _pack_value(
    pack: dict[str, tuple[int, tuple[object, ...]]],
    name: str,
    index: int = 0,
    default: object = None,
) -> object:
    element = pack.get(name)
    if element is None or index < 0 or index >= len(element[1]):
        return default
    return element[1][index]


def _expand_native_payload(payload: bytes) -> bytes:
    if len(payload) < 11:
        raise ValueError("compressed VPN Gate payload is too short")
    expected_length = struct.unpack(">I", payload[:4])[0]
    if not 1 <= expected_length <= 32 * 1024 * 1024:
        raise ValueError("compressed VPN Gate payload length is invalid")
    try:
        # The envelope stores a standard zlib stream after the four-byte
        # expected length.  Mirror SoftEther's reader by consuming its two-byte
        # zlib header and four-byte checksum around the raw DEFLATE stream.
        expanded = zlib.decompress(payload[6:-4], wbits=-zlib.MAX_WBITS)
    except zlib.error as exc:
        raise ValueError("compressed VPN Gate payload is not valid DEFLATE") from exc
    if len(expanded) != expected_length:
        raise ValueError("compressed VPN Gate payload length does not match its envelope")
    return expanded


def _native_timestamp(file_bytes: bytes, path: Path) -> datetime:
    header = file_bytes[:240].decode("ascii", errors="ignore")
    match = re.search(r"(?m)^(\d{8}_\d{6}\.\d{3})\r?$", header)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S.%f").replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _read_native_bytes(path: Path) -> bytes:
    last_error: OSError | None = None
    for attempt in range(3):
        try:
            data = path.read_bytes()
            if not 0 < len(data) <= 16 * 1024 * 1024:
                raise ValueError("VPN Gate native catalog size is invalid")
            return data
        except OSError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.15)
    assert last_error is not None
    raise last_error


class NativeCatalogReader:
    """Read the SoftEther VPN Gate plugin's current local ``VPNGate.dat``."""

    @staticmethod
    def read(
        path: str | Path,
        *,
        max_age_hours: int = 24,
        now: datetime | None = None,
        allowed_countries: Iterable[str] | None = None,
    ) -> NativeCatalog:
        catalog_path = Path(path).expanduser().resolve()
        if not catalog_path.is_file():
            raise FileNotFoundError(f"VPN Gate native catalog was not found: {catalog_path}")
        file_bytes = _read_native_bytes(catalog_path)
        if len(file_bytes) <= 0x104:
            raise ValueError("VPN Gate native catalog is too short")
        if not file_bytes[:32].decode("ascii", errors="ignore").startswith("[VPNGate Data File]"):
            raise ValueError("VPN Gate native catalog header is invalid")

        timestamp = _native_timestamp(file_bytes, catalog_path)
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        else:
            current_time = current_time.astimezone(timezone.utc)
        age = current_time - timestamp
        if age < timedelta(hours=-1):
            raise ValueError("VPN Gate native catalog timestamp is unexpectedly in the future")
        if max_age_hours > 0 and age > timedelta(hours=max_age_hours):
            raise ValueError(f"VPN Gate native catalog is stale ({age.total_seconds() / 3600:.1f} hours old)")

        seed = file_bytes[0xF0:0x104]
        key = hashlib.sha1(seed).digest()
        plain = rc4_transform(file_bytes[0x104:], key)
        outer = _PackReader(
            plain,
            maximum_elements=64,
            maximum_values=16,
            maximum_value_bytes=16 * 1024 * 1024,
        ).read()
        payload = _pack_value(outer, "data")
        signature = _pack_value(outer, "sign")
        declared_size = int(_pack_value(outer, "data_size", default=0))
        compressed_value = int(_pack_value(outer, "compressed", default=0))
        if not isinstance(payload, bytes) or len(payload) < 4:
            raise ValueError("VPN Gate native catalog contains no server payload")
        if not isinstance(signature, bytes) or len(signature) != 128:
            raise ValueError("VPN Gate native catalog signature marker is invalid")
        if declared_size and declared_size != len(payload):
            raise ValueError("VPN Gate native catalog payload size does not match its envelope")
        if compressed_value:
            payload = _expand_native_payload(payload)

        inner = _PackReader(
            payload,
            maximum_elements=128,
            maximum_values=4096,
            maximum_value_bytes=4 * 1024 * 1024,
        ).read()
        required = {"CountryShort", "IP", "SslPorts"}
        missing = required.difference(inner)
        if missing:
            raise ValueError(f"VPN Gate native catalog is missing {', '.join(sorted(missing))}")
        row_count = len(inner["CountryShort"][1])
        allowed = {
            country.upper()
            for country in (allowed_countries if allowed_countries is not None else COUNTRY_PRIORITIES)
        }
        relays: list[Relay] = []
        for index in range(row_count):
            country = str(_pack_value(inner, "CountryShort", index, "")).strip().upper()
            if country not in allowed:
                continue
            raw_ip = str(_pack_value(inner, "IP", index, "")).strip()
            try:
                parsed_ip = ip_address(raw_ip)
            except ValueError:
                continue
            if not isinstance(parsed_ip, IPv4Address):
                continue
            ssl_ports = str(_pack_value(inner, "SslPorts", index, ""))
            ports = {
                int(match.group(0))
                for match in re.finditer(r"(?<!\d)\d{1,5}(?!\d)", ssl_ports)
                if 1 <= int(match.group(0)) <= 65_535
            }
            ping = int(_pack_value(inner, "PingToJapan", index, 9999))
            if not 0 <= ping <= 60_000:
                ping = 9999
            speed = max(0, int(_pack_value(inner, "SpeedToJapan", index, 0)))
            score = int(_pack_value(inner, "Score", index, 0))
            sessions = max(0, int(_pack_value(inner, "NumClients", index, 0)))
            host_name = str(_pack_value(inner, "Fqdn", index, "")).strip() or str(parsed_ip)
            udp_port = int(_pack_value(inner, "UdpPort", index, 0) or 0)
            if 1 <= udp_port <= 65_535:
                relays.append(
                    Relay(
                        host_name=host_name,
                        ip=str(parsed_ip),
                        port=udp_port,
                        country_short=country,
                        country_long=str(_pack_value(inner, "CountryFull", index, "")),
                        score=score,
                        ping=ping,
                        speed_mbps=round(speed / 1_000_000, 1),
                        sessions=sessions,
                        source="NativeCatalog",
                        source_priority=3,
                        transport="udp",
                    )
                )
            for port in sorted(ports):
                relays.append(
                    Relay(
                        host_name=host_name,
                        ip=str(parsed_ip),
                        port=port,
                        country_short=country,
                        country_long=str(_pack_value(inner, "CountryFull", index, "")),
                        score=score,
                        ping=ping,
                        speed_mbps=round(speed / 1_000_000, 1),
                        sessions=sessions,
                        source="NativeCatalog",
                        source_priority=3,
                    )
                )
        unique_relays = deduplicate_native_relays(relays)
        return NativeCatalog(
            path=catalog_path,
            timestamp_utc=timestamp,
            age_hours=max(0.0, round(age.total_seconds() / 3600, 2)),
            signature_marker_present=True,
            compressed=bool(compressed_value),
            source_row_count=row_count,
            relays=unique_relays,
        )


def deduplicate_native_relays(relays: Iterable[Relay]) -> tuple[Relay, ...]:
    by_endpoint: dict[str, Relay] = {}
    for relay in relays:
        previous = by_endpoint.get(relay.endpoint)
        if previous is None or _relay_rank(relay) > _relay_rank(previous):
            by_endpoint[relay.endpoint] = relay
    return tuple(
        sorted(
            by_endpoint.values(),
            key=lambda relay: (-relay.priority, -relay.score, relay.ping, relay.endpoint),
        )
    )


def read_vpngate_native_catalog(
    path: str | Path,
    *,
    max_age_hours: int = 24,
    now: datetime | None = None,
    allowed_countries: Iterable[str] | None = None,
) -> NativeCatalog:
    return NativeCatalogReader.read(
        path,
        max_age_hours=max_age_hours,
        now=now,
        allowed_countries=allowed_countries,
    )


def _tokens(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return []
    stripped = re.split(r"\s+[;#]", stripped, maxsplit=1)[0]
    try:
        return shlex.split(stripped, comments=False, posix=True)
    except ValueError:
        return []


def decode_openvpn_config(encoded_config: str) -> str:
    compact = "".join(encoded_config.split())
    if not compact:
        raise ValueError("OpenVPN profile is empty")
    try:
        payload = base64.b64decode(compact, validate=True)
        return payload.decode("utf-8-sig")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("OpenVPN profile is not valid UTF-8 Base64") from exc


def _tcp_ports_for_lines(lines: Iterable[str], inherited_proto: str | None = None) -> list[int]:
    directives = [_tokens(line) for line in lines]
    directives = [tokens for tokens in directives if tokens]
    proto_values = [tokens[1].lower() for tokens in directives if tokens[0].lower() == "proto" and len(tokens) >= 2]
    context_proto = proto_values[-1] if proto_values else inherited_proto
    ports: list[int] = []
    for tokens in directives:
        if tokens[0].lower() != "remote" or len(tokens) < 3:
            continue
        try:
            port = int(tokens[2])
        except ValueError:
            continue
        if not 1 <= port <= 65_535:
            continue
        remote_proto = tokens[3].lower() if len(tokens) >= 4 else context_proto
        if remote_proto in {"tcp", "tcp-client"}:
            ports.append(port)
    return ports


def extract_tcp_ports_from_openvpn_config(encoded_config: str) -> tuple[int, ...]:
    """Return only ports whose OpenVPN profile unambiguously selects TCP."""

    text = decode_openvpn_config(encoded_config)
    top_level: list[str] = []
    blocks: list[list[str]] = []
    current_block: list[str] | None = None
    for line in text.splitlines():
        marker = line.strip().lower()
        if marker == "<connection>":
            current_block = []
            continue
        if marker == "</connection>":
            if current_block is not None:
                blocks.append(current_block)
            current_block = None
            continue
        if current_block is None:
            top_level.append(line)
        else:
            current_block.append(line)

    top_tokens = [_tokens(line) for line in top_level]
    global_protos = [tokens[1].lower() for tokens in top_tokens if len(tokens) >= 2 and tokens[0].lower() == "proto"]
    inherited_proto = global_protos[-1] if global_protos else None
    ports = _tcp_ports_for_lines(top_level)
    for block in blocks:
        ports.extend(_tcp_ports_for_lines(block, inherited_proto=inherited_proto))
    return tuple(dict.fromkeys(ports))


def _integer(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _relay_rank(relay: Relay) -> tuple[int, int, int, int, str]:
    return (
        relay.source_priority,
        1 if relay.sessions > 0 else 0,
        relay.score,
        -relay.ping,
        relay.host_name.lower(),
    )


def parse_vpngate_csv(
    content: str,
    *,
    allowed_countries: Iterable[str] | None = None,
) -> tuple[Relay, ...]:
    """Parse the official API response into unique, explicit TCP relays."""

    allowed = {
        country.upper()
        for country in (allowed_countries if allowed_countries is not None else COUNTRY_PRIORITIES)
    }
    rows = list(csv.reader(StringIO(content.lstrip("\ufeff"))))
    header_index = next(
        (index for index, row in enumerate(rows) if row and row[0].lstrip("\ufeff") == "#HostName"),
        None,
    )
    if header_index is None:
        raise ValueError("VPN Gate CSV header was not found")
    header = list(rows[header_index])
    header[0] = header[0].lstrip("\ufeff#")
    required = {"HostName", "IP", "CountryShort", "OpenVPN_ConfigData_Base64"}
    if not required.issubset(header):
        missing = ", ".join(sorted(required.difference(header)))
        raise ValueError(f"VPN Gate CSV is missing required columns: {missing}")

    by_endpoint: dict[str, Relay] = {}
    for values in rows[header_index + 1 :]:
        if not values or not values[0] or values[0].startswith("*"):
            continue
        if len(values) < len(header):
            values = values + [""] * (len(header) - len(values))
        row = dict(zip(header, values, strict=False))
        country = row.get("CountryShort", "").strip().upper()
        if country not in allowed:
            continue
        try:
            parsed_ip = ip_address(row.get("IP", "").strip())
        except ValueError:
            continue
        if not isinstance(parsed_ip, IPv4Address):
            continue
        try:
            ports = extract_tcp_ports_from_openvpn_config(row.get("OpenVPN_ConfigData_Base64", ""))
        except ValueError:
            continue
        for port in ports:
            relay = Relay(
                host_name=row.get("HostName", "").strip() or str(parsed_ip),
                ip=str(parsed_ip),
                port=port,
                country_short=country,
                country_long=row.get("CountryLong", "").strip(),
                score=_integer(row.get("Score")),
                ping=max(0, _integer(row.get("Ping"), 9999)),
                speed_mbps=round(max(0, _integer(row.get("Speed"))) / 1_000_000, 1),
                sessions=max(0, _integer(row.get("NumVpnSessions"))),
                source="HttpsApi",
                source_priority=2,
            )
            previous = by_endpoint.get(relay.endpoint)
            if previous is None or _relay_rank(relay) > _relay_rank(previous):
                by_endpoint[relay.endpoint] = relay

    return tuple(
        sorted(
            by_endpoint.values(),
            key=lambda relay: (
                -relay.priority,
                -relay.source_priority,
                -(1 if relay.sessions > 0 else 0),
                -relay.score,
                relay.ping,
                relay.endpoint,
            ),
        )
    )
