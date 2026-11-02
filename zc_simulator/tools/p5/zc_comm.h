/* SPDX-License-Identifier: MIT */
/* Simulator-owned Host communication queue over the supplied HDMA registers.
 * Header-only, no new firmware/PCIe register ABI. Caller owns buffer lifetime.
 */
#ifndef ZC_COMM_H
#define ZC_COMM_H
#include <stdint.h>
#include <stddef.h>
#define ZC_QUEUE_DEPTH 8
#define ZC_PENDING 0
#define ZC_COMPLETE 1
#define ZC_ABORT -1
#define ZC_FULL -2
struct zc_request {
    uint64_t source, destination, sequence;
    uint32_t bytes;
    int state;
};
struct zc_queue {
    volatile uint32_t *regs;
    struct zc_request ring[ZC_QUEUE_DEPTH];
    unsigned head, tail, count, running;
    uint64_t sequence;
};
static inline void zc_fence(void) { __sync_synchronize(); }
static inline void zc_queue_init(struct zc_queue *q, volatile uint32_t *regs)
{
    *q = (struct zc_queue){.regs = regs};
}
static inline int zc_submit(struct zc_queue *q, uint64_t src, uint64_t dst,
                            uint32_t bytes, struct zc_request **out)
{
    if (q->count == ZC_QUEUE_DEPTH) return ZC_FULL;
    if (!bytes || src > UINT64_MAX-(bytes-1) || dst > UINT64_MAX-(bytes-1)) return ZC_ABORT;
    struct zc_request *r = &q->ring[q->tail];
    *r = (struct zc_request){src, dst, ++q->sequence, bytes, ZC_PENDING};
    q->tail = (q->tail+1)%ZC_QUEUE_DEPTH;
    ++q->count;
    *out = r;
    return 0;
}
static inline void zc_progress(struct zc_queue *q)
{
    if (!q->count) return;
    struct zc_request *r = &q->ring[q->head];
    volatile uint32_t *a = q->regs;
    if (!q->running) {
        a[0] = 1; a[0x8c/4] = UINT32_MAX; a[0x88/4] = 0;
        a[0x34/4] = 0x202; a[0x1c/4] = r->bytes;
        a[0x20/4] = r->source; a[0x24/4] = r->source>>32;
        a[0x28/4] = r->destination; a[0x2c/4] = r->destination>>32;
        zc_fence(); a[1] = 1; q->running = 1;
    } else if (a[0x80/4] == 3) {
        zc_fence();
        r->state = (a[0x84/4]&5)==1 ? ZC_COMPLETE : ZC_ABORT;
        q->head = (q->head+1)%ZC_QUEUE_DEPTH;
        --q->count; q->running = 0;
    }
}
static inline void zc_queue_reset(struct zc_queue *q)
{
    q->regs[0] = 0; zc_fence();
    q->regs[0x8c/4] = UINT32_MAX;
    while (q->count) {
        q->ring[q->head].state = ZC_ABORT;
        q->head = (q->head+1)%ZC_QUEUE_DEPTH;
        --q->count;
    }
    q->tail = q->head; q->running = 0;
}
#endif
