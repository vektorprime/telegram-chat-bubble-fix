#!/usr/bin/env python3
"""
Telegram Desktop Chat Bubble Width Patcher

Searches Telegram.exe for a hex pattern and replaces it to change
the chat bubble width. Creates a backup before patching.

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

SEARCH_HEX = "8b05d75e9605"

OPTIONS = {
    '1': ('800px',  'B82003000090'),
    '2': ('1000px', 'B8E803000090'),
    '3': ('1200px', 'B8004B000090'),
    '4': ('1500px', 'B8DC05000090'),
    '5': ('2000px', 'B8D007000090'),
}

FALLBACK_OFFSET = 0x2F48BFB
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


def locate_patch_target(data: bytes):
    """Find the patch location(s) in the binary. Tries in order:

    1. Original hex pattern
    2. Previously-applied replacement patterns
    3. Returns (None, None, []) if nothing found
    """
    # 1. Try original pattern
    positions = find_all_occurrences(data, bytes.fromhex(SEARCH_HEX))
    if positions:
        return SEARCH_HEX, "original", positions

    # 2. Try each replacement pattern (already patched before)
    for key, (desc, hex_str) in OPTIONS.items():
        positions = find_all_occurrences(data, bytes.fromhex(hex_str))
        if positions:
            return hex_str, desc, positions

    return None, None, []


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

    found_hex, found_desc, positions = locate_patch_target(data)

    if positions:
        if found_desc == "original":
            print(f"\nOriginal pattern found at {len(positions)} location(s):")
        else:
            print(f"\nFile was previously patched with: {found_desc}")
            print(f"Found at {len(positions)} location(s):")

        for p in positions:
            print(f"  Offset: 0x{p:X}")
    else:
        print(f"\nOriginal pattern '{SEARCH_HEX}' not found.")
        print("Searching for previously-applied patches...")
        print("No known patch pattern found either.")

        if FALLBACK_OFFSET + 5 >= len(data):
            print(f"\nERROR: Fallback offset 0x{FALLBACK_OFFSET:X} is out of range.")
            print(f"File size: {len(data)} bytes (0x{len(data):X})")
            sys.exit(1)

        current_bytes = data[FALLBACK_OFFSET:FALLBACK_OFFSET + 6]
        print(f"\nFalling back to hardcoded file offset: 0x{FALLBACK_OFFSET:X}")
        print("WARNING: This offset may be incorrect if Telegram was updated.")
        print(f"Current bytes at that offset: {' '.join(f'{b:02X}' for b in current_bytes)}")

        proceed = input("\nContinue with this offset anyway? [y/N]: ").strip().lower()
        if proceed != 'y':
            print("No changes made.")
            return

        positions = [FALLBACK_OFFSET]
        found_hex = None
        found_desc = f"unknown (offset 0x{FALLBACK_OFFSET:X})"

    # Determine which option key matches the currently-found patch (if any)
    current_key = None
    for key, (desc, hex_str) in OPTIONS.items():
        if found_hex and found_hex.upper() == hex_str.upper():
            current_key = key
            break

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
    replacement = bytes.fromhex(hex_str)

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

    # Apply replacements (same length: 6 bytes)
    new_data = data
    for pos in reversed(positions):
        new_data = new_data[:pos] + replacement + new_data[pos + 6:]

    try:
        with open(exe_path, 'wb') as f:
            f.write(new_data)
    except PermissionError:
        print("ERROR: Cannot write. Make sure Telegram is NOT running.")
        sys.exit(1)

    print(f"\nDone. Patched {len(positions)} occurrence(s) with {desc} width.")
    print("Restart Telegram to see the changes.")


if __name__ == '__main__':
    main()
