/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Runs only on the QEMU device thread. No blocking socket/model calls. */
static uint8_t *p2_local(ZCP1State *s, uint64_t addr, uint64_t len)
{
    if (addr >= 0x20000000 && addr - 0x20000000 <= DDR0_SIZE &&
        len <= DDR0_SIZE - (addr - 0x20000000)) {
        return s->ddr0 + addr - 0x20000000;
    }
    if (addr >= 0x4000000000ULL && addr - 0x4000000000ULL <= DDR1_SIZE &&
        len <= DDR1_SIZE - (addr - 0x4000000000ULL)) {
        return s->ddr1 + addr - 0x4000000000ULL;
    }
    return NULL;
}

static bool p2_translate(ZCP1State *s, uint64_t addr, uint64_t len,
                         uint64_t *iova)
{
    if (!len || addr > UINT64_MAX - (len - 1)) {
        return false;
    }
    for (unsigned i = 0; i < 32; i++) {
        uint32_t *r = s->p2.atu[i];
        uint64_t base = ((uint64_t)r[3] << 32) | r[2];
        uint64_t limit = ((uint64_t)((r[0] & (1u << 13)) ?
                                   r[8] : r[3]) << 32) | r[4];
        uint64_t target = ((uint64_t)r[6] << 32) | r[5];
        if (!(r[1] & (1u << 31)) || addr < base || addr > limit) {
            continue;
        }
        /* The first matching enabled region owns errors as well as success. */
        if ((r[0] & ~0x2000u) || (r[1] & ~0x80000000u) ||
            len - 1 > limit - addr || target > UINT64_MAX - (addr - base) ||
            target + (addr - base) > UINT64_MAX - (len - 1)) {
            return false;
        }
        *iova = target + addr - base;
        return true;
    }
    return false;
}

#include "zc-pcie-p3-route.h"

/*
 * The source function selects the DMA AddressSpace, hence the requester/domain.
 * memory=true rejects MMIO and self BAR recursion.
 * P3 uses an explicitly reserved peer aperture.
 * One page per transaction makes successful byte accounting unambiguous.
 */
static bool p2_host(ZCP1State *s, uint64_t iova, void *buf, size_t len,
                    bool write, const char *engine, uint64_t original,
                    int channel, uint64_t parent)
{
    MemTxAttrs attrs = { .memory = true };
    PCIDevice *dev = &s->parent_obj;
    bool peer_domain = s->p3 && iova >= s->peer_base &&
                       iova - s->peer_base < s->peer_size;
    const char *path = peer_domain ? "switch_peer" : "host_dma_as";
    int peer = -1;
    MemTxResult result = MEMTX_ACCESS_ERROR;
    s->p2.requests++;
    if (!len || iova > UINT64_MAX - (len - 1) ||
        !(pci_get_word(dev->config + PCI_COMMAND) & PCI_COMMAND_MASTER)) {
        goto done;
    }
    if (s->p3 && channel >= 0 &&
        s->p2.ch[channel].route_signature != p3_signature()) {
        goto done;
    }
    if (s->p3 && channel < 0) {
        uint64_t task = p1_load(control(s) + 64);
        if (!s->proxy_pinned || s->proxy_task != task) {
            s->proxy_task = task;
            s->proxy_signature = p3_signature();
            s->proxy_pinned = true;
        }
        if (s->proxy_signature != p3_signature()) {
            goto done;
        }
    }
    if (peer_domain) {
        result = p3_transfer(s, iova, buf, len, write, &peer);
        goto done;
    }
    result = pci_dma_rw(dev, iova, buf, len,
                        write ? DMA_DIRECTION_FROM_DEVICE :
                                DMA_DIRECTION_TO_DEVICE, attrs);
done:
    if (s->p2.trace && peer >= 0) {
        for (GSList *it = p3_endpoints; it; it = it->next) {
            ZCP1State *t = it->data;
            if (t->p3 && t->card == peer) {
                fprintf(stderr, "{\"event\":\"p3_route\",\"card\":%u,"
                        "\"request\":%" PRIu64 ",\"peer_card\":%u,"
                        "\"peer_session\":%" PRIu64 ",\"peer_epoch\":%u,"
                        "\"peer_mapping_version\":%" PRIu64 ","
                        "\"route_signature\":%" PRIu64 ",\"memtx\":%u}\n",
                        s->card, s->p2.requests, t->card, t->session, t->epoch,
                        t->p2.mapping_version, p3_signature(), result);
            }
        }
    }
    if (s->p2.trace) {
        fprintf(stderr, "{\"event\":\"p2_host\",\"card\":%u,"
                "\"session\":%" PRIu64 ",\"epoch\":%u,\"devfn\":%u,"
                "\"engine\":\"%s\",\"request\":%" PRIu64 ","
                "\"device_addr\":%" PRIu64 ",\"iova\":%" PRIu64 ","
                "\"length\":%zu,\"write\":%s,\"path\":\"%s\",\"peer_card\":%d,"
                "\"mapping_version\":%" PRIu64 ",\"memtx\":%u,"
                "\"timestamp_ns\":%" PRId64 ",\"bus\":%u,\"channel\":%d,"
                "\"parent_id\":%" PRIu64 ",\"task_id\":null,\"hart_id\":null,"
                "\"phase\":\"transaction_returned\","
                "\"address_domain\":\"%s\"}\n",
                s->card, s->session, s->epoch, dev->devfn, engine,
                s->p2.requests, original, iova, len, write ? "true" : "false",
                path, peer, s->p2.mapping_version, result,
                qemu_clock_get_ns(QEMU_CLOCK_REALTIME),
                pci_bus_num(pci_get_bus(dev)), channel, parent,
                peer_domain ? "peer_bus_address" : "host_iova");
    }
    if (result != MEMTX_OK) {
        s->p2.faults++;
        return false;
    }
    s->p2.bytes += len;
    return true;
}

