#!/usr/bin/env python3
# Self-contained, layout-agnostic hand-port of omni_clock_offset into merlin's
# bakasu + SUSFS-2.2.00 tree. Discovers target files by CONTENT (os.walk) instead
# of assuming the resukisu paths the upstream patch expects. Idempotent.
# Pieces: susfs_def.h define, susfs.h structs+decls, fs/susfs.c funcs,
#         posix-timers.c syscall hook, KSU Kconfig entry, core_hook dispatch.
import os, sys

def read(p):
    with open(p, "r", errors="ignore") as f:
        return f.read()

def write(p, s):
    with open(p, "w") as f:
        f.write(s)

def walk_find(needle, name_suffix=None):
    """Return path of the first file (optionally name-filtered) whose text contains needle."""
    for dp, dn, fn in os.walk("."):
        if "/.git" in dp or dp.endswith("/.git"):
            continue
        for f in fn:
            if name_suffix and not f.endswith(name_suffix):
                continue
            fp = os.path.join(dp, f)
            try:
                if needle in read(fp):
                    return fp
            except Exception:
                pass
    return None

errs = []

# ---- 1) susfs_def.h: CMD define ----
p = "include/linux/susfs_def.h"
if os.path.exists(p):
    s = read(p)
    if "CMD_SUSFS_ADD_CLOCK_OFFSET" not in s:
        lines = s.split("\n")
        idxs = [i for i, l in enumerate(lines) if l.startswith("#define CMD_SUSFS_")]
        if idxs:
            lines.insert(idxs[-1] + 1, "#define CMD_SUSFS_ADD_CLOCK_OFFSET 0x60010")
            write(p, "\n".join(lines))
            print("[+] susfs_def.h: added define")
        else:
            errs.append("susfs_def.h: no CMD_SUSFS_ anchor")
    else:
        print("[=] susfs_def.h present")
else:
    errs.append("susfs_def.h not found")

# ---- 2) susfs.h: structs + decls before last #endif ----
p = "include/linux/susfs.h"
if os.path.exists(p):
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
        if endifs:
            lines.insert(endifs[-1], block)
            write(p, "\n".join(lines))
            print("[+] susfs.h: inserted structs + decls")
        else:
            errs.append("susfs.h: no #endif anchor")
    else:
        print("[=] susfs.h present")
else:
    errs.append("susfs.h not found")

