from __future__ import annotations

_NAMED: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "esc": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "delete": 0x2E, "win": 0x5B, "apps": 0x5D,
}  # fmt: skip


def vk(name: str) -> int:
    n = name.strip().lower()
    if n in _NAMED:
        return _NAMED[n]
    if len(n) == 1 and (n.isdigit() or "a" <= n <= "z"):
        return ord(n.upper())
    if n.startswith("f") and n[1:].isdigit() and 1 <= int(n[1:]) <= 12:
        return 0x70 + int(n[1:]) - 1
    raise ValueError(f"unknown key {name!r}")


def chord(spec: str) -> list[int]:
    return [vk(part) for part in spec.split("+")]