/*
 * The physical MCU IRQ ID is not supplied by the IP manual. IRQ 60 is an
 * explicit internal profile choice, shared with the integration firmware.
 */
static void p2_irq(ZCP1State *s)
{
    bool level = false;
    for (unsigned i = 0; i < P2_CHANNELS; i++) {
        uint32_t st = s->p2.ch[i].r[0x84 / 4];
        uint32_t cfg = s->p2.ch[i].r[0x88 / 4];
        level |= ((st & 1) && (cfg & (1u << 4)) && !(cfg & 1)) ||
                 ((st & 4) && (cfg & (1u << 6)) && !(cfg & 4));
    }
    p1_store(s->mailbox + 0x430 / 4, 60);
    p1_store(s->mailbox + 0x434 / 4, level);
}

static void p2_finish(ZCP1State *s, P2Channel *c, bool error)
{
    c->busy = false;
    c->r[0x80 / 4] = 3; /* stopped */
    c->r[0x84 / 4] |= error ? 4 : 1;
    p2_irq(s);
}

static void p2_cancel_channels(ZCP1State *s)
{
    for (unsigned i = 0; i < P2_CHANNELS; i++) {
        if (s->p2.ch[i].busy) {
            p2_finish(s, &s->p2.ch[i], true);
        }
    }
}

static void p2_cancel(ZCP1State *s)
{
    s->p2.test_hold = 0;
    p2_cancel_channels(s);
    /* Queue response 2 means rejected; producer cannot reuse until response. */
    uint32_t *q = s->mailbox + P2_QUEUE / 4;
    if (p1_load(q) == 1) {
        q[6] = 1;
        p1_store(q, 2);
    }
}

static uint32_t p2_reg_read(ZCP1State *s, uint64_t addr)
{
    if (s->p3 && qtest_enabled() && addr == 0x381088) {
        return s->proxy_pinned ? s->proxy_task : 0;
    }
    if (s->p3 && addr == 0x381084) {
        return s->p3_allow;
    }
    if (addr == 0x381080 && qtest_enabled()) {
        return s->p2.test_hold;
    }
    if (addr >= 0x381000 && addr < 0x381080) {
        return p1_load(s->mailbox + 0x440 / 4 + (addr - 0x381000) / 4);
    }
    if (addr >= P2_HDMA_BASE && addr < P2_HDMA_BASE + 0x1000) {
        unsigned idx = (addr - P2_HDMA_BASE) / 0x100;
        unsigned off = (addr - P2_HDMA_BASE) % 0x100;
        if (off == 4 || off == 0x8c) {
            return 0;
        }
        if (off < 0xac) {
            return s->p2.ch[idx].r[off / 4];
        }
    } else if (addr >= P2_ATU_BASE && addr < P2_ATU_BASE + 32 * 0x200) {
        unsigned idx = (addr - P2_ATU_BASE) / 0x200;
        unsigned off = (addr - P2_ATU_BASE) % 0x200;
        if (off <= 0x20 || (off >= 0x100 && off <= 0x120)) {
            return off >= 0x100 ? s->p2.inbound[idx][(off - 0x100) / 4] :
                                 s->p2.atu[idx][off / 4];
        }
    }
    s->error = 3;
    return UINT32_MAX;
}

