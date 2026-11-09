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
# Fix (confirmed live on the 5TG): ksu_susfs add_sus_kstat_statically stores the
# REAL ino (it ignores the <ino> arg, storing the path-resolved value) while
# honoring mtime/ctime — so there is NO distinct spoofed_ino to key on. Derive a
# de-correlated ino from the per-account spoofed mtime (+ the real ino so data and
# obb differ) for any sus_kstat entry that spoofs mtime; on the fleet those are only
# the .nomedia entries. A distinct spoofed_ino / the KSTAT_SPOOF_INO flag is still
# honored first if a binary ever sets it.
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
            f"{ind}if (entry->info.flags & KSTAT_SPOOF_INO) /* {MARK} */\n"
            f"{ind}\tstat->ino = entry->info.spoofed_ino;\n"
            f"{ind}else if (entry->info.spoofed_ino && entry->info.spoofed_ino != target_ino)\n"
            f"{ind}\tstat->ino = entry->info.spoofed_ino;\n"
            f"{ind}else if (entry->info.flags & KSTAT_SPOOF_MTIME_TV_SEC) /* {MARK}:"
            f" ksu_susfs add_sus_kstat_statically omits the ino (stores the real one), so derive a"
            f" de-correlated ino from the per-account spoofed mtime (+ real ino so data/obb differ);"
            f" kills the shared-inode device-reputation correlator (SS06) on sdcardfs */\n"
            f"{ind}\tstat->ino = 200000UL + (((unsigned long)entry->info.spoofed_mtime_tv_sec"
            f" + target_ino) % 700000UL);"
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
