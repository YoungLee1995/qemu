/* SPDX-License-Identifier: MIT */
/* Host helpers for the current PCIe model; no firmware-private ready flags.
 * Compile with -D_POSIX_C_SOURCE=200809L. Caller owns mappings and locking.
 */
#ifndef ZC_HOST_H
#define ZC_HOST_H
#include <errno.h>
#include <stdint.h>
#include <time.h>

static inline void zc_host_fence(void) { __sync_synchronize(); }

/* regs points at one already-selected iATU region, not the BAR base.
 * Current model uses a 32-bit limit; ranges crossing 4 GiB need splitting.
 * Does not select a region or overwrite other inbound/outbound windows.
 */
static inline int zc_host_configure_atu(volatile uint32_t *regs,
                                      uint64_t base, uint64_t target,
                                      uint64_t bytes)
{
    if (!regs || !bytes || base > UINT64_MAX - (bytes - 1) ||
        target > UINT64_MAX - (bytes - 1) ||
        (base >> 32) != ((base + bytes - 1) >> 32))
        return -EINVAL;
    regs[1] = 0;
    zc_host_fence();
    regs[0] = 0;
    regs[2] = (uint32_t)base;
    regs[3] = base >> 32;
    regs[4] = (uint32_t)(base + bytes - 1);
    regs[5] = (uint32_t)target;
    regs[6] = target >> 32;
    zc_host_fence();
    regs[1] = 1u << 31;
    zc_host_fence();
    return 0;
}

/* IPC address, mask and expected value come from the actual BootROM/FW
 * contract. This helper defines no ready register, magic or download command.
 * timeout_ms == 0 performs one read, without waiting.
 */
static inline int zc_host_wait_ipc(volatile uint32_t *reg, uint32_t mask,
                                   uint32_t expected, unsigned timeout_ms)
{
    struct timespec start, now, interval = {0, 1000000};
    if (!reg || !mask || (expected & ~mask))
        return -EINVAL;
    if (clock_gettime(CLOCK_MONOTONIC, &start))
        return -errno;
    for (;;) {
        zc_host_fence();
        uint32_t value = *reg;
        zc_host_fence();
        if ((value & mask) == expected)
            return 0;
        if (clock_gettime(CLOCK_MONOTONIC, &now))
            return -errno;
        int64_t elapsed_ns = (int64_t)(now.tv_sec - start.tv_sec) * 1000000000 +
                             now.tv_nsec - start.tv_nsec;
        if (elapsed_ns / 1000000 >= timeout_ms)
            return -ETIMEDOUT;
        if (nanosleep(&interval, NULL) && errno != EINTR)
            return -errno;
    }
}
#endif