static void p2_reg_write(ZCP1State *s, uint64_t addr, uint32_t value)
{
    if (s->p3 && addr == 0x381084) {
        if (value <= 1) {
            s->p3_allow = value;
            s->p2.mapping_version++;
        } else {
            s->error = 3;
        }
        return;
    }
    if (addr == 0x381080 && qtest_enabled()) {
        s->p2.test_hold = value & 15;
        return;
    }
    if (addr >= 0x381000 && addr < 0x381080) {
        if (value == 0) {
            p1_store(s->mailbox + 0x440 / 4 +
                     (addr - 0x381000) / 4, 0);
        }
        return;
    }
    if (addr >= P2_HDMA_BASE && addr < P2_HDMA_BASE + 0x1000) {
        unsigned idx = (addr - P2_HDMA_BASE) / 0x100;
        unsigned off = (addr - P2_HDMA_BASE) % 0x100;
        P2Channel *c = &s->p2.ch[idx];
        if (off == 0x8c) {
            c->r[0x84 / 4] &= ~value;
        } else if (off == 0 && !(value & 1)) {
            if (c->busy) {
                p2_finish(s, c, true);
            }
            c->r[0] = value & 1;
        } else if (off == 4) {
            if (c->busy || value != 1 || !c->r[0] || !p1_ready(s) ||
                c->r[0x38 / 4] || (c->r[0x34 / 4] & ~0x203u) ||
                !(c->r[0x34 / 4] & 2)) {
                p2_finish(s, c, true);
                return;
            }
            c->parent = ++s->p2.parents;
            c->route_signature = s->p3 ? p3_signature() : 0;
            c->linked = c->r[0x34 / 4] & 1;
            c->desc = ((uint64_t)c->r[5] << 32) | c->r[4];
            c->src = ((uint64_t)c->r[9] << 32) | c->r[8];
            c->dst = ((uint64_t)c->r[11] << 32) | c->r[10];
            c->remaining = c->linked ? 0 : c->r[7];
            c->elements = c->desc_control = 0;
            c->busy = true;
            c->r[0x80 / 4] = 1;
            if (!c->linked && !c->remaining) {
                p2_finish(s, c, true);
            }
        } else if (off < 0x80 && !c->busy) {
            c->r[off / 4] = value;
        } else if (off == 0x88) {
            c->r[off / 4] = value;
        } else {
            s->error = 3;
        }
        p2_irq(s);
    } else if (addr >= P2_ATU_BASE && addr < P2_ATU_BASE + 32 * 0x200) {
        unsigned idx = (addr - P2_ATU_BASE) / 0x200;
        unsigned off = (addr - P2_ATU_BASE) % 0x200;
        if (off <= 0x20 || (off >= 0x100 && off <= 0x120)) {
            /* Mapping mutation invalidates in-flight channels. */
            p2_cancel_channels(s);
            s->p2.mapping_version++;
            if (off >= 0x100) {
                s->p2.inbound[idx][(off - 0x100) / 4] = value;
            } else {
                s->p2.atu[idx][off / 4] = value;
            }
        } else {
            s->error = 3;
        }
    } else {
        s->error = 3;
    }
}

