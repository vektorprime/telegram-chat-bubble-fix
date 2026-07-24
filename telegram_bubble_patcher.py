#!/usr/bin/env python3
"""
Telegram Desktop Chat Bubble Width Patcher

Widens the chat bubbles by forcing the bubble width at two writers:
  1. the settings-struct field copy ('mov eax,[rbx+9C8]; mov [rsi+9C8],eax')
  2. a float->int conversion that stores the width to global RVA 0x8C8F160
Both are overwritten with the chosen constant width. Creates a backup first.

Usage:
    python telegram_bubble_patcher.py          # Interactive mode
    python telegram_bubble_patcher.py --restore # Restore from backup
"""

import os
import sys
import shutil
import subprocess
import time
from pathlib import Path

# The bubble width is stored in a settings-struct field (offset +0x9C8) and
# copied between objects with this instruction pair:
#     8B 83 C8 09 00 00      mov eax, [rbx+9C8]   <- source load (we patch this)
#     89 86 C8 09 00 00      mov [rsi+9C8], eax   <- destination store (anchor)
# Replacing the 6-byte load with 'mov eax, <width>; nop' (B8 .. 90) forces the
# width that gets stored. The store anchor makes the match unambiguous.
LOAD_HEX = "8b83c8090000"        # mov eax, [rbx+9C8]  (patch target, 6 bytes)
STORE_HEX = "8986c8090000"       # mov [rsi+9C8], eax  (following anchor)
SEARCH_SIG = LOAD_HEX + STORE_HEX

# Second width writer: a float->int conversion whose result is stored to the
# global at RVA 0x8C8F160. We replace this 13-byte block:
#     F2 0F 2C C0      cvttsd2si eax, xmm0   } replaced by 'mov eax,<width>; nop'
#     85 C0            test eax, eax         }
#     41 0F 28 C0      movaps xmm0, xmm8     <- kept (feeds the next iteration)
#     0F 44 C3         cmove eax, ebx        <- dropped (would clobber our value)
# The trailing mulsd/store are left untouched and act as a re-find anchor.
CVTT_SIG = "f20f2cc085c0410f28c00f44c3f20f590552afae048905d434aa05"
CVTT_KEEP = "410f28c0909090"                 # movaps xmm0,xmm8 + 3 nops
CVTT_SUFFIX = "f20f590552afae048905d434aa05"  # unchanged tail (patched re-find anchor)
CVTT_PATCH_LEN = 13

KIND_LOAD = 'load'   # 6-byte field-copy load
KIND_CVTT = 'cvtt'   # 13-byte cvttsd2si block

# Shape of an already-patched site: 'mov eax, <any imm32>; nop'. Matching on the
# shape (not a specific immediate) lets us re-patch binaries modified by older
# builds of this tool even if the width encoding changed.
LOAD_PATCHED_SIG = "b8????????90" + STORE_HEX
CVTT_PATCHED_SIG = "b8????????90" + CVTT_KEEP + CVTT_SUFFIX

OPTIONS = {
    '1': ('800px',  'B82003000090'),
    '2': ('1000px', 'B8E803000090'),
    '3': ('1200px', 'B8B004000090'),
    '4': ('1500px', 'B8DC05000090'),
    '5': ('2000px', 'B8D007000090'),
}

# Last-known good offset of the load (Telegram Desktop 7.0.5.0). Used only as a
# last resort and only after the bytes there are validated as a real patch site.
FALLBACK_OFFSET = 0x2C8DA3A
PROCESS_NAME = "Telegram.exe"


def get_default_exe_path():
    appdata = os.environ.get('APPDATA', '')
    return Path(appdata) / 'Telegram Desktop' / 'Telegram.exe'


def find_exe():
    exe_path = get_default_exe_path()
    if exe_path.is_file():
        return exe_path

    print(f"Telegram.exe not found at: {exe_path}")

    while True:
        folder = input("Enter the folder containing Telegram.exe (or 'q' to quit): ").strip()
        if folder.lower() == 'q':
            sys.exit(0)

        folder = folder.strip('"')
        exe_path = Path(folder) / 'Telegram.exe'
        if exe_path.is_file():
            return exe_path
        print(f"Telegram.exe not found at: {exe_path}")


def ensure_telegram_closed():
    """Check if Telegram is running and offer to close it."""
    result = subprocess.run(
        ['tasklist', '/FI', f'IMAGENAME eq {PROCESS_NAME}', '/NH'],
        capture_output=True, text=True
    )
    if PROCESS_NAME not in result.stdout:
        return  # not running

    print("\nTelegram is currently running.")
    answer = input("Close Telegram to continue? [Y/n]: ").strip().lower()
    if answer == 'n':
        print("Cannot proceed while Telegram is running. Exiting.")
        sys.exit(0)

    print("Closing Telegram...")
    subprocess.run(
        ['taskkill', '/IM', PROCESS_NAME, '/F'],
        capture_output=True
    )
    time.sleep(0.5)
    print("Telegram closed.")