# ---- 3) fs/susfs.c: functions at EOF ----
p = "fs/susfs.c"
if os.path.exists(p):
    s = read(p)
    if "susfs_apply_clock_offset" not in s:
        block = (
            "\n/* clock_offset (merlin hand-port) */\n"
            "#ifdef CONFIG_KSU_SUSFS_CLOCK_OFFSET\n"
            "static DEFINE_HASHTABLE(CLOCK_OFFSET_HLIST, 4);\n"
            "static bool susfs_clock_offset_active = false;\n"
            "static DEFINE_SPINLOCK(susfs_clock_offset_lock);\n\n"
            "int susfs_add_clock_offset(struct st_susfs_clock_offset* __user user_info) {\n"
            "\tstruct st_susfs_clock_offset info;\n"
            "\tstruct st_susfs_clock_offset_hlist *new_entry, *tmp_entry;\n"
            "\tstruct hlist_node *tmp_node;\n"
            "\tint bkt;\n\n"
            "\tif (copy_from_user(&info, user_info, sizeof(info))) {\n"
            "\t\tSUSFS_LOGE(\"failed copying from userspace\\n\");\n"
            "\t\treturn 1;\n"
            "\t}\n\n"
            "\tspin_lock(&susfs_clock_offset_lock);\n"
            "\thash_for_each_safe(CLOCK_OFFSET_HLIST, bkt, tmp_node, tmp_entry, node) {\n"
            "\t\tif (tmp_entry->target_uid == info.target_uid) {\n"
            "\t\t\thash_del(&tmp_entry->node);\n"
            "\t\t\tkfree(tmp_entry);\n"
            "\t\t\tbreak;\n"
            "\t\t}\n"
            "\t}\n"
            "\tspin_unlock(&susfs_clock_offset_lock);\n\n"
            "\tnew_entry = kmalloc(sizeof(struct st_susfs_clock_offset_hlist), GFP_KERNEL);\n"
            "\tif (!new_entry) {\n"
            "\t\tSUSFS_LOGE(\"no enough memory\\n\");\n"
            "\t\treturn 1;\n"
            "\t}\n"
            "\tnew_entry->target_uid = info.target_uid;\n"
            "\tnew_entry->delta_mono_ns = info.delta_mono_ns;\n"
            "\tnew_entry->delta_boot_ns = info.delta_boot_ns;\n\n"
            "\tspin_lock(&susfs_clock_offset_lock);\n"
            "\thash_add(CLOCK_OFFSET_HLIST, &new_entry->node, (unsigned int)info.target_uid);\n"
            "\tsusfs_clock_offset_active = true;\n"
            "\tspin_unlock(&susfs_clock_offset_lock);\n"
            "\tSUSFS_LOGI(\"clock_offset added: uid=%d delta_mono=%lld delta_boot=%lld\\n\",\n"
            "\t\t\tinfo.target_uid, info.delta_mono_ns, info.delta_boot_ns);\n"
            "\treturn 0;\n"
            "}\n\n"
            "void susfs_apply_clock_offset(const clockid_t which_clock, struct timespec64 *tp) {\n"
            "\tstruct st_susfs_clock_offset_hlist *entry;\n"
            "\tuid_t uid;\n"
            "\tlong long d = 0;\n\n"
            "\tif (!susfs_clock_offset_active)\n"
            "\t\treturn;\n"
            "\tuid = current_uid().val;\n\n"
            "\tspin_lock(&susfs_clock_offset_lock);\n"
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
            "\tspin_unlock(&susfs_clock_offset_lock);\n\n"
            "\tif (d > 0)\n"
            "\t\ttimespec64_add_ns(tp, (u64)d);\n"
            "}\n"
            "#endif\n"
        )
        write(p, s.rstrip("\n") + "\n" + block)
        print("[+] fs/susfs.c: appended functions")
    else:
        print("[=] fs/susfs.c present")
else:
    errs.append("fs/susfs.c not found")

# ---- 4) posix-timers.c: extern + 2 syscall hooks ----
p = "kernel/time/posix-timers.c"
if os.path.exists(p):
    s = read(p)
    if "susfs_apply_clock_offset" not in s:
        ext = ("#ifdef CONFIG_KSU_SUSFS_CLOCK_OFFSET\n"
               "extern void susfs_apply_clock_offset(const clockid_t which_clock, struct timespec64 *tp);\n"
               "#endif\n\n")
        # extern before the SYSCALL_DEFINE2(clock_gettime ...) definition
        anchor_syscall = "SYSCALL_DEFINE2(clock_gettime,"
        if anchor_syscall in s:
            s = s.replace(anchor_syscall, ext + anchor_syscall, 1)
        else:
            errs.append("posix-timers.c: no SYSCALL_DEFINE2(clock_gettime anchor for extern")
        # hook after the native clock_get into kernel_tp
        did = 0
        for a in ("error = kc->clock_get(which_clock, &kernel_tp);",):
            if a in s:
                s = s.replace(a, a + "\n#ifdef CONFIG_KSU_SUSFS_CLOCK_OFFSET\n\tif (!error)\n\t\tsusfs_apply_clock_offset(which_clock, &kernel_tp);\n#endif", 1)
                did += 1
        # hook after the compat clock_get into ts
        for a in ("err = kc->clock_get(which_clock, &ts);",):
            if a in s:
                s = s.replace(a, a + "\n#ifdef CONFIG_KSU_SUSFS_CLOCK_OFFSET\n\tif (!err)\n\t\tsusfs_apply_clock_offset(which_clock, &ts);\n#endif", 1)
                did += 1
        write(p, s)
        if did >= 1:
            print("[+] posix-timers.c: extern + %d syscall hook(s)" % did)
        else:
            errs.append("posix-timers.c: no clock_get() hook anchor matched")
    else:
        print("[=] posix-timers.c present")
