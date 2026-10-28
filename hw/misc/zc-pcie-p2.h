/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Internal P2 Host-only functional subset of the supplied HDMA v0 demo. */
#define P2_HDMA_BASE 0x380000
#define P2_ATU_BASE 0x300000
#define P2_CHANNELS 16
#define P2_REGS (0xac / 4)
#define P2_QUEUE 0x500
#define P2_DATA 0x600
#define P2_PAYLOAD 2048

typedef struct P2Channel {
    uint32_t r[P2_REGS];
    uint64_t src, dst, desc, parent, route_signature, route_addr;
    uint32_t remaining, elements, desc_control;
    bool busy, linked;
} P2Channel;

typedef struct P2State {
    bool enabled, trace;
    P2Channel ch[P2_CHANNELS];
    uint32_t atu[32][9], inbound[32][9];
    uint32_t test_hold; /* qtest-only deterministic in-flight cancellation */
    uint64_t requests, parents, bytes, faults, mapping_version;
} P2State;

static void p2_tick(ZCP1State *s);
static void p2_cancel(ZCP1State *s);
static uint32_t p2_reg_read(ZCP1State *s, uint64_t addr);
static void p2_reg_write(ZCP1State *s, uint64_t addr, uint32_t value);

static bool p2_inbound(ZCP1State *s, unsigned bar, uint64_t off,
                       uint32_t *value, bool write);

static uint32_t p2_ipc_read(ZCP1State *s, unsigned off);
static void p2_ipc_write(ZCP1State *s, unsigned off, uint32_t value);