def find_all_occurrences(data: bytes, pattern: bytes) -> list:
    positions = []
    start = 0
    while True:
        pos = data.find(pattern, start)
        if pos == -1:
            break
        positions.append(pos)
        start = pos + 1
    return positions


def find_all_wildcard(data: bytes, hex_pattern: str) -> list:
    """Find all offsets matching hex_pattern, where '??' is a wildcard byte.
    Anchors on the longest run of fixed bytes for speed."""
    toks = [hex_pattern[i:i + 2] for i in range(0, len(hex_pattern), 2)]
    n = len(toks)
    fixed = [None if t == '??' else int(t, 16) for t in toks]

    best_start, best_len = 0, 0
    i = 0
    while i < n:
        if fixed[i] is not None:
            j = i
            while j < n and fixed[j] is not None:
                j += 1
            if j - i > best_len:
                best_start, best_len = i, j - i
            i = j
        else:
            i += 1
    if best_len == 0:
        return []

    anchor = bytes(fixed[best_start:best_start + best_len])
    positions = []
    start = 0
    while True:
        f = data.find(anchor, start)
        if f == -1:
            break
        base = f - best_start
        if 0 <= base and base + n <= len(data):
            if all(fixed[k] is None or data[base + k] == fixed[k] for k in range(n)):
                positions.append(base)
        start = f + 1
    return positions


def is_valid_load_site(data: bytes, off: int) -> bool:
    """A safe load patch site is 'mov eax,[rbx+9C8]' immediately followed by the
    store 'mov [rsi+9C8],eax'. The store anchor guards against patching unrelated
    code if the offset ever goes stale."""
    if data[off + 6:off + 12] != bytes.fromhex(STORE_HEX):
        return False
    load = data[off:off + 6]
    if load == bytes.fromhex(LOAD_HEX):
        return True
    # Already patched: 'mov eax, imm32; nop' (B8 .. 90)
    return len(load) == 6 and load[0] == 0xB8 and load[5] == 0x90


def patch_len(kind: str) -> int:
    return 6 if kind == KIND_LOAD else CVTT_PATCH_LEN


def make_replacement(kind: str, option_hex: str) -> bytes:
    if kind == KIND_LOAD:
        return bytes.fromhex(option_hex)
    # cvtt: 'mov eax,<width>; nop' + preserved movaps + nops
    return bytes.fromhex(option_hex + CVTT_KEEP)


def detect_current_key(data: bytes, sites: list):
    """Return the OPTIONS key currently applied, by reading the 'mov eax,imm32'
    bytes at any patched site, or None if still original."""
    for off, _kind in sites:
        b = data[off:off + 6]
        if len(b) == 6 and b[0] == 0xB8 and b[5] == 0x90:
            for key, (_desc, hx) in OPTIONS.items():
                if b == bytes.fromhex(hx):
                    return key
    return None


def locate_patch_targets(data: bytes):
    """Find every patch site. Returns (sites, current_key) where sites is a list
    of (offset, kind). Handles both the unpatched signatures and previously
    applied patches so the tool can re-patch an already-modified binary."""
    sites = []

    # Field-copy load sites ('mov eax,[rbx+9C8]; mov [rsi+9C8],eax')
    load_pos = find_all_occurrences(data, bytes.fromhex(SEARCH_SIG))
    if load_pos:
        sites += [(p, KIND_LOAD) for p in load_pos]
    else:
        # Already patched: 'mov eax,<imm32>; nop' followed by the store anchor.
        sites += [(p, KIND_LOAD) for p in find_all_wildcard(data, LOAD_PATCHED_SIG)]

    # cvttsd2si site (global 0x8C8F160)
    cvtt_pos = find_all_occurrences(data, bytes.fromhex(CVTT_SIG))
    if cvtt_pos:
        sites += [(p, KIND_CVTT) for p in cvtt_pos]
    else:
        sites += [(p, KIND_CVTT) for p in find_all_wildcard(data, CVTT_PATCHED_SIG)]

    return sites, detect_current_key(data, sites)


def restore_backup(exe_path: Path):
    backup = exe_path.with_suffix('.exe.bak')
    if not backup.is_file():
        print(f"No backup found at: {backup}")
        return False

    shutil.copy2(backup, exe_path)
    print(f"Restored original from: {backup}")
    return True


def print_banner():
    print(r"""
  _____                                _
 |_   _|__  __ _  __ _ _ _ _ _ __  ___| |___
   | |/ _ \/ _` |/ _` | '_| ' \/ -_) / -_)
   |_|\___/\__, |\__,_|_| |_|_|_\___|_\___|
            |___/
  Chat Bubble Width Patcher
""")


