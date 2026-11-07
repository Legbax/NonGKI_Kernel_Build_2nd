#!/usr/bin/env python3
# Extends the self-contained OmniShield per-uid clock offset (omni_uptime_inject.py,
# magic-clockid OMNI_UPTIME_MAGIC in kernel/time/posix-timers.c) to the 3 NON-syscall
# uptime surfaces the Snap login member ALSO cross-reads: /proc/uptime, sysinfo(),
# /proc/stat btime. Coverage of clock_gettime alone (A) diverges on those -> rc16.
# Adds an exported accessor omni_get_boot_off_current() (per-uid BOOTTIME delta) inside
# the omni block, and injects a call in each of the 3 surfaces. 4.14 / pre-timens.
# Run AFTER omni_uptime_inject.py. Usage: omni_uptime_surfaces_inject.py <kernel_root>
import sys, os

root = sys.argv[1] if len(sys.argv) > 1 else '.'

def rd(p):
    return open(os.path.join(root, p), encoding='utf-8').read()

def wr(p, s):
    open(os.path.join(root, p), 'w', encoding='utf-8', newline='\n').write(s)
    print('[omni-surfaces] patched', p)

# 1) export a per-uid BOOTTIME-delta accessor inside the omni block (posix-timers.c)
pt = 'kernel/time/posix-timers.c'
s = rd(pt)
MARK = '/* ===== end OmniShield PR-SealUptime ===== */'
if MARK not in s:
    sys.exit('[omni-surfaces] end marker not in posix-timers.c (run omni_uptime_inject.py first)')
if 'omni_get_boot_off_current' not in s:
    HELPER = (
        's64 omni_get_boot_off_current(void)\n'
        '{\n'
        '\tuid_t uid;\n'
        '\ts64 d = 0;\n'
        '\tint i;\n'
        '\n'
        '\tif (!omni_uptime_active)\n'
        '\t\treturn 0;\n'
        '\tuid = current_uid().val;\n'
        '\tspin_lock(&omni_uptime_lock);\n'
        '\tfor (i = 0; i < OMNI_UPTIME_MAX; i++) {\n'
        '\t\tif (omni_uptime_tbl[i].used && omni_uptime_tbl[i].uid == uid) {\n'
        '\t\t\td = omni_uptime_tbl[i].dboot;\n'
        '\t\t\tbreak;\n'
        '\t\t}\n'
        '\t}\n'
        '\tspin_unlock(&omni_uptime_lock);\n'
        '\treturn d;\n'
        '}\n'
        '\n'
    )
    s = s.replace(MARK, HELPER + MARK, 1)
    wr(pt, s)
else:
    print('[omni-surfaces] accessor already present in posix-timers.c')

def inject(path, anchor, body):
    s = rd(path)
    if 'omni_get_boot_off_current' in s and anchor + body.splitlines()[1] in s:
        print('[omni-surfaces] already injected in', path); return
    if anchor not in s:
        sys.exit('[omni-surfaces] anchor not found in %s' % path)
    s = s.replace(anchor, anchor + body, 1)
    wr(path, s)

# 2) /proc/uptime : field1 is BOOTTIME -> +boot_off (ns)
inject('fs/proc/uptime.c',
       '\tget_monotonic_boottime(&uptime);\n',
       '\t{\n'
       '\t\textern s64 omni_get_boot_off_current(void);\n'
       '\t\ts64 _b = omni_get_boot_off_current();\n'
       '\t\tif (_b > 0)\n'
       '\t\t\ttimespec_add_ns(&uptime, (u64)_b);\n'
       '\t}\n')

# 3) sysinfo() : info->uptime (seconds) -> +boot_off
inject('kernel/sys.c',
       '\tinfo->uptime = tp.tv_sec + (tp.tv_nsec ? 1 : 0);\n',
       '\t{\n'
       '\t\textern s64 omni_get_boot_off_current(void);\n'
       '\t\ts64 _b = omni_get_boot_off_current();\n'
       '\t\tif (_b > 0)\n'
       '\t\t\tinfo->uptime += (long)(_b / NSEC_PER_SEC);\n'
       '\t}\n')

# 4) /proc/stat btime : wall-clock of boot -> -boot_off (larger uptime == earlier boot)
inject('fs/proc/stat.c',
       '\tgetboottime64(&boottime);\n',
       '\t{\n'
       '\t\textern s64 omni_get_boot_off_current(void);\n'
       '\t\ts64 _b = omni_get_boot_off_current();\n'
       '\t\tif (_b > 0)\n'
       '\t\t\tboottime.tv_sec -= (time64_t)(_b / NSEC_PER_SEC);\n'
       '\t}\n')

print('[omni-surfaces] /proc/uptime + sysinfo + /proc/stat btime injected OK')
