#!/usr/bin/env python3
# omni_uptime_randinit_inject.py  (PR-UptimeRandInit / offset-global v2, COHERENT duty-cycle)
#
# Awake-days uptime realism for the Snap login/signup seal, 4.14 arm64 (rosemary).
# Calco EXACTO del patch validado por Almohada en raven (gb-v2-mono-offset, oracle rc13),
# portado del anchor 6.1 (wall_to_mono = timespec64_sub(...)) al 4.14
# (set_normalized_timespec64(&tmp, -boot...); + tk_set_wall_to_mono(tk, tmp);).
#
# Two independent offsets so mono < boot with a realistic duty cycle (coherent with the
# deviceless refresh's virtual_clock, ~20-54% awake; a single mono=boot offset is a
# duty-cycle tell AND incoherent):
#   gb_age   = 259200 + (gb_r % 345600)                  # device age [3d,7d)  (low bits)
#   gb_awake = gb_age * (20 + ((gb_r / 345600) % 35)) / 100  # mono = [20,54]% (high bits -> independent of age)
#   gb_susp  = gb_age - gb_awake                          # suspend gap => boottime = age
# monotonic += gb_awake (via tmp); boottime via offs_boot += gb_susp. eventTime follows
# MONOTONIC (awake) => recency ~0 => grant. random_get_entropy() = arch-timer CNTVCT,
# varies per boot => seals de-correlate. realtime/xtime untouched (date intact).
#
# Three insertions in timekeeping_init() (match Almohada's 3 perl injections):
#   (A) decls in the decl block (4.14 = -Werror=declaration-after-statement):
#         u64 gb_r; long gb_age, gb_awake, gb_susp;
#   (B) after set_normalized_timespec64(&tmp, -boot.tv_sec, -boot.tv_nsec):
#         gb_r = random_get_entropy(); gb_age = ...; gb_awake = ...; gb_susp = ...;
#         tmp.tv_sec += gb_awake;
#   (C) after tk_set_wall_to_mono(tk, tmp):
#         tk->offs_boot = ktime_add(tk->offs_boot, ktime_set(gb_susp, 0));
#
# 4.14 verified: tk->offs_boot exists (ktime_t); tk_set_wall_to_mono does NOT touch
# offs_boot; its WARN_ON_ONCE checks the OLD wall_to_monotonic (0 at init, no trip);
# tk_update_ktime_data does not recompute offs_boot. random_get_entropy() available via
# <linux/timex.h> (already included) => get_cycles() => arch_counter_get_cntvct() (CNTVCT).
#
# Usage: python3 omni_uptime_randinit_inject.py kernel/time/timekeeping.c
# Exits nonzero if an anchor is missing/duplicated or already patched (fail loudly).

import re, sys

MARK = "OMNI-RandInit"

DECL = re.compile(
    r'^([ \t]*)struct timespec64 now, boot, tmp;[ \t]*$',
    re.MULTILINE,
)
BODY = re.compile(
    r'^([ \t]*)set_normalized_timespec64\(&tmp,[ \t]*'
    r'-boot\.tv_sec,[ \t]*-boot\.tv_nsec\);[ \t]*$',
    re.MULTILINE,
)
BOOTOFF = re.compile(
    r'^([ \t]*)tk_set_wall_to_mono\(tk, tmp\);[ \t]*$',
    re.MULTILINE,
)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: omni_uptime_randinit_inject.py <path/to/timekeeping.c>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        src = f.read()

    if MARK in src:
        print(f"[{MARK}] already injected in {path} -- refusing to double-patch", file=sys.stderr)
        return 1

    for name, rx in (("decl", DECL), ("body", BODY), ("bootoff", BOOTOFF)):
        n = len(rx.findall(src))
        if n != 1:
            print(f"[{MARK}] expected exactly 1 {name} anchor in {path}, found {n}", file=sys.stderr)
            return 1

    def decl_repl(m: "re.Match") -> str:
        ind = m.group(1)
        return f"{m.group(0)}\n{ind}u64 gb_r; long gb_age, gb_awake, gb_susp;  /* {MARK} */"

    def body_repl(m: "re.Match") -> str:
        ind = m.group(1)
        return (
            f"{m.group(0)}\n"
            f"{ind}/* {MARK}: coherent awake-days uptime for the login seal. monotonic ="
            f" awake\n"
            f"{ind} * [20,54]% of a random [3d,7d) device age; boottime = full age via a"
            f" suspend\n"
            f"{ind} * gap in offs_boot (below). eventTime follows MONOTONIC => recency ~0 =>"
            f" grant;\n"
            f"{ind} * random per boot => seals de-correlate; realtime/xtime intact. */\n"
            f"{ind}gb_r = random_get_entropy();\n"
            f"{ind}gb_age = 259200 + (gb_r % 345600);\n"
            f"{ind}gb_awake = gb_age * (20 + ((gb_r / 345600) % 35)) / 100;\n"
            f"{ind}gb_susp = gb_age - gb_awake;\n"
            f"{ind}tmp.tv_sec += gb_awake;"
        )

    def bootoff_repl(m: "re.Match") -> str:
        ind = m.group(1)
        return (
            f"{m.group(0)}\n"
            f"{ind}/* {MARK}: suspend gap => CLOCK_BOOTTIME = gb_age (device age) while"
            f" monotonic stays awake-only. */\n"
            f"{ind}tk->offs_boot = ktime_add(tk->offs_boot, ktime_set(gb_susp, 0));"
        )

    new = DECL.sub(decl_repl, src, count=1)
    new = BODY.sub(body_repl, new, count=1)
    new = BOOTOFF.sub(bootoff_repl, new, count=1)
    if new == src:
        print(f"[{MARK}] substitution produced no change", file=sys.stderr)
        return 1

    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(new)
    print(f"[{MARK}] injected coherent 2-offset random-at-init uptime into {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
