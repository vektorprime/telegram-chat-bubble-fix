#!/usr/bin/env python3
"""
Telegram Desktop Chat Bubble Width Patcher

Widens the chat bubbles by forcing the bubble width at the writer that
stores it to the width global ('mov [rip+disp], eax'). The 6-byte load
feeding that store ('mov eax, [rbp+disp]') is replaced with
'mov eax, <width>' (B8 ..) plus a NOP, so the constant flows through the
existing global store. Creates a backup first.

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

# The bubble width is stored to the width global by 'mov [rip+disp], eax'.
# The load feeding that store is the patch site:
#     89 85 ?? ?? ?? ??      mov [rbp+disp], eax       (width saved to stack)
#     8B D3                  mov edx, ebx
#     B9 80 02 00 00         mov ecx, 0x280
#     E8 ?? ?? ?? ??         call
#     89 85 ?? ?? ?? ??      mov [rbp+disp], eax
#     8B D3                  mov edx, ebx
#     B9 20 1C 00 00         mov ecx, 0x1C20
#     E8 ?? ?? ?? ??         call
#     8B D8                  mov ebx, eax
#     8B 85 ?? ?? ?? ??      mov eax, [rbp+disp]  <- replaced by 'mov eax,<width>'+nop
#     89 05 ?? ?? ?? ??      mov [rip+disp], eax  <- stores the width to the global
# The ecx constants and fixed bytes make the match unambiguous; the wildcarded
# frame/rip displacements and call rel32s keep it working across updates.
SEARCH_SIG = "8985????????8bd3b980020000e8????????8985????????8bd3b9201c0000e8????????8bd88b85????????8905????????"

# Shape of an already-patched site: the load replaced by
# 'mov eax, <any imm32>' (B8 ..) + nop (90). Matching on the shape (not a
# specific immediate) lets us re-patch binaries modified by older builds of
# this tool even if the width encoding changed.
PATCHED_SIG = "8985????????8bd3b980020000e8????????8985????????8bd3b9201c0000e8????????8bd8b8????????908905????????"

# 'mov eax,[rbp+disp32]' (8B 85 .., 6 bytes) is replaced by 'mov eax,imm32'
# (B8 .., 5 bytes) + 'nop' (90), so the patch never changes the code size.
PATCH_LEN = 6
PATCH_OFFSET = 38  # start of the 'mov eax,[rbp+disp]' load inside the match

OPTIONS = {
    '1': ('800px',  'B82003000090'),
    '2': ('1000px', 'B8E803000090'),
    '3': ('1200px', 'B8B004000090'),
    '4': ('1500px', 'B8DC05000090'),
    '5': ('2000px', 'B8D007000090'),
}

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


def detect_current_key(data: bytes, offsets: list):
    """Return the OPTIONS key currently applied, by reading the 'mov eax,imm32'
    bytes at any patched site, or None if still original."""
    for off in offsets:
        b = data[off + PATCH_OFFSET:off + PATCH_OFFSET + PATCH_LEN]
        if len(b) == PATCH_LEN and b[0] == 0xB8:
            for key, (_desc, hx) in OPTIONS.items():
                if b == bytes.fromhex(hx):
                    return key
    return None


def locate_patch_sites(data: bytes):
    """Find every patch site. Returns (offsets, current_key). Handles both the
    unpatched signature and a previously applied patch so the tool can
    re-patch an already-modified binary."""
    offsets = find_all_wildcard(data, SEARCH_SIG)
    if not offsets:
        offsets = find_all_wildcard(data, PATCHED_SIG)
    return offsets, detect_current_key(data, offsets)


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

    offsets, current_key = locate_patch_sites(data)

    if offsets:
        if current_key:
            print(f"\nFile was previously patched with: {OPTIONS[current_key][0]}")
        elif any(data[o + PATCH_OFFSET] == 0xB8 for o in offsets):
            print("\nFile was previously patched with an unrecognized width.")
        else:
            print("\nFound unpatched width writer(s):")
        for off in offsets:
            print(f"  Offset: 0x{off + PATCH_OFFSET:X}  (width load -> global store)")
    else:
        print("\nWidth-writer signature not found.")
        print("No previously-applied patch pattern found either.")
        print("Telegram was likely updated; update SEARCH_SIG for this version,")
        print("then retry.")
        sys.exit(1)

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

    # Apply 'mov eax,<width>'+nop over the load (patch highest offsets first so
    # earlier offsets stay valid)
    repl = bytes.fromhex(hex_str)
    new_data = data
    for off in sorted(offsets, reverse=True):
        site = off + PATCH_OFFSET
        new_data = new_data[:site] + repl + new_data[site + PATCH_LEN:]

    try:
        with open(exe_path, 'wb') as f:
            f.write(new_data)
    except PermissionError:
        print("ERROR: Cannot write. Make sure Telegram is NOT running.")
        sys.exit(1)

    print(f"\nSuccess. Patched {len(offsets)} site(s) with {desc} width.")
    launch = input("Would you like to launch Telegram now? [Y/n]: ").strip().lower()
    if launch != 'n':
        os.startfile(str(exe_path))


if __name__ == '__main__':
    main()
