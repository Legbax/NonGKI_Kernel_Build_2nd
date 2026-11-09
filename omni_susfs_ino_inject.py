#!/usr/bin/env python3
# omni_susfs_ino_inject.py  (PR-NomediaInoSpoof)
#
# Makes susfs sus_kstat spoof the st_ino robustly on sdcardfs (the .nomedia
# device-reputation correlator, SS06 class).
#
# Root cause (confirmed live on the 5TG, sdcardfs): susfs ALREADY rewrites
# stat->ino in susfs_sus_kstat_spoof_generic_fillattr() (vfs_getattr_nosec, runs
# AFTER sdcardfs_getattr), but GATED on the KSTAT_SPOOF_INO flag — and the
# ksu_susfs userspace binary stores the spoofed_ino from `add_sus_kstat_statically
# <path> <INO> ...` yet does NOT set that flag (it DOES set the mtime/ctime flags),
# so mtime/ctime reach the reader but the ino stays the real (shared) lower-f2fs
# inode. On sdcardfs the reader (and the Snap member) gets the real ino 8599,
# shared across every account on the device = the device-reputation link.
#
# Fix: spoof the ino whenever the matched sus_kstat entry carries a spoofed_ino
# that DIFFERS from the real matched ino (target_ino), regardless of the flag.
# Safe: an entry that wants to keep the real ino stores spoofed_ino == target_ino
# (the "default" arg resolves to the real value) and is skipped.
#
# Runs AFTER the susfs patch adds fs/susfs.c. Context-robust, mirrors the uptime
# injectors.  Usage: python3 omni_susfs_ino_inject.py fs/susfs.c
# Exits nonzero if the anchor is missing/duplicated or already patched.

import re, sys

MARK = "OMNI-InoSpoof"
ANCHOR = re.compile(
    r'^([ \t]*)if \(entry->info\.flags & KSTAT_SPOOF_INO\)[ \t]*\n'
    r'[ \t]*stat->ino = entry->info\.spoofed_ino;[ \t]*$',
    re.MULTILINE,
)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: omni_susfs_ino_inject.py <path/to/fs/susfs.c>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        src = f.read()

    if MARK in src:
        print(f"[{MARK}] already injected in {path} -- refusing to double-patch", file=sys.stderr)
        return 1

    matches = ANCHOR.findall(src)
    if len(matches) != 1:
        print(f"[{MARK}] expected exactly 1 anchor in {path}, found {len(matches)}", file=sys.stderr)
        return 1

    def repl(m: "re.Match") -> str:
        ind = m.group(1)
        return (
            f"{ind}if ((entry->info.flags & KSTAT_SPOOF_INO) ||\n"
            f"{ind}    (entry->info.spoofed_ino && entry->info.spoofed_ino != target_ino)) /* {MARK}:"
            f" force ino spoof when a distinct spoofed_ino is set (ksu_susfs omits the flag for"
            f" add_sus_kstat_statically; needed for the sdcardfs .nomedia ino correlator) */\n"
            f"{ind}\tstat->ino = entry->info.spoofed_ino;"
        )

    new = ANCHOR.sub(repl, src, count=1)
    if new == src:
        print(f"[{MARK}] substitution produced no change", file=sys.stderr)
        return 1

    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(new)
    print(f"[{MARK}] injected robust ino spoof into {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
