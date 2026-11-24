#!/usr/bin/env python3
# Hand-port omni_clock_offset into merlin's bakasu + SUSFS-2.2.00 layout.
# `git apply --recount` already lands kernel/time/posix-timers.c + kernel/Kconfig;
# this script adds the 4 pieces that reject due to context/path skew:
#   1) include/linux/susfs_def.h   -> CMD_SUSFS_ADD_CLOCK_OFFSET define
#   2) include/linux/susfs.h       -> structs + function decls
#   3) fs/susfs.c                  -> hashtable + add/apply functions
#   4) drivers/kernelsu/core_hook.c-> CMD dispatch (bakasu path, not kernel/core_hook.c)
# Idempotent: skips any piece already present. Run from the kernel source root.
import os, sys

def read(p):
    with open(p, "r") as f:
        return f.read()

def write(p, s):
    with open(p, "w") as f:
        f.write(s)

errs = []

# 1) susfs_def.h
p = "include/linux/susfs_def.h"
s = read(p)
if "CMD_SUSFS_ADD_CLOCK_OFFSET" not in s:
    lines = s.split("\n")
    idxs = [i for i, l in enumerate(lines) if l.startswith("#define CMD_SUSFS_")]
    if not idxs:
        errs.append("susfs_def.h: no '#define CMD_SUSFS_' anchor")
    else:
        lines.insert(idxs[-1] + 1, "#define CMD_SUSFS_ADD_CLOCK_OFFSET 0x60010")
        write(p, "\n".join(lines))
        print("[+] susfs_def.h: added CMD_SUSFS_ADD_CLOCK_OFFSET after line", idxs[-1] + 1)
else:
    print("[=] susfs_def.h: already present")

# 2) susfs.h (structs + decls, before the last #endif = include-guard close)
p = "include/linux/susfs.h"
s = read(p)
if "st_susfs_clock_offset" not in s:
    block = (
        "\n/* clock_offset (merlin hand-port) */\n"
        "#ifdef CONFIG_KSU_SUSFS_CLOCK_OFFSET\n"
        "struct st_susfs_clock_offset {\n"
        "\tint                              target_uid;\n"
        "\tlong long                        delta_mono_ns;\n"
        "\tlong long                        delta_boot_ns;\n"
        "};\n"
        "struct st_susfs_clock_offset_hlist {\n"
        "\tint                              target_uid;\n"
        "\tlong long                        delta_mono_ns;\n"
        "\tlong long                        delta_boot_ns;\n"
        "\tstruct hlist_node                node;\n"
        "};\n"
        "int susfs_add_clock_offset(struct st_susfs_clock_offset* __user user_info);\n"
        "void susfs_apply_clock_offset(const clockid_t which_clock, struct timespec64 *tp);\n"
        "#endif\n"
    )
    lines = s.split("\n")
    endifs = [i for i, l in enumerate(lines) if l.strip().startswith("#endif")]
    if not endifs:
        errs.append("susfs.h: no #endif anchor")
    else:
        lines.insert(endifs[-1], block)
        write(p, "\n".join(lines))
        print("[+] susfs.h: inserted structs + decls before #endif at line", endifs[-1])
else:
    print("[=] susfs.h: already present")

