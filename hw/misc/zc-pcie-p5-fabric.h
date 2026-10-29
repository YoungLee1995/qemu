/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Optional asynchronous admission to the P5 discrete-event fabric service.
 * Fixed LE frames, no blocking calls from MMIO/the QEMU device thread.
 */
#define P5_SLOTS (P2_CHANNELS + 1)
#define P5_FRAME 48

typedef struct P5Slot {
    uint64_t token;
    int64_t deadline;
    bool sent, granted;
    uint32_t status;
} P5Slot;

typedef struct P5State {
    char *socket;
    int fd;
    uint64_t sequence;
    P5Slot slot[P5_SLOTS];
    uint8_t input[P5_FRAME];
    unsigned received;
    bool failed;
} P5State;
