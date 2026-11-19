#!/usr/bin/env python3
# omni_uptime_v4_inject.py  (OMNI-v4 — uniform offset + idle coherence, 4.14 arm64 rosemary)
#
# Login/signup-seal uptime realism, FULLY COHERENT across EVERY uptime surface the Snap
# login member can read. Port of the raven (GKI 6.1, gb-v3-mono-offset, commit 74fb550,
# oracle rc13) "v4" design to the 4.14 Redmi Note 10S (rosemary) kernel.
#
# SUPERSEDES omni_uptime_randinit_inject.py ("v2"). That v2 SPLIT the offset:
#   tmp.tv_sec   += gb_awake            (CLOCK_MONOTONIC, reaches the vDSO)
#   tk->offs_boot += ktime(gb_susp)     (a BOOTTIME "suspend gap", KERNEL path only)
# but offs_boot does NOT reach the arm64 vDSO, so an app's clock_gettime(CLOCK_BOOTTIME)
# stayed at the MONOTONIC value (gb_awake) while /proc/uptime (kernel get_monotonic_boottime,
# which DOES add offs_boot) read gb_age -> two reads of the SAME measurement disagree = the
# exact internal contradiction the server flags (delayed SS06). v2 also left MONOTONIC_RAW
# leaking the real ~minutes uptime, and /proc/uptime col2 (idle) + /proc/stat idle leaking
# the real ~0 idle (idle/uptime ~= 0 on a "days-old" device = a fresh-boot tell).
#
# v4 = a SINGLE per-boot source of truth, uniform across every surface:
#   gb_age      (local)  = 259200 + (gb_r % 345600)                      # [3d,7d) s
#                          += tmp.tv_sec (wall_to_monotonic) -> CLOCK_MONOTONIC == CLOCK_BOOTTIME
#                          (offs_boot stays 0) == /proc/uptime col1 == sysinfo.uptime, via
#                          BOTH the vDSO and the syscall; AND += tk->raw_sec -> MONOTONIC_RAW.
#                          REALTIME/xtime UNTOUCHED -> /proc/stat btime (= now - uptime) is
#                          AUTOMATICALLY coherent (btime moves back by gb_age; btime+uptime=now).
#   gb_idle_age (GLOBAL) = gb_age * [2.30,2.79)  (Pixel-9 ground-truth idle/uptime ~2.56),
#                          file-scope so /proc/uptime.c col2 AND /proc/stat.c read the SAME value:
#                            fs/proc/uptime.c col2 (idle.tv_sec)       += gb_idle_age
#                            fs/proc/stat.c  aggregate "cpu" idle (ns) += gb_idle_age*NSEC_PER_SEC
#                            fs/proc/stat.c  per-cpu "cpuN" idle (ns)  += that / num_online_cpus()
#                          (per-cpu distributes the SAME total so sum(cpuN)==aggregate; an
#                          aggregate-only inflation is itself a tell). gb_r high bits drive the
#                          idle multiplier -> de-correlated from gb_age.
#
# NO offs_boot split (removed). random_get_entropy() = arch-timer CNTVCT, varies per boot ->
# seals de-correlate. gb_age magnitude unchanged from v2 -> the mint banks /proc/uptime col1
# as account u_login and the deviceless refresh/COF continuity is preserved.
#
# 4.14 anchors (verified against gabutgadungan/android_kernel_xiaomi_rosemary@rosemary-13,
# 4.14.186-perf): this kernel carries the raw_sec backport (tk->raw_sec served to
# MONOTONIC_RAW via getrawmonotonic64; tk->raw_sec = 0 in timekeeping_init), so the raven
# `tk->raw_sec += gb_age` applies verbatim. gb_age is computed at the `tk->raw_sec = 0;`
# anchor (which precedes the wall_to_monotonic setup) and reused at the set_normalized_timespec64
# body anchor. /proc/uptime.c idle var is `nsec` (not `idle_nsec`); /proc/stat.c uses
# get_idle_time(i) and nsec_to_clock_t().
#
# NOTE on the per-uid A/B layers (omni_uptime_inject.py magic-clockid + omni_uptime_surfaces_inject.py):
# they stay compiled but INERT (omni_uptime_active=false / omni_get_boot_off_current()==0 unless
# a magic-clockid registration arms them), so on this always-on global path they add nothing and
# do NOT double-count. The rosemary mint must rely on THIS global offset and MUST NOT issue the
# OMNI_UPTIME_MAGIC registration (the raven "GB_SKIP_CLOCK" rule) or the surfaces would double.
#
# Usage: python3 omni_uptime_v4_inject.py <kernel_root>
# Exits nonzero if any anchor is missing/duplicated or already patched (fail loudly).

