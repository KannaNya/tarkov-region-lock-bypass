"""Anonymous, verified HTTPS transport probe; not a login or WSS health claim."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
import socket
import ssl
import time
from dataclasses import dataclass

from .eft_logs import is_authorization_host


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str


def probe_authorization(targets, *, timeout=6.0, deadline=None) -> HealthResult:
    """Probe one hostname per routed IP, bounded by a shared wall-clock budget.

    Connect to the exact IPv4 /32, with hostname SNI and normal CA/hostname
    validation. HEAD / sends no credentials, cookies, game data or redirects.
    Any HTTP status (including 401/403) proves response transport, not login.
    """
    targets = tuple(targets)
    probe_timeout = max(0.1, float(timeout))
    deadline = min(deadline or float('inf'), time.monotonic() + probe_timeout)
    context = ssl.create_default_context()

    if not targets:
        return HealthResult(False, "no authorization targets were probed")

    def probe_one(target):
        host = next((h for h in target.hostnames if is_authorization_host(h)), None)
        if not host:
            return False, 'no approved hostname for TLS SNI'

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError('HTTPS probe budget exhausted')
            return value

        try:
            with socket.create_connection((target.ip, 443), timeout=remaining()) as raw:
                raw.settimeout(remaining())
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    tls.settimeout(remaining())
                    tls.sendall(f'HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode('ascii'))
                    response = b''
                    while b'\r\n' not in response and len(response) < 1024:
                        tls.settimeout(remaining())
                        chunk = tls.recv(1024 - len(response))
                        if not chunk:
                            break
                        response += chunk
                    if not response.startswith((b'HTTP/1.0 ', b'HTTP/1.1 ')):
                        return False, f'{host}: empty or non-HTTP response'
                    return True, ''
        except (OSError, ValueError) as exc:
            return False, f'{host}: {type(exc).__name__}'

    # Run target probes concurrently. A dark host therefore cannot spend the
    # entire shared budget before a later healthy host is checked, while every
    # socket/TLS operation still observes the same hard deadline.
    results: list[tuple[bool, str] | None] = [None] * len(targets)
    with ThreadPoolExecutor(
        max_workers=min(8, len(targets)),
        thread_name_prefix='TarkovCisHttpsProbe',
    ) as executor:
        pending = {
            executor.submit(probe_one, target): index
            for index, target in enumerate(targets)
        }
        try:
            for future in as_completed(
                pending, timeout=max(0.0, deadline - time.monotonic())
            ):
                index = pending[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = (False, f'probe worker: {type(exc).__name__}')
        except FutureTimeout:
            for future in pending:
                future.cancel()

    checked = 0
    failures: list[str] = []
    for result in results:
        if result is None:
            failures.append('HTTPS probe deadline exhausted')
        elif result[0]:
            checked += 1
        else:
            failures.append(result[1])

    # A single authorization hostname can be intentionally dark, rate-limited,
    # or temporarily unavailable while another hostname on the same relay is
    # healthy.  Failing fast on the first target made the keeper tear down a
    # valid SoftEther session and caused the Windows NCSI/MSN popup loop.  A
    # health cycle is therefore successful when at least one independent
    # routed target answered; failover is reserved for an all-target failure.
    if checked:
        suffix = f"; {len(failures)} target(s) skipped" if failures else ""
        return HealthResult(
            True,
            f'verified HTTPS responses from {checked} routed IP(s){suffix}',
        )
    detail = "; ".join(failures[:3]) or "no authorization targets were probed"
    if len(failures) > 3:
        detail += f"; +{len(failures) - 3} more"
    return HealthResult(False, detail)