static void p2_channel_tick(ZCP1State *s, unsigned idx)
{
    P2Channel *c = &s->p2.ch[idx];
    bool read = idx & 1;
    uint8_t *local;
    uint64_t remote, iova;
    unsigned n;
    uint8_t buf[4096];
    if (!c->busy) {
        return;
    }
    if (!p1_ready(s)) {
        p2_finish(s, c, true);
        return;
    }
    if (s->p3 && c->route_signature != p3_signature()) {
        p2_finish(s, c, true);
        return;
    }
    if (c->linked && !c->remaining) {
        uint8_t *d = p2_local(s, c->desc, 24);
        uint32_t ctl;
        if (!d || (c->desc & 7) || ++c->elements > 4096) {
            p2_finish(s, c, true);
            return;
        }
        ctl = ldl_le_p(d);
        if (ctl & ~7u || (ctl & 1) != ((c->r[6] >> 1) & 1)) {
            p2_finish(s, c, true);
            return;
        }
        if (ctl & 4) {
            c->desc = ldq_le_p(d + 8);
            if (ctl & 2) {
                c->r[6] ^= 2;
            }
            return;
        }
        c->desc_control = ctl;
        c->remaining = ldl_le_p(d + 4);
        c->src = ldq_le_p(d + 8);
        c->dst = ldq_le_p(d + 16);
        if (!c->remaining) {
            p2_finish(s, c, true);
            return;
        }
    }
    if (s->p2.test_hold & (read ? 4 : 8)) {
        return;
    }
    remote = read ? c->src : c->dst;
    if (c->r[0x34 / 4] & 0x200) {
        iova = remote;
    } else if (!p2_translate(s, remote, c->remaining, &iova)) {
        p2_finish(s, c, true);
        return;
    }
    local = p2_local(s, read ? c->dst : c->src, c->remaining);
    if (!local || c->src > UINT64_MAX - (c->remaining - 1) ||
        c->dst > UINT64_MAX - (c->remaining - 1)) {
        p2_finish(s, c, true);
        return;
    }
    n = MIN(c->remaining, 4096 - (iova & 4095));
    if (!read) {
        memcpy(buf, local, n);
    }
    if (!p2_host(s, iova, buf, n, !read, read ? "hdma_read" :
                 "hdma_write", remote, idx, c->parent)) {
        p2_finish(s, c, true);
        return;
    }
    if (read) {
        memcpy(local, buf, n);
    }
    c->src += n;
    c->dst += n;
    c->remaining -= n;
    c->r[7] = c->remaining;
    if (!c->remaining) {
        if (!c->linked || (c->desc_control & 2)) {
            p2_finish(s, c, false);
        } else if (c->desc > UINT64_MAX - 24) {
            p2_finish(s, c, true);
        } else {
            c->desc += 24;
        }
    }
}

static void p2_proxy_tick(ZCP1State *s)
{
    uint32_t *q = s->mailbox + P2_QUEUE / 4;
    uint64_t addr, iova;
    unsigned kind, len;
    bool okay = false;
    uint8_t *data = (uint8_t *)s->mailbox + P2_DATA;
    if (p1_load(q) != 1) {
        return;
    }
    addr = ((uint64_t)q[4] << 32) | q[3];
    kind = q[2];
    len = q[5];
    if (s->p3 && (kind == 1 || kind == 2)) {
        uint64_t task = p1_load(control(s) + 64);
        if (!s->proxy_pinned || s->proxy_task != task) {
            s->proxy_task = task;
            s->proxy_signature = p3_signature();
            s->proxy_pinned = true;
        }
        if (s->proxy_signature != p3_signature()) {
            q[6] = 1;
            p1_store(q, 2);
            return;
        }
    }
    if (p1_ready(s) && (kind == 1 || kind == 2) &&
        (s->p2.test_hold & (kind == 1 ? 1 : 2))) {
        return;
    }
    if (q[1] == s->epoch && q[7] == 0 && len == 4 && !(addr & 3) &&
        (kind == 3 || kind == 4) && addr >= 0x18103180 && addr < 0x1810319c) {
        unsigned off = addr - 0x18103180;
        if (kind == 3) {
            stl_le_p(data, p2_ipc_read(s, off));
        } else {
            p1_store((uint32_t *)(s->ddr1 + CONTROL_OFFSET +
                                 0x2000 + off), ldl_le_p(data));
        }
        okay = true;
    } else if (q[1] == s->epoch && q[7] == 0 && len && len <= P2_PAYLOAD &&
        p1_ready(s) && (kind == 1 || kind == 2) &&
        addr >= 0x18000000000ULL && addr < 0x20000000000ULL &&
        len <= 0x20000000000ULL - addr &&
        p2_translate(s, addr, len, &iova)) {
        unsigned done = 0;
        okay = true;
        while (done < len) {
            unsigned n = MIN(len - done, 4096 - ((iova + done) & 4095));
            if (!p2_host(s, iova + done, data + done, n, kind == 2,
                         "model_proxy", addr + done, -1,
                         ((uint64_t)q[9] << 32) | q[8])) {
                okay = false;
                break;
            }
            done += n;
        }
    } else if (q[1] == s->epoch && q[7] == 0 && len == 4 &&
               !(addr & 3) && (kind == 3 || kind == 4) &&
               ((addr >= 0x21300000 && addr < 0x21304000) ||
                (addr >= 0x21380000 && addr < 0x21381100))) {
        /* Same register state for internal DBI and diagnostic BAR mirror. */
        if (kind == 3) {
            stl_le_p(data, p2_reg_read(s, addr - 0x21000000));
        } else {
            p2_reg_write(s, addr - 0x21000000, ldl_le_p(data));
        }
        okay = true;
    }
    q[6] = okay ? 0 : 1;
    p1_store(q, 2);
}

