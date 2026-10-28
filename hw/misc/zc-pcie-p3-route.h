/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Internal P3 functional routing. Called under the QEMU device-thread lock. */
static uint64_t p3_mix(uint64_t hash, uint64_t value)
{
    return (hash ^ value) * 1099511628211ULL;
}

/* Pin requests to the fabric configuration present at submission. */
static uint64_t p3_signature(void)
{
    uint64_t hash = p3_mix(1469598103934665603ULL, p3_fabric_generation);
    for (GSList *it = p3_endpoints; it; it = it->next) {
        ZCP1State *t = it->data;
        PCIDevice *d = &t->parent_obj;
        if (!t->p3) {
            continue;
        }
        hash = p3_mix(hash, t->p3_allow);
        hash = p3_mix(hash, t->session);
        hash = p3_mix(hash, t->epoch);
        hash = p3_mix(hash, t->p2.mapping_version);
        hash = p3_mix(hash, p1_ready(t));
        for (PCIBus *bus = pci_get_bus(d); !pci_bus_is_root(bus);
             bus = pci_get_bus(bus->parent_dev)) {
            PCIDevice *br = bus->parent_dev;
            /* Bus numbers, command, memory and prefetch windows. */
            hash = p3_mix(hash, pci_get_word(br->config + PCI_COMMAND));
            for (unsigned off = 0x18; off <= 0x2c; off += 4) {
                hash = p3_mix(hash, pci_get_long(br->config + off));
            }
        }
    }
    return hash;
}

static PCIBus *p3_root(PCIDevice *d)
{
    PCIBus *bus = pci_get_bus(d);
    while (!pci_bus_is_root(bus)) {
        bus = pci_get_bus(bus->parent_dev);
    }
    return bus;
}

/* P3 models a switch-local route, not forwarding through a Host root. */
static PCIBus *p3_common_bus(PCIDevice *a, PCIDevice *b)
{
    for (PCIBus *left = pci_get_bus(a); !pci_bus_is_root(left);
         left = pci_get_bus(left->parent_dev)) {
        for (PCIBus *right = pci_get_bus(b); !pci_bus_is_root(right);
             right = pci_get_bus(right->parent_dev)) {
            if (left == right) {
                return left;
            }
        }
    }
    return NULL;
}

static bool p3_windows(PCIDevice *d, PCIBus *common,
                        uint64_t addr, size_t len)
{
    for (PCIBus *bus = pci_get_bus(d); bus != common;
         bus = pci_get_bus(bus->parent_dev)) {
        PCIDevice *br = bus->parent_dev;
        uint64_t base = pci_bridge_get_base(br, PCI_BASE_ADDRESS_MEM_PREFETCH);
        uint64_t limit = pci_bridge_get_limit(br,
                                              PCI_BASE_ADDRESS_MEM_PREFETCH);
        if (!(pci_get_word(br->config + PCI_COMMAND) & PCI_COMMAND_MEMORY) ||
            addr < base || addr > limit || len - 1 > limit - addr) {
            return false;
        }
    }
    return true;
}

static MemTxResult p3_transfer(ZCP1State *s, uint64_t addr, void *buf,
                               size_t len, bool write, int *peer)
{
    ZCP1State *target = NULL;
    uint64_t offset = 0;
    if (!s->p3_allow || len - 1 > s->peer_size - 1 - (addr - s->peer_base)) {
        return MEMTX_ACCESS_ERROR;
    }
    for (GSList *it = p3_endpoints; it; it = it->next) {
        ZCP1State *t = it->data;
        uint64_t bar = pci_get_bar_addr(&t->parent_obj, 4);
        if (bar == PCI_BAR_UNMAPPED || addr < bar ||
            addr - bar >= 0x800000000ULL) {
            continue;
        }
        /* Overlap, self, another root, disabled/dead target all reject. */
        if (target || t == s || !t->p3 || !t->p3_allow ||
            p3_root(&s->parent_obj) != p3_root(&t->parent_obj) ||
            !p3_common_bus(&s->parent_obj, &t->parent_obj) ||
            !p1_ready(t) || !(pci_get_word(t->parent_obj.config + PCI_COMMAND) &
                              PCI_COMMAND_MEMORY) ||
            len > 0x800000000ULL - (addr - bar)) {
            return MEMTX_ACCESS_ERROR;
        }
        target = t;
        offset = addr - bar;
        if ((offset < SERVICE_OFFSET + 0x10000 &&
             offset + len > SERVICE_OFFSET) ||
            (offset < MAILBOX_OFFSET + 6 * 0x68 &&
             offset + len > MAILBOX_OFFSET) ||
            (offset < 0x1810319c && offset + len > 0x18103180)) {
            return MEMTX_ACCESS_ERROR;
        }
    }
    /*
     * Upstream from the source to the common switch bus, then downstream
     * through target windows. Source windows describe its own subtree, not
     * the peer destination; windows above the common bus are not traversed.
     */
    if (!target || !p3_windows(&target->parent_obj,
                              p3_common_bus(&s->parent_obj,
                                            &target->parent_obj), addr, len)) {
        return MEMTX_ACCESS_ERROR;
    }
    *peer = target->card;
    for (unsigned i = 0; i < 32; i++) {
        uint32_t *r = target->p2.inbound[i];
        uint64_t base = ((uint64_t)r[3] << 32) | r[2];
        uint64_t limit = ((uint64_t)((r[0] & 0x2000) ?
                                    r[8] : r[3]) << 32) | r[4];
        uint64_t local = ((uint64_t)r[6] << 32) | r[5];
        uint64_t delta;
        uint8_t *p;
        if (!(r[1] & 0x80000000u) ||
            ((r[1] & 0x80000) && ((r[0] >> 20) & 7))) {
            continue;
        }
        if (r[1] & 0x40000000) {
            if (((r[1] >> 8) & 7) != 4) {
                continue;
            }
            delta = offset;
        } else {
            if (addr < base || addr > limit) {
                continue;
            }
            if (len - 1 > limit - addr) {
                return MEMTX_ACCESS_ERROR;
            }
            delta = addr - base;
        }
        if ((r[0] & ~0x702000u) || (r[1] & ~0xc0080700u) ||
            local > UINT64_MAX - delta ||
            local + delta > UINT64_MAX - len) {
            return MEMTX_ACCESS_ERROR;
        }
        local += delta;
        /* Peer payloads cannot reach controller/IPC/service state. */
        if ((local < 0x21381200 && local + len > 0x21300000) ||
            (local < 0x1810319c && local + len > 0x18103180) ||
            (local < 0x4110020000ULL && local + len > 0x4100000000ULL)) {
            return MEMTX_ACCESS_ERROR;
        }
        p = p2_local(target, local, len);
        if (!p) {
            return MEMTX_ACCESS_ERROR;
        }
        if (write) {
            memcpy(p, buf, len);
        } else {
            memcpy(buf, p, len);
        }
        return MEMTX_OK;
    }
    return MEMTX_ACCESS_ERROR;
}
