#!/usr/bin/env python3
# Anchor-based injector for the self-contained OmniShield per-uid clock offset.
# Modifies ONLY kernel/time/posix-timers.c. Zero filesystem footprint:
# registration rides on a magic clockid in clock_gettime (root-only), no /proc.
import sys

path = sys.argv[1]
src = open(path, encoding='utf-8').read()

BLOCK = r'''/* ===== OmniShield PR-SealUptime: self-contained per-uid clock offset =====
 * Adds a per-uid delta to CLOCK_MONOTONIC*/CLOCK_BOOTTIME results of the
 * clock_gettime(2) SYSCALL path only (raw svc). The vDSO fast path never
 * reaches here, so Choreographer/vsync and normal app timing are unaffected.
 * Registration is footprint-free: a root caller issues
 *   clock_gettime(OMNI_UPTIME_MAGIC, &v)  with v = {uid, dmono_ns, dboot_ns}
 * which is intercepted below. No /proc, /sys or any persistent artifact.
 */
#include <linux/uaccess.h>
#include <linux/cred.h>
#include <linux/spinlock.h>

#define OMNI_UPTIME_MAGIC  0x6F6D6E69   /* 'omni' — never a real clockid */
#define OMNI_UPTIME_MAX    8
struct omni_uptime_entry { uid_t uid; s64 dmono; s64 dboot; bool used; };
static struct omni_uptime_entry omni_uptime_tbl[OMNI_UPTIME_MAX];
static DEFINE_SPINLOCK(omni_uptime_lock);
static bool omni_uptime_active;

/* Intercept the magic clockid as a registration channel. Returns true when the
 * call was the magic one (caller must then return 0 and not treat it as a real
 * clock_gettime). Only root can register; non-root magic calls are swallowed. */
static bool omni_uptime_try_register(const clockid_t which_clock,
				     struct timespec __user *utp)
{
	long long v[3];
	int i, slot = -1;

	if (which_clock != OMNI_UPTIME_MAGIC)
		return false;
	if (current_uid().val != 0)
		return true;
	if (!utp || copy_from_user(v, utp, sizeof(v)))
		return true;

	spin_lock(&omni_uptime_lock);
	for (i = 0; i < OMNI_UPTIME_MAX; i++)
		if (omni_uptime_tbl[i].used && omni_uptime_tbl[i].uid == (uid_t)v[0]) {
			slot = i;
			break;
		}
	if (slot < 0)
		for (i = 0; i < OMNI_UPTIME_MAX; i++)
			if (!omni_uptime_tbl[i].used) {
				slot = i;
				break;
			}
	if (slot >= 0) {
		omni_uptime_tbl[slot].uid = (uid_t)v[0];
		omni_uptime_tbl[slot].dmono = v[1];
		omni_uptime_tbl[slot].dboot = v[2];
		omni_uptime_tbl[slot].used = true;
		omni_uptime_active = true;
	}
	spin_unlock(&omni_uptime_lock);
	return true;
}

void omni_apply_clock_offset(const clockid_t which_clock, struct timespec64 *tp)
{
	uid_t uid;
	s64 d = 0;
	int i;

	if (!omni_uptime_active)
		return;
	uid = current_uid().val;
	spin_lock(&omni_uptime_lock);
	for (i = 0; i < OMNI_UPTIME_MAX; i++) {
		if (omni_uptime_tbl[i].used && omni_uptime_tbl[i].uid == uid) {
			if (which_clock == CLOCK_BOOTTIME ||
			    which_clock == CLOCK_BOOTTIME_ALARM)
				d = omni_uptime_tbl[i].dboot;
			else if (which_clock == CLOCK_MONOTONIC ||
				 which_clock == CLOCK_MONOTONIC_RAW ||
				 which_clock == CLOCK_MONOTONIC_COARSE)
				d = omni_uptime_tbl[i].dmono;
			break;
		}
	}
	spin_unlock(&omni_uptime_lock);
	if (d > 0)
		timespec64_add_ns(tp, (u64)d);
}
/* ===== end OmniShield PR-SealUptime ===== */

'''

a1 = 'SYSCALL_DEFINE2(clock_gettime, const clockid_t, which_clock,'
if a1 not in src:
    sys.exit('ANCHOR1 (clock_gettime syscall) not found')
src = src.replace(a1, BLOCK + a1, 1)

# registration hook: after the clock_gettime native declarations, before if(!kc)
a_reg = ('\tconst struct k_clock *kc = clockid_to_kclock(which_clock);\n'
         '\tstruct timespec64 kernel_tp;\n'
         '\tint error;')
if a_reg not in src:
    sys.exit('ANCHOR_REG (clock_gettime decls) not found')
src = src.replace(a_reg, a_reg + '\n\n\tif (omni_uptime_try_register(which_clock, tp))\n\t\treturn 0;', 1)

# offset hook: native
a2 = 'error = kc->clock_get(which_clock, &kernel_tp);'
if a2 not in src:
    sys.exit('ANCHOR2 (native clock_get) not found')
src = src.replace(a2, a2 + '\n\tif (!error)\n\t\tomni_apply_clock_offset(which_clock, &kernel_tp);', 1)

# offset hook: compat
a3 = 'err = kc->clock_get(which_clock, &ts);'
if a3 in src:
    src = src.replace(a3, a3 + '\n\tif (!err)\n\t\tomni_apply_clock_offset(which_clock, &ts);', 1)

open(path, 'w', encoding='utf-8', newline='\n').write(src)
print('omni_uptime (magic-clockid, zero-footprint) injected into %s' % path)