def main():
    print_banner()

    if len(sys.argv) > 1 and sys.argv[1] == '--restore':
        exe_path = get_default_exe_path()
        if not exe_path.is_file():
            folder = input("Enter folder containing Telegram.exe: ").strip().strip('"')
            exe_path = Path(folder) / 'Telegram.exe'
        ensure_telegram_closed()
        restore_backup(exe_path)
        return

    exe_path = find_exe()
    print(f"\nTarget: {exe_path}")

    ensure_telegram_closed()

    try:
        with open(exe_path, 'rb') as f:
            data = f.read()
    except PermissionError:
        print("ERROR: Cannot read the file. Make sure Telegram is not running.")
        sys.exit(1)

    sites, current_key = locate_patch_targets(data)

    if sites:
        if current_key:
            print(f"\nFile was previously patched with: {OPTIONS[current_key][0]}")
        else:
            print("\nFound unpatched width writer(s):")
        for off, kind in sites:
            label = "field +9C8 copy" if kind == KIND_LOAD else "cvttsd2si -> 0x8C8F160"
            print(f"  Offset: 0x{off:X}  ({label})")
        if not any(k == KIND_CVTT for _o, k in sites):
            print("  NOTE: secondary writer (cvttsd2si) not found in this build.")
    else:
        print(f"\nWriter signature '{SEARCH_SIG}' not found.")
        print("No previously-applied patch pattern found either.")

        if FALLBACK_OFFSET + 12 > len(data):
            print(f"\nERROR: Fallback offset 0x{FALLBACK_OFFSET:X} is out of range.")
            print(f"File size: {len(data)} bytes (0x{len(data):X})")
            sys.exit(1)

        current_bytes = data[FALLBACK_OFFSET:FALLBACK_OFFSET + 12]
        print(f"\nFalling back to hardcoded file offset: 0x{FALLBACK_OFFSET:X}")
        print(f"Current bytes at that offset: {' '.join(f'{b:02X}' for b in current_bytes)}")

        # Safety check: never patch unless the offset really holds the
        # bubble-width load+store pair. Patching anything else corrupts the
        # code and crashes Telegram.
        if not is_valid_load_site(data, FALLBACK_OFFSET):
            print("\nERROR: The bytes at the fallback offset are NOT the")
            print("bubble-width load+store pair. Patching here would corrupt")
            print("Telegram.exe and crash it. Aborting without changes.")
            print("\nTelegram was likely updated; the fallback offset is stale.")
            print("Update SEARCH_SIG / FALLBACK_OFFSET for this version, then retry.")
            sys.exit(1)

        print("WARNING: This offset may be incorrect if Telegram was updated.")
        proceed = input("\nContinue with this offset anyway? [y/N]: ").strip().lower()
        if proceed != 'y':
            print("No changes made.")
            return

        sites = [(FALLBACK_OFFSET, KIND_LOAD)]
        current_key = None

    # Show menu
    print("\n" + "=" * 40)
    print("  Select chat bubble width:")
    print("=" * 40)
    for key in sorted(OPTIONS.keys(), key=int):
        desc, _ = OPTIONS[key]
        marker = " <-- current" if key == current_key else ""
        print(f"  [{key}] {desc}{marker}")
    print(f"  [r] Restore original from backup")
    print(f"  [q] Quit")

    choice = input("\nChoice: ").strip().lower()

    if choice == 'q':
        print("No changes made.")
        return
    if choice == 'r':
        restore_backup(exe_path)
        return
    if choice not in OPTIONS:
        print("Invalid choice.")
        sys.exit(1)

    desc, hex_str = OPTIONS[choice]

    confirm = input(
        f"\nPatch with {desc} width? "
        f"Telegram must NOT be running. [y/N]: "
    ).strip().lower()
    if confirm != 'y':
        print("No changes made.")
        return

    # Create backup if needed
    backup_path = exe_path.with_suffix('.exe.bak')
    if backup_path.is_file():
        overwrite = input(f"Backup exists. Create fresh backup? [Y/n]: ").strip().lower()
        if overwrite != 'n':
            shutil.copy2(exe_path, backup_path)
            print(f"Backup updated: {backup_path}")
    else:
        shutil.copy2(exe_path, backup_path)
        print(f"Backup created: {backup_path}")

    # Apply replacements (patch highest offsets first so earlier offsets stay valid)
    new_data = data
    for off, kind in sorted(sites, key=lambda s: s[0], reverse=True):
        repl = make_replacement(kind, hex_str)
        plen = patch_len(kind)
        new_data = new_data[:off] + repl + new_data[off + plen:]

    try:
        with open(exe_path, 'wb') as f:
            f.write(new_data)
    except PermissionError:
        print("ERROR: Cannot write. Make sure Telegram is NOT running.")
        sys.exit(1)

    print(f"\nDone. Patched {len(sites)} site(s) with {desc} width.")
    print("Restart Telegram to see the changes.")


if __name__ == '__main__':
    main()