static void p2_tick(ZCP1State *s)
{
    if (!p1_ready(s)) {
        p2_cancel_channels(s);
        if (s->reset_pending || s->software_stopped ||
            (s->backend_socket && s->session_fd < 0)) {
            p2_cancel(s);
        } else {
            /* Boot-time MCU register polling precedes ready. */
            p2_proxy_tick(s);
        }
        return;
    }
    p2_proxy_tick(s);
    for (unsigned i = 0; i < P2_CHANNELS; i++) {
        p2_channel_tick(s, i);
    }
}

/*
 * DWC unrolled +0x100 inbound bank, memory address/BAR match subset.
 * Lowest numbered matching region wins; invalid targets cannot fall through.
 * Returns true for a claimed or rejected inbound transaction.
 */
static bool p2_inbound(ZCP1State *s, unsigned bar, uint64_t off,
                       uint32_t *value, bool write)
{
    uint64_t address = pci_get_bar_addr(&s->parent_obj, bar);
    if (address == PCI_BAR_UNMAPPED || off > UINT64_MAX - address) {
        goto reject;
    }
    address += off;
    for (unsigned i = 0; i < 32; i++) {
        uint32_t *r = s->p2.inbound[i];
        uint64_t base = ((uint64_t)r[3] << 32) | r[2];
        uint64_t limit = ((uint64_t)((r[0] & (1u << 13)) ?
                                   r[8] : r[3]) << 32) | r[4];
        uint64_t target = ((uint64_t)r[6] << 32) | r[5];
        uint64_t delta;
        uint8_t *local;
        if (!(r[1] & (1u << 31))) {
            continue;
        }
        if ((r[1] & (1u << 19)) && ((r[0] >> 20) & 7) != 0) {
            continue;
        }
        if (r[1] & (1u << 30)) {
            if (((r[1] >> 8) & 7) != bar) {
                continue;
            }
            delta = off;
        } else {
            if (address < base || address > limit) {
                continue;
            }
            if (limit - address < 3) {
                goto reject;
            }
            delta = address - base;
        }
        if ((r[0] & ~0x702000u) || (r[1] & ~0xc0080700u) ||
            target > UINT64_MAX - delta || ((target + delta) & 3)) {
            goto reject;
        }
        target += delta;
        if (!p1_ready(s)) {
            goto reject;
        }
        local = p2_local(s, target, 4);
        /* Controller pages overlap the legacy DDR0 address range. */
        if ((target >= 0x21300000 && target < 0x21304000) ||
            (target >= 0x21380000 && target < 0x21381080)) {
            if (write) {
                p2_reg_write(s, target - 0x21000000, *value);
            } else {
                *value = p2_reg_read(s, target - 0x21000000);
            }
        } else if (target >= 0x18103180 && target < 0x1810319c) {
            if (write) {
                p2_ipc_write(s, target - 0x18103180, *value);
            } else {
                *value = p2_ipc_read(s, target - 0x18103180);
            }
        } else if (local) {
            if (write) {
                stl_le_p(local, *value);
            } else {
                *value = ldl_le_p(local);
            }
        } else {
            goto reject;
        }
        return true;
    }
reject:
    s->error = 3;
    if (!write) {
        *value = UINT32_MAX;
    }
    return true;
}

/* Host-demo IPC registers are shared with the MCU consumer. */
static uint32_t p2_ipc_read(ZCP1State *s, unsigned off)
{
    return p1_load((uint32_t *)(s->ddr1 + CONTROL_OFFSET + 0x2000 + off));
}
static void p2_ipc_write(ZCP1State *s, unsigned off, uint32_t value)
{
    uint32_t trigger = p2_ipc_read(s, 0x10);
    if (!p1_ready(s) || trigger == 0x5a5aa5a5 || trigger == 0x72736c74) {
        s->error = 3;
        return;
    }
    p1_store((uint32_t *)(s->ddr1 + CONTROL_OFFSET + 0x2000 + off), value);
}