else:
    errs.append("posix-timers.c not found")

# ---- 5) KSU Kconfig: add config next to KSU_SUSFS_OPEN_REDIRECT ----
kc = walk_find("config KSU_SUSFS_OPEN_REDIRECT", "Kconfig")
if kc is None:
    errs.append("Kconfig with KSU_SUSFS_OPEN_REDIRECT not found")
else:
    s = read(kc)
    if "KSU_SUSFS_CLOCK_OFFSET" not in s:
        cfg = (
            "config KSU_SUSFS_CLOCK_OFFSET\n"
            "    bool \"Per-uid CLOCK_MONOTONIC/BOOTTIME offset in the clock_gettime syscall path\"\n"
            "    depends on KSU_SUSFS\n"
            "    default y\n"
            "    help\n"
            "      - Adds a per-uid delta to CLOCK_MONOTONIC* and CLOCK_BOOTTIME results of the\n"
            "        clock_gettime(2) SYSCALL only (raw svc); the vDSO fast path is untouched.\n\n"
        )
        lines = s.split("\n")
        # insert before the next 'config KSU_SUSFS_' that appears AFTER OPEN_REDIRECT
        oidx = next((i for i, l in enumerate(lines) if l.strip() == "config KSU_SUSFS_OPEN_REDIRECT"), None)
        ins = None
        if oidx is not None:
            for j in range(oidx + 1, len(lines)):
                if lines[j].startswith("config KSU_SUSFS_"):
                    ins = j
                    break
        if ins is not None:
            lines.insert(ins, cfg)
            write(kc, "\n".join(lines))
            print("[+] %s: inserted KSU_SUSFS_CLOCK_OFFSET before next config (line %d)" % (kc, ins))
        else:
            # fallback: append after the OPEN_REDIRECT config's help block (end of file-ish)
            lines.insert(oidx + 1 if oidx is not None else len(lines), cfg)
            write(kc, "\n".join(lines))
            print("[+] %s: inserted KSU_SUSFS_CLOCK_OFFSET (fallback)" % kc)
    else:
        print("[=] %s: KSU_SUSFS_CLOCK_OFFSET present" % kc)

# ---- 6) core_hook dispatch: find the file with the SUSFS command handler ----
ch = walk_find("#endif //#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT", ".c")
if ch is None:
    ch = walk_find("CMD_SUSFS_SUS_SU", ".c")
if ch is None:
    errs.append("core_hook dispatch file not found (no OPEN_REDIRECT/SUS_SU handler)")
else:
    s = read(ch)
    if "CMD_SUSFS_ADD_CLOCK_OFFSET" in s:
        print("[=] %s: dispatch present" % ch)
    else:
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
        a1 = "#endif //#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT"
        a2 = "#ifdef CONFIG_KSU_SUSFS_SUS_SU"
        if a1 in s:
            s = s.replace(a1, a1 + "\n" + block, 1)
            write(ch, s)
            print("[+] %s: dispatch after OPEN_REDIRECT #endif" % ch)
        elif a2 in s:
            s = s.replace(a2, block + a2, 1)
            write(ch, s)
            print("[+] %s: dispatch before SUS_SU #ifdef (fallback)" % ch)
        else:
            errs.append("%s: no dispatch anchor" % ch)

if errs:
    print("[!] clock_offset hand-port issues:")
    for e in errs:
        print("    - " + e)
    sys.exit(2)
print("[+] clock_offset hand-port complete (all 6 pieces)")