# 3) fs/susfs.c (functions appended at EOF, inside #ifdef)
p = "fs/susfs.c"
s = read(p)
if "susfs_apply_clock_offset" not in s:
    block = (
        "\n/* clock_offset (merlin hand-port) */\n"
        "#ifdef CONFIG_KSU_SUSFS_CLOCK_OFFSET\n"
        "static DEFINE_HASHTABLE(CLOCK_OFFSET_HLIST, 4);\n"
        "static bool susfs_clock_offset_active = false;\n"
        "\n"
        "int susfs_add_clock_offset(struct st_susfs_clock_offset* __user user_info) {\n"
        "\tstruct st_susfs_clock_offset info;\n"
        "\tstruct st_susfs_clock_offset_hlist *new_entry, *tmp_entry;\n"
        "\tstruct hlist_node *tmp_node;\n"
        "\tint bkt;\n"
        "\n"
        "\tif (copy_from_user(&info, user_info, sizeof(info))) {\n"
        "\t\tSUSFS_LOGE(\"failed copying from userspace\\n\");\n"
        "\t\treturn 1;\n"
        "\t}\n"
        "\n"
        "\tspin_lock(&susfs_spin_lock);\n"
        "\thash_for_each_safe(CLOCK_OFFSET_HLIST, bkt, tmp_node, tmp_entry, node) {\n"
        "\t\tif (tmp_entry->target_uid == info.target_uid) {\n"
        "\t\t\thash_del(&tmp_entry->node);\n"
        "\t\t\tkfree(tmp_entry);\n"
        "\t\t\tbreak;\n"
        "\t\t}\n"
        "\t}\n"
        "\tspin_unlock(&susfs_spin_lock);\n"
        "\n"
        "\tnew_entry = kmalloc(sizeof(struct st_susfs_clock_offset_hlist), GFP_KERNEL);\n"
        "\tif (!new_entry) {\n"
        "\t\tSUSFS_LOGE(\"no enough memory\\n\");\n"
        "\t\treturn 1;\n"
        "\t}\n"
        "\tnew_entry->target_uid = info.target_uid;\n"
        "\tnew_entry->delta_mono_ns = info.delta_mono_ns;\n"
        "\tnew_entry->delta_boot_ns = info.delta_boot_ns;\n"
        "\n"
        "\tspin_lock(&susfs_spin_lock);\n"
        "\thash_add(CLOCK_OFFSET_HLIST, &new_entry->node, (unsigned int)info.target_uid);\n"
        "\tsusfs_clock_offset_active = true;\n"
        "\tspin_unlock(&susfs_spin_lock);\n"
        "\tSUSFS_LOGI(\"clock_offset added: uid=%d delta_mono=%lld delta_boot=%lld\\n\",\n"
        "\t\t\tinfo.target_uid, info.delta_mono_ns, info.delta_boot_ns);\n"
        "\treturn 0;\n"
        "}\n"
        "\n"
        "void susfs_apply_clock_offset(const clockid_t which_clock, struct timespec64 *tp) {\n"
        "\tstruct st_susfs_clock_offset_hlist *entry;\n"
        "\tuid_t uid;\n"
        "\tlong long d = 0;\n"
        "\n"
        "\tif (!susfs_clock_offset_active)\n"
        "\t\treturn;\n"
        "\tuid = current_uid().val;\n"
        "\n"
        "\tspin_lock(&susfs_spin_lock);\n"
        "\thash_for_each_possible(CLOCK_OFFSET_HLIST, entry, node, uid) {\n"
        "\t\tif (entry->target_uid == uid) {\n"
        "\t\t\tif (which_clock == CLOCK_BOOTTIME || which_clock == CLOCK_BOOTTIME_ALARM)\n"
        "\t\t\t\td = entry->delta_boot_ns;\n"
        "\t\t\telse if (which_clock == CLOCK_MONOTONIC ||\n"
        "\t\t\t         which_clock == CLOCK_MONOTONIC_RAW ||\n"
        "\t\t\t         which_clock == CLOCK_MONOTONIC_COARSE)\n"
        "\t\t\t\td = entry->delta_mono_ns;\n"
        "\t\t\tbreak;\n"
        "\t\t}\n"
        "\t}\n"
        "\tspin_unlock(&susfs_spin_lock);\n"
        "\n"
        "\tif (d > 0)\n"
        "\t\ttimespec64_add_ns(tp, (u64)d);\n"
        "}\n"
        "#endif\n"
    )
    write(p, s.rstrip("\n") + "\n" + block)
    print("[+] fs/susfs.c: appended clock_offset functions at EOF")
else:
    print("[=] fs/susfs.c: already present")

# 4) core_hook.c dispatch (bakasu puts it at drivers/kernelsu/core_hook.c)
cands = ["drivers/kernelsu/core_hook.c", "kernel/core_hook.c"]
p = next((c for c in cands if os.path.exists(c)), None)
if p is None:
    errs.append("core_hook.c not found in %s" % cands)
elif "CMD_SUSFS_ADD_CLOCK_OFFSET" in read(p):
    print("[=] %s: dispatch already present" % p)
else:
    s = read(p)
    block = (
        "#ifdef CONFIG_KSU_SUSFS_CLOCK_OFFSET\n"
        "\t\tif (arg2 == CMD_SUSFS_ADD_CLOCK_OFFSET) {\n"
        "\t\t\tint error = 0;\n"
        "\t\t\tif (!ksu_access_ok((void __user*)arg3, sizeof(struct st_susfs_clock_offset))) {\n"
        "\t\t\t\tpr_err(\"susfs: CMD_SUSFS_ADD_CLOCK_OFFSET -> arg3 is not accessible\\n\");\n"
        "\t\t\t\treturn 0;\n"
        "\t\t\t}\n"
        "\t\t\tif (!ksu_access_ok((void __user*)arg5, sizeof(error))) {\n"
        "\t\t\t\tpr_err(\"susfs: CMD_SUSFS_ADD_CLOCK_OFFSET -> arg5 is not accessible\\n\");\n"
        "\t\t\t\treturn 0;\n"
        "\t\t\t}\n"
        "\t\t\terror = susfs_add_clock_offset((struct st_susfs_clock_offset __user*)arg3);\n"
        "\t\t\tpr_info(\"susfs: CMD_SUSFS_ADD_CLOCK_OFFSET -> ret: %d\\n\", error);\n"
        "\t\t\tif (copy_to_user((void __user*)arg5, &error, sizeof(error)))\n"
        "\t\t\t\tpr_info(\"susfs: copy_to_user() failed\\n\");\n"
        "\t\t\treturn 0;\n"
        "\t\t}\n"
        "#endif //#ifdef CONFIG_KSU_SUSFS_CLOCK_OFFSET\n"
    )
    anchor = "#endif //#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT"
    anchor2 = "#ifdef CONFIG_KSU_SUSFS_SUS_SU"
    if anchor in s:
        s = s.replace(anchor, anchor + "\n" + block, 1)
        write(p, s)
        print("[+] %s: inserted dispatch after OPEN_REDIRECT #endif" % p)
    elif anchor2 in s:
        s = s.replace(anchor2, block + anchor2, 1)
        write(p, s)
        print("[+] %s: inserted dispatch before SUS_SU #ifdef (fallback anchor)" % p)
    else:
        errs.append("%s: no OPEN_REDIRECT/SUS_SU dispatch anchor; CMD handler NOT inserted" % p)

if errs:
    print("[!] clock_offset hand-port issues:")
    for e in errs:
        print("    - " + e)
    sys.exit(2)
print("[+] clock_offset hand-port complete")
