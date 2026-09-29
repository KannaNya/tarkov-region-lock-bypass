"""Thin ctypes bindings for the Windows APIs the keeper needs.

Nothing in this package starts a console process: routes, interfaces,
processes and cross-process signalling all go straight to the Win32 API, which
is locale-independent and never flashes a window.
"""
