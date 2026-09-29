"""IPv4 route and interface access through iphlpapi.dll.

Only the handful of calls the keeper needs are bound: resolving an interface
alias, reading its unicast address, enumerating/creating/deleting IPv4 routes
and changing the interface metric.  Routes created with CreateIpForwardEntry2
live in the active store only and vanish on reboot or when the adapter goes
down, which is exactly the lifetime the /32 authorization routes need.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import ipaddress
import sys

AF_INET = 2
NO_ERROR = 0
ERROR_NOT_FOUND = 1168
ERROR_OBJECT_ALREADY_EXISTS = 5010
MIB_IPPROTO_NETMGMT = 3
IP_DAD_STATE_PREFERRED = 4


class IpHelperError(OSError):
    def __init__(self, operation: str, code: int) -> None:
        super().__init__(code, f"{operation} failed with Win32 error {code}")
        self.operation = operation
        self.code = code


class SOCKADDR_IN(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_ushort),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_ubyte * 8),
    ]


class SOCKADDR_IN6(ctypes.Structure):
    _fields_ = [
        ("sin6_family", ctypes.c_ushort),
        ("sin6_port", ctypes.c_ushort),
        ("sin6_flowinfo", ctypes.c_uint32),
        ("sin6_addr", ctypes.c_ubyte * 16),
        ("sin6_scope_id", ctypes.c_uint32),
    ]


class SOCKADDR_INET(ctypes.Union):
    _fields_ = [
        ("Ipv4", SOCKADDR_IN),
        ("Ipv6", SOCKADDR_IN6),
        ("si_family", ctypes.c_ushort),
    ]


class IP_ADDRESS_PREFIX(ctypes.Structure):
    _fields_ = [("Prefix", SOCKADDR_INET), ("PrefixLength", ctypes.c_ubyte)]


class MIB_IPFORWARD_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", ctypes.c_uint32),
        ("DestinationPrefix", IP_ADDRESS_PREFIX),
        ("NextHop", SOCKADDR_INET),
        ("SitePrefixLength", ctypes.c_ubyte),
        ("ValidLifetime", ctypes.c_uint32),
        ("PreferredLifetime", ctypes.c_uint32),
        ("Metric", ctypes.c_uint32),
        ("Protocol", ctypes.c_int),
        ("Loopback", ctypes.c_ubyte),
        ("AutoconfigureAddress", ctypes.c_ubyte),
        ("Publish", ctypes.c_ubyte),
        ("Immortal", ctypes.c_ubyte),
        ("Age", ctypes.c_uint32),
        ("Origin", ctypes.c_int),
    ]


class MIB_UNICASTIPADDRESS_ROW(ctypes.Structure):
    _fields_ = [
        ("Address", SOCKADDR_INET),
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", ctypes.c_uint32),
        ("PrefixOrigin", ctypes.c_int),
        ("SuffixOrigin", ctypes.c_int),
        ("ValidLifetime", ctypes.c_uint32),
        ("PreferredLifetime", ctypes.c_uint32),
        ("OnLinkPrefixLength", ctypes.c_ubyte),
        ("SkipAsSource", ctypes.c_ubyte),
        ("DadState", ctypes.c_int),
        ("ScopeId", ctypes.c_uint32),
        ("CreationTimeStamp", ctypes.c_int64),
    ]


class NL_INTERFACE_OFFLOAD_ROD(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_ubyte)]


class MIB_IPINTERFACE_ROW(ctypes.Structure):
    _fields_ = [
        ("Family", ctypes.c_ushort),
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", ctypes.c_uint32),
        ("MaxReassemblySize", ctypes.c_uint32),
        ("InterfaceIdentifier", ctypes.c_uint64),
        ("MinRouterAdvertisementInterval", ctypes.c_uint32),
        ("MaxRouterAdvertisementInterval", ctypes.c_uint32),
        ("AdvertisingEnabled", ctypes.c_ubyte),
        ("ForwardingEnabled", ctypes.c_ubyte),
        ("WeakHostSend", ctypes.c_ubyte),
        ("WeakHostReceive", ctypes.c_ubyte),
        ("UseAutomaticMetric", ctypes.c_ubyte),
        ("UseNeighborUnreachabilityDetection", ctypes.c_ubyte),
        ("ManagedAddressConfigurationSupported", ctypes.c_ubyte),
        ("OtherStatefulConfigurationSupported", ctypes.c_ubyte),
        ("AdvertiseDefaultRoute", ctypes.c_ubyte),
        ("RouterDiscoveryBehavior", ctypes.c_int),
        ("DadTransmits", ctypes.c_uint32),
        ("BaseReachableTime", ctypes.c_uint32),
        ("RetransmitTime", ctypes.c_uint32),
        ("PathMtuDiscoveryTimeout", ctypes.c_uint32),
        ("LinkLocalAddressBehavior", ctypes.c_int),
        ("LinkLocalAddressTimeout", ctypes.c_uint32),
        ("ZoneIndices", ctypes.c_uint32 * 16),
        ("SitePrefixLength", ctypes.c_uint32),
        ("Metric", ctypes.c_uint32),
        ("NlMtu", ctypes.c_uint32),
        ("Connected", ctypes.c_ubyte),
        ("SupportsWakeUpPatterns", ctypes.c_ubyte),
        ("SupportsNeighborDiscovery", ctypes.c_ubyte),
        ("SupportsRouterDiscovery", ctypes.c_ubyte),
        ("ReachableTime", ctypes.c_uint32),
        ("TransmitOffload", NL_INTERFACE_OFFLOAD_ROD),
        ("ReceiveOffload", NL_INTERFACE_OFFLOAD_ROD),
        ("DisableDefaultRoutes", ctypes.c_ubyte),
    ]


# Guard against a silent layout mistake corrupting the kernel's view of a row.
assert ctypes.sizeof(MIB_IPFORWARD_ROW2) == 104
assert ctypes.sizeof(MIB_UNICASTIPADDRESS_ROW) == 80
assert ctypes.sizeof(MIB_IPINTERFACE_ROW) == 168


@dataclass(frozen=True, slots=True)
class Route:
    destination: str
    prefix_length: int
    next_hop: str
    interface_index: int
    metric: int

    @property
    def prefix(self) -> str:
        return f"{self.destination}/{self.prefix_length}"


@dataclass(frozen=True, slots=True)
class InterfaceInfo:
    index: int
    connected: bool
    metric: int
    automatic_metric: bool


_api = None


def _load():
    global _api
    if _api is not None:
        return _api
    if sys.platform != "win32":
        raise OSError("iphlpapi is only available on Windows")
    api = ctypes.WinDLL("iphlpapi.dll")
    signatures = {
        "ConvertInterfaceAliasToLuid": ([wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_uint64)], wintypes.DWORD),
        "ConvertInterfaceLuidToIndex": ([ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint32)], wintypes.DWORD),
        "GetIpForwardTable2": ([ctypes.c_ushort, ctypes.POINTER(ctypes.c_void_p)], wintypes.DWORD),
        "GetUnicastIpAddressTable": ([ctypes.c_ushort, ctypes.POINTER(ctypes.c_void_p)], wintypes.DWORD),
        "FreeMibTable": ([ctypes.c_void_p], None),
        "InitializeIpForwardEntry": ([ctypes.POINTER(MIB_IPFORWARD_ROW2)], None),
        "CreateIpForwardEntry2": ([ctypes.POINTER(MIB_IPFORWARD_ROW2)], wintypes.DWORD),
        "DeleteIpForwardEntry2": ([ctypes.POINTER(MIB_IPFORWARD_ROW2)], wintypes.DWORD),
        "InitializeIpInterfaceEntry": ([ctypes.POINTER(MIB_IPINTERFACE_ROW)], None),
        "GetIpInterfaceEntry": ([ctypes.POINTER(MIB_IPINTERFACE_ROW)], wintypes.DWORD),
        "SetIpInterfaceEntry": ([ctypes.POINTER(MIB_IPINTERFACE_ROW)], wintypes.DWORD),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(api, name)
        function.argtypes = argtypes
        function.restype = restype
    _api = api
    return api


def _check(operation: str, code: int) -> None:
    if code != NO_ERROR:
        raise IpHelperError(operation, code)


def _ipv4_text(address: SOCKADDR_INET) -> str:
    return str(ipaddress.IPv4Address(bytes(address.Ipv4.sin_addr)))


def _set_ipv4(address: SOCKADDR_INET, value: str) -> None:
    address.si_family = AF_INET
    address.Ipv4.sin_addr = (ctypes.c_ubyte * 4)(*ipaddress.IPv4Address(value).packed)


def interface_index(alias: str) -> int | None:
    """Return the ifIndex of an interface alias, or None if it does not exist."""

    api = _load()
    luid = ctypes.c_uint64()
    if api.ConvertInterfaceAliasToLuid(alias, ctypes.byref(luid)) != NO_ERROR:
        return None
    index = ctypes.c_uint32()
    if api.ConvertInterfaceLuidToIndex(ctypes.byref(luid), ctypes.byref(index)) != NO_ERROR:
        return None
    return int(index.value)


def ipv4_routes() -> list[Route]:
    api = _load()
    table = ctypes.c_void_p()
    _check("GetIpForwardTable2", api.GetIpForwardTable2(AF_INET, ctypes.byref(table)))
    try:
        count = ctypes.c_uint32.from_address(table.value).value
        # MIB_IPFORWARD_TABLE2.Table follows NumEntries at the 8-byte aligned offset.
        rows = (MIB_IPFORWARD_ROW2 * count).from_address(table.value + 8)
        return [
            Route(
                destination=_ipv4_text(row.DestinationPrefix.Prefix),
                prefix_length=int(row.DestinationPrefix.PrefixLength),
                next_hop=_ipv4_text(row.NextHop),
                interface_index=int(row.InterfaceIndex),
                metric=int(row.Metric),
            )
            for row in rows
        ]
    finally:
        api.FreeMibTable(table)


def preferred_ipv4_addresses(index: int) -> list[str]:
    """Usable (DAD-preferred) unicast IPv4 addresses on one interface."""

    api = _load()
    table = ctypes.c_void_p()
    _check("GetUnicastIpAddressTable", api.GetUnicastIpAddressTable(AF_INET, ctypes.byref(table)))
    try:
        count = ctypes.c_uint32.from_address(table.value).value
        rows = (MIB_UNICASTIPADDRESS_ROW * count).from_address(table.value + 8)
        return [
            _ipv4_text(row.Address)
            for row in rows
            if int(row.InterfaceIndex) == index and row.DadState == IP_DAD_STATE_PREFERRED
        ]
    finally:
        api.FreeMibTable(table)


def _route_row(destination: str, prefix_length: int, next_hop: str, index: int) -> MIB_IPFORWARD_ROW2:
    api = _load()
    row = MIB_IPFORWARD_ROW2()
    api.InitializeIpForwardEntry(ctypes.byref(row))
    row.InterfaceIndex = index
    _set_ipv4(row.DestinationPrefix.Prefix, destination)
    row.DestinationPrefix.PrefixLength = prefix_length
    _set_ipv4(row.NextHop, next_hop)
    return row


def add_route(destination: str, prefix_length: int, next_hop: str, index: int, *, metric: int = 1) -> bool:
    """Create an active-store route; return False if it already existed."""

    row = _route_row(destination, prefix_length, next_hop, index)
    row.Metric = metric
    row.Protocol = MIB_IPPROTO_NETMGMT
    code = _load().CreateIpForwardEntry2(ctypes.byref(row))
    if code == ERROR_OBJECT_ALREADY_EXISTS:
        return False
    _check("CreateIpForwardEntry2", code)
    return True


def delete_route(destination: str, prefix_length: int, next_hop: str, index: int) -> bool:
    """Delete one exact route; return False if it was already gone."""

    row = _route_row(destination, prefix_length, next_hop, index)
    code = _load().DeleteIpForwardEntry2(ctypes.byref(row))
    if code == ERROR_NOT_FOUND:
        return False
    _check("DeleteIpForwardEntry2", code)
    return True


def _interface_row(index: int) -> MIB_IPINTERFACE_ROW:
    api = _load()
    row = MIB_IPINTERFACE_ROW()
    api.InitializeIpInterfaceEntry(ctypes.byref(row))
    row.Family = AF_INET
    row.InterfaceIndex = index
    _check("GetIpInterfaceEntry", api.GetIpInterfaceEntry(ctypes.byref(row)))
    return row


def interface_info(index: int) -> InterfaceInfo:
    row = _interface_row(index)
    return InterfaceInfo(
        index=index,
        connected=bool(row.Connected),
        metric=int(row.Metric),
        automatic_metric=bool(row.UseAutomaticMetric),
    )


def set_interface_metric(index: int, metric: int) -> None:
    """Persistently pin the IPv4 interface metric (disables automatic metric)."""

    row = _interface_row(index)
    row.UseAutomaticMetric = 0
    row.Metric = metric
    # SetIpInterfaceEntry rejects IPv4 rows unless SitePrefixLength is zero.
    row.SitePrefixLength = 0
    _check("SetIpInterfaceEntry", _load().SetIpInterfaceEntry(ctypes.byref(row)))