import re
import sys
import os

MARK = "OMNI-v4"


def rd(root, p):
    with open(os.path.join(root, p), "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def wr(root, p, s):
    with open(os.path.join(root, p), "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(s)
    print("[%s] patched %s" % (MARK, p))


def sub_once(name, rx, repl, src, path):
    n = len(rx.findall(src))
    if n != 1:
        sys.exit("[%s] expected exactly 1 %s anchor in %s, found %d" % (MARK, name, path, n))
    return rx.sub(repl, src, count=1)


# ---- kernel/time/timekeeping.c ---------------------------------------------
TK = "kernel/time/timekeeping.c"

TK_FN = re.compile(r'(void __init timekeeping_init\(void\)\n\{)')
TK_DECL = re.compile(
    r'^([ \t]*)struct timespec64 now, boot, tmp;[ \t]*$',
    re.MULTILINE,
)
TK_RAW = re.compile(
    r'^([ \t]*)tk->raw_sec = 0;[ \t]*$',
    re.MULTILINE,
)
TK_BODY = re.compile(
    r'^([ \t]*)set_normalized_timespec64\(&tmp,[ \t]*'
    r'-boot\.tv_sec,[ \t]*-boot\.tv_nsec\);[ \t]*$',
    re.MULTILINE,
)


def patch_timekeeping(root):
    path = TK
    src = rd(root, path)
    if MARK in src:
        sys.exit("[%s] already injected in %s -- refusing to double-patch" % (MARK, path))

    # file-scope global = single source of truth for the idle offset (readers extern it):
    def fn_repl(m):
        return "long gb_idle_age;  /* %s */\n%s" % (MARK, m.group(1))
    src = sub_once("fn", TK_FN, fn_repl, src, path)

    # locals (gb_age stays a proven local, as v2):
    def decl_repl(m):
        return "%s\n%su64 gb_r; long gb_age;  /* %s */" % (m.group(0), m.group(1), MARK)
    src = sub_once("decl", TK_DECL, decl_repl, src, path)

    # compute per-boot gb_age + gb_idle_age at the raw_sec anchor (precedes the body anchor),
    # and inflate MONOTONIC_RAW there (tk->raw_sec = gb_age):
    def raw_repl(m):
        ind = m.group(1)
        return (
            "%s\n"
            "%s/* %s: single-source uniform uptime offset [3d,7d), coherent across EVERY surface\n"
            "%s * the login member reads. gb_age -> MONOTONIC==BOOTTIME==MONOTONIC_RAW==/proc/uptime\n"
            "%s * col1==sysinfo (vDSO + syscall); NO offs_boot split (the v2 bug: offs_boot never\n"
            "%s * reached the vDSO, so clock_gettime(BOOTTIME) disagreed with /proc/uptime). gb_idle_age\n"
            "%s * (~2.5x age) -> /proc/uptime col2 + /proc/stat idle (realistic idle/uptime ratio).\n"
            "%s * realtime/xtime untouched => btime auto-coherent (= now - gb_age); random per boot\n"
            "%s * => seals de-correlate. */\n"
            "%sgb_r = random_get_entropy();\n"
            "%sgb_age = 259200 + (gb_r %% 345600);\n"
            "%sgb_idle_age = (gb_age * (230 + (long)((gb_r >> 20) %% 50))) / 100;\n"
            "%stk->raw_sec += gb_age;"
            % (m.group(0), ind, MARK, ind, ind, ind, ind, ind, ind, ind, ind, ind, ind)
        )
    src = sub_once("raw", TK_RAW, raw_repl, src, path)

    # inflate MONOTONIC/BOOTTIME base uniformly via wall_to_monotonic (tmp):
    def body_repl(m):
        ind = m.group(1)
        return "%s\n%stmp.tv_sec += gb_age;  /* %s: uniform monotonic/boottime offset */" % (m.group(0), ind, MARK)
    src = sub_once("body", TK_BODY, body_repl, src, path)

    wr(root, path, src)


# ---- fs/proc/uptime.c (col2 idle) ------------------------------------------
UP = "fs/proc/uptime.c"
UP_IDLE = re.compile(
    r'^([ \t]*)idle\.tv_sec = div_u64_rem\(nsec, NSEC_PER_SEC, &rem\);[ \t]*$',
    re.MULTILINE,
)


def patch_uptime(root):
    path = UP
    src = rd(root, path)
    if MARK in src:
        sys.exit("[%s] already injected in %s" % (MARK, path))

    def idle_repl(m):
        ind = m.group(1)
        return "%s\n%s{ extern long gb_idle_age; if (gb_idle_age) idle.tv_sec += gb_idle_age; }  /* %s */" % (m.group(0), ind, MARK)
    src = sub_once("uptime-idle", UP_IDLE, idle_repl, src, path)
    wr(root, path, src)


# ---- fs/proc/stat.c (aggregate + per-cpu idle) -----------------------------
ST = "fs/proc/stat.c"
ST_AGG = re.compile(
    r'^([ \t]*)seq_put_decimal_ull\(p, "cpu  ", nsec_to_clock_t\(user\)\);[ \t]*$',
    re.MULTILINE,
)
ST_PERCPU = re.compile(
    r'^([ \t]*)idle = get_idle_time\(i\);[ \t]*$',
    re.MULTILINE,
)


def patch_stat(root):
    path = ST
    src = rd(root, path)
    if MARK in src:
        sys.exit("[%s] already injected in %s" % (MARK, path))

    # aggregate "cpu" idle (ns) — keep it == /proc/uptime col2 (prepend before the emit):
    def agg_repl(m):
        ind = m.group(1)
        return "%s{ extern long gb_idle_age; if (gb_idle_age) idle += (u64)gb_idle_age * NSEC_PER_SEC; }  /* %s */\n%s" % (ind, MARK, m.group(0))
    src = sub_once("stat-agg", ST_AGG, agg_repl, src, path)

    # per-cpu "cpuN" idle — distribute the SAME total so sum(cpuN idle)==cpu aggregate
    # (a real non-hotplugged device has them equal; an aggregate-only inflation would be a tell):
    def percpu_repl(m):
        ind = m.group(1)
        return "%s\n%s{ extern long gb_idle_age; if (gb_idle_age) idle += (u64)gb_idle_age * NSEC_PER_SEC / num_online_cpus(); }  /* %s */" % (m.group(0), ind, MARK)
    src = sub_once("stat-percpu", ST_PERCPU, percpu_repl, src, path)
    wr(root, path, src)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    patch_timekeeping(root)
    patch_uptime(root)
    patch_stat(root)
    print("[%s] coherent: gb_age[3d,7d) -> MONO/BOOT/RAW/uptime-col1/sysinfo; "
          "gb_idle_age(~2.5x) -> /proc/uptime col2 + /proc/stat idle (aggregate + per-cpu); "
          "btime auto-coherent" % MARK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
