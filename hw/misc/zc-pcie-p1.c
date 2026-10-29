/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Internal functional experiment: shared single-die backend, host-demo IDs.
 * BAR layout is a documented software profile, not a hardware register claim.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/bswap.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qemu/error-report.h"
#include "qemu/memfd.h"
#include "system/qtest.h"
#include "system/address-spaces.h"
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/random.h>
#include "hw/core/qdev-properties.h"
#include "hw/pci/pci_device.h"
#include "hw/pci/pcie.h"
#include "hw/pci/pci_bridge.h"
#include "hw/pci/pci_bus.h"
#include "hw/pci/msix.h"
#include "migration/vmstate.h"
#include "qom/object.h"
#include <sys/mman.h>
#include <sys/stat.h>

#define TYPE_ZC_PCIE_P1 "zc-pcie-p1"
OBJECT_DECLARE_SIMPLE_TYPE(ZCP1State, ZC_PCIE_P1)
#define DDR0_SIZE (1ULL << 30)
#define DDR1_SIZE (1ULL << 38)
#define CONTROL_OFFSET 0x100100000ULL
#define SERVICE_OFFSET 0x10000000ULL
#define MAILBOX_OFFSET 0x18019000ULL

#include "zc-pcie-p2.h"
#include "zc-pcie-p5-fabric.h"

struct ZCP1State {
    PCIDevice parent_obj;
    P2State p2;
    P5State p5;
    bool p3, p3_allow, p4_isolation, p5_flr;
    uint64_t p4_map_generation;
    uint64_t peer_base, peer_size;
    uint64_t proxy_task, proxy_signature;
    bool proxy_pinned;
    MemoryRegion bar0, bar2, bar4;
    QEMUTimer *timer;
    int32_t ddr0_fd, ddr1_fd, mailbox_fd;
    uint8_t *ddr0, *ddr1;
    uint32_t *mailbox;
    uint32_t last_done, notifications, error, epoch;
    bool active, initialized;
    char *backend_socket, *config_sha256;
    uint8_t config_digest[32];
    uint32_t card, timeout_ms, target_epoch;
    int session_fd;
    uint64_t session, request;
    int64_t deadline, heartbeat_at, retry_at;
    uint16_t pending_type;
    bool session_ready, reset_pending, software_stopped, session_started;
};

static GSList *p3_endpoints; /* Device-thread owned; removed before unmap. */
static uint64_t p3_fabric_generation;
static unsigned p3_users;

static void p3_map_commit(MemoryListener *listener)
{
    p3_fabric_generation++;
}

/* Attribute flat-map changes to the endpoints whose BAR decode changed. */
static void p4_map_change(MemoryListener *listener,
                          MemoryRegionSection *section)
{
    for (GSList *it = p3_endpoints; it; it = it->next) {
        ZCP1State *s = it->data;
        if (section->mr == &s->bar0 || section->mr == &s->bar2 ||
            section->mr == &s->bar4) {
            s->p4_map_generation++;
        }
    }
}

static MemoryListener p3_map_listener = {
    .name = "zc-p3-fabric",
    .commit = p3_map_commit,
    .region_add = p4_map_change,
    .region_del = p4_map_change,
};

#include "zc-pcie-p1-session.h"

static uint32_t p1_load(uint32_t *p)
{
    return __atomic_load_n(p, __ATOMIC_ACQUIRE);
}

static void p1_store(uint32_t *p, uint32_t v)
{
    __atomic_store_n(p, v, __ATOMIC_RELEASE);
}

static uint32_t *control(ZCP1State *s)
{
    return (uint32_t *)(s->ddr1 + CONTROL_OFFSET);
}

static bool p1_ready(ZCP1State *s)
{
    uint32_t *c = control(s);
    return s->active && (!s->backend_socket || s->session_ready) &&
           p1_load(s->mailbox + 0x404 / 4) == s->epoch &&
           p1_load(s->mailbox + 0x408 / 4) == 1 &&
           p1_load(c + 4) == 1 && p1_load(c + 5) == 1;
}


#include "zc-pcie-p2-engine.h"

static void p1_tick(void *opaque)
{
    ZCP1State *s = opaque;
    uint32_t done;
    if (s->backend_socket) {
        p1_session_poll(s);
    }
    done = p1_load(control(s) + 11);
    s->active = (!s->backend_socket ||
                 (s->session_ready && !s->reset_pending)) &&
                p1_load(s->mailbox + 0x404 / 4) == s->epoch &&
                p1_load(s->mailbox + 0x408 / 4) == 1;
    if (s->p2.enabled) {
        p2_tick(s);
    }
    if (p1_ready(s) && done && done != s->last_done) {
        s->last_done = done;
        s->notifications++;
        msix_notify(&s->parent_obj, 0);
    }
    timer_mod(s->timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 1);
}

static uint64_t p1_mbox_read(ZCP1State *s, hwaddr off)
{
    unsigned bank = off / 0x68, local = off % 0x68;
    if (bank >= 6) {
        s->error = 2;
        return 0;
    }
    if (local < 8 || (local - 8) % 12 < 8) {
        return p1_load(s->mailbox + off / 4);
    }
    return 0; /* SEND reads zero */
}

static void p1_mbox_write(ZCP1State *s, hwaddr off, uint32_t value)
{
    unsigned bank = off / 0x68, local = off % 0x68;
    uint32_t *p = s->mailbox + off / 4;
    if (bank >= 6 || !p1_ready(s)) {
        s->error = 2;
        return;
    }
    if (local == 0) {
        __atomic_fetch_and(p, ~value, __ATOMIC_ACQ_REL);
    } else if (local == 4 || (local >= 8 && (local - 8) % 12 < 8)) {
        p1_store(p, value);
    } else if (value) {
        unsigned ch = (local - 16) / 12;
        __atomic_fetch_or(s->mailbox + bank * 0x68 / 4,
                          1u << ch, __ATOMIC_RELEASE);
    }
}

static uint64_t p1_read0(void *opaque, hwaddr addr, unsigned size)
{
    ZCP1State *s = opaque;
    if (s->p2.enabled && addr >= P2_ATU_BASE) {
        return p2_reg_read(s, addr);
    }
    switch (addr) {
    case 0: return 0x5a435031;
    case 4: return 2;
    case 8: return p1_ready(s);
    case 12: return s->error;
    case 16: return s->notifications;
    case 20: return s->epoch;
    case 24: return s->last_done;
    default: return 0;
    }
}

static void p1_write0(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    ZCP1State *s = opaque;
    if (s->p2.enabled && addr >= P2_ATU_BASE) {
        p2_reg_write(s, addr, value);
        return;
    }
    /* Test-only IRQ injection independent of data completion. */
    if (addr == 32 && value < 32 && p1_ready(s)) {
        msix_notify(&s->parent_obj, value);
    } else if (addr == 12 && value == 0) {
        s->error = 0;
    }
}

static uint64_t p1_read2(void *opaque, hwaddr addr, unsigned size)
{
    ZCP1State *s = opaque;
    if (s->p2.enabled) {
        uint32_t value;
        p2_inbound(s, 2, addr, &value, false);
        return value;
    }
    if (addr + size <= 0x200000) {
        return ldl_le_p(s->ddr0 + 0x20000000 + addr);
    }
    s->error = 3; /* BootROM FW IPC not implemented. */
    return 0xffffffff;
}

static void p1_write2(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    ZCP1State *s = opaque;
    if (s->p2.enabled) {
        uint32_t v = value;
        p2_inbound(s, 2, addr, &v, true);
        return;
    }
    if (p1_ready(s) && addr + size <= 0x200000) {
        stl_le_p(s->ddr0 + 0x20000000 + addr, value);
    } else {
        s->error = 3;
    }
}

static uint64_t p1_read4(void *opaque, hwaddr addr, unsigned size)
{
    ZCP1State *s = opaque;
    if (addr >= MAILBOX_OFFSET && addr + size <= MAILBOX_OFFSET + 6 * 0x68) {
        return p1_mbox_read(s, addr - MAILBOX_OFFSET);
    }
    if (addr >= SERVICE_OFFSET && addr + size <= SERVICE_OFFSET + 0x10000) {
        return __atomic_load_n((uint32_t *)(s->ddr1 + CONTROL_OFFSET +
                                           addr - SERVICE_OFFSET),
                               __ATOMIC_ACQUIRE);
    }
    if (s->p2.enabled) {
        uint32_t value;
        if (addr >= 0x18103180 && addr < 0x1810319c) {
            return p2_ipc_read(s, addr - 0x18103180);
        }
        p2_inbound(s, 4, addr, &value, false);
        return value;
    }
    s->error = 3; /* Includes unsupported host-demo transfer/HDMA commands. */
    return 0xffffffff;
}

static void p1_write4(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    ZCP1State *s = opaque;
    if (addr >= MAILBOX_OFFSET && addr + size <= MAILBOX_OFFSET + 6 * 0x68) {
        p1_mbox_write(s, addr - MAILBOX_OFFSET, value);
    } else if (p1_ready(s) && addr >= SERVICE_OFFSET + 0x100 &&
               addr + size <= SERVICE_OFFSET + 0x1000) {
        /* Host owns input; backend owns completion and result. */
        p1_store((uint32_t *)(s->ddr1 + CONTROL_OFFSET +
                              addr - SERVICE_OFFSET), value);
    } else if (s->p2.enabled) {
        uint32_t v = value;
        if (addr >= 0x18103180 && addr < 0x1810319c) {
            p2_ipc_write(s, addr - 0x18103180, value);
        } else {
            p2_inbound(s, 4, addr, &v, true);
        }
    } else {
        s->error = 3;
    }
}

#define P1_OPS(readfn, writefn) { .read = readfn, .write = writefn, \
    .endianness = DEVICE_LITTLE_ENDIAN, \
    .valid = { .min_access_size = 4, .max_access_size = 4, \
               .unaligned = false }, \
    .impl = { .min_access_size = 4, .max_access_size = 4 } }
static const MemoryRegionOps ops0 = P1_OPS(p1_read0, p1_write0);
static const MemoryRegionOps ops2 = P1_OPS(p1_read2, p1_write2);
static const MemoryRegionOps ops4 = P1_OPS(p1_read4, p1_write4);

static void *p1_map(int fd, size_t size, Error **errp)
{
    struct stat st;
    void *p;
    if (fstat(fd, &st) || st.st_size != size) {
        error_setg(errp, "P1 shared FD %d must have size %zu", fd, size);
        return NULL;
    }
    p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) {
        error_setg_errno(errp, errno, "P1 cannot mmap FD %d", fd);
        return NULL;
    }
    return p;
}

static void p1_realize(PCIDevice *pdev, Error **errp)
{
    ZCP1State *s = ZC_PCIE_P1(pdev);
    s->session_fd = -1;
    s->p5.fd = -1;
    if (s->p5.socket && !s->p3) {
        error_setg(errp, "P5 fabric requires P3");
        return;
    }
    if (s->p4_isolation && !s->p3) {
        error_setg(errp, "P4 isolation requires P3");
        return;
    }
    if (s->p3 && (!s->p2.enabled || !s->peer_size ||
        s->peer_base > UINT64_MAX - (s->peer_size - 1))) {
        error_setg(errp, "P3 requires P2 and a nonempty, "
                   "nonoverflowing peer aperture");
        return;
    }
    for (GSList *it = p3_endpoints; s->p3 && it; it = it->next) {
        ZCP1State *t = it->data;
        if (t->p3 && t->card == s->card) {
            error_setg(errp, "P3 card-id must be unique");
            return;
        }
    }
    if (s->backend_socket) {
        if (s->ddr0_fd >= 0 || s->ddr1_fd >= 0 || s->mailbox_fd >= 0 ||
            !s->config_sha256 || strlen(s->config_sha256) != 64 ||
            s->timeout_ms < 100 || s->timeout_ms > 60000) {
            error_setg(errp, "P1 socket needs digest/timeout and "
                       "no inherited FDs");
            return;
        }
        for (unsigned i = 0; i < 32; i++) {
            int a = g_ascii_xdigit_value(s->config_sha256[2 * i]);
            int b = g_ascii_xdigit_value(s->config_sha256[2 * i + 1]);
            if (a < 0 || b < 0) {
                error_setg(errp, "P1 invalid configuration SHA256");
                return;
            }
            s->config_digest[i] = a * 16 + b;
        }
        s->ddr0_fd = qemu_memfd_create("zc-p1-ddr0", DDR0_SIZE, false, 0,
            F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL, errp);
        if (s->ddr0_fd < 0) {
            return;
        }
        s->ddr1_fd = qemu_memfd_create("zc-p1-ddr1", DDR1_SIZE, false, 0,
            F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL, errp);
        if (s->ddr1_fd < 0) {
            close(s->ddr0_fd);
            return;
        }
        s->mailbox_fd = qemu_memfd_create("zc-p1-mailbox", 4096, false, 0,
            F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL, errp);
        if (s->mailbox_fd < 0) {
            close(s->ddr1_fd);
            close(s->ddr0_fd);
            return;
        }
    }
    if (!HOST_BIG_ENDIAN && __atomic_always_lock_free(4, NULL)) {
        s->ddr0 = p1_map(s->ddr0_fd, DDR0_SIZE, errp);
        if (!s->ddr0) {
            goto failfds;
        }
        s->ddr1 = p1_map(s->ddr1_fd, DDR1_SIZE, errp);
        if (!s->ddr1) {
            goto fail0;
        }
        s->mailbox = p1_map(s->mailbox_fd, 4096, errp);
        if (!s->mailbox) {
            goto fail1;
        }
    } else {
        error_setg(errp, "P1 requires little-endian host with lock-free u32");
        goto failfds;
    }
    if (pcie_endpoint_cap_init(pdev, 0x80) < 0) {
        error_setg(errp, "cannot initialize P1 PCIe capability");
        goto failmb;
    }
    if (s->p5_flr) {
        pcie_cap_flr_init(pdev);
    }
    memory_region_init_io(&s->bar0, OBJECT(s), &ops0, s,
                          "zc-p1-control", 0x400000);
    memory_region_init_io(&s->bar2, OBJECT(s), &ops2, s, "zc-p1-fw", 0x2000000);
    memory_region_init_io(&s->bar4, OBJECT(s), &ops4, s,
                          "zc-p1-device", 0x800000000ULL);
    pci_register_bar(pdev, 0, PCI_BASE_ADDRESS_SPACE_MEMORY |
                     PCI_BASE_ADDRESS_MEM_TYPE_64, &s->bar0);
    pci_register_bar(pdev, 2, PCI_BASE_ADDRESS_SPACE_MEMORY |
                     PCI_BASE_ADDRESS_MEM_TYPE_64, &s->bar2);
    pci_register_bar(pdev, 4, PCI_BASE_ADDRESS_SPACE_MEMORY |
                     PCI_BASE_ADDRESS_MEM_TYPE_64 |
                     PCI_BASE_ADDRESS_MEM_PREFETCH,
                     &s->bar4);
    if (msix_init(pdev, 32, &s->bar0, 0, 0x2000,
                  &s->bar0, 0, 0x3000, 0, errp)) {
        pcie_cap_exit(pdev);
        goto failmb;
    }
    for (unsigned i = 0; i < 32; i++) {
        msix_vector_use(pdev, i);
    }
    if (s->p3 && p3_users++ == 0) {
        memory_listener_register(&p3_map_listener, &address_space_memory);
    }
    p3_endpoints = g_slist_prepend(p3_endpoints, s);
    s->timer = timer_new_ms(QEMU_CLOCK_REALTIME, p1_tick, s);
    s->active = false;
    timer_mod(s->timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 1);
    return;
failmb:
    munmap(s->mailbox, 4096);
fail1:
    munmap(s->ddr1, DDR1_SIZE);
fail0:
    munmap(s->ddr0, DDR0_SIZE);
failfds:
    if (s->backend_socket) {
        close(s->mailbox_fd);
        close(s->ddr1_fd);
        close(s->ddr0_fd);
    }
}

static void p1_exit(PCIDevice *pdev)
{
    ZCP1State *s = ZC_PCIE_P1(pdev);
    p3_endpoints = g_slist_remove(p3_endpoints, s);
    if (s->p3 && --p3_users == 0) {
        memory_listener_unregister(&p3_map_listener);
    }
    timer_free(s->timer);
    if (s->p5.fd >= 0) {
        close(s->p5.fd);
    }
    if (s->session_fd >= 0) {
        close(s->session_fd); /* Service reaps backend on EOF. */
    }
    msix_uninit(pdev, &s->bar0, &s->bar0);
    pcie_cap_exit(pdev);
    munmap(s->mailbox, 4096);
    munmap(s->ddr1, DDR1_SIZE);
    munmap(s->ddr0, DDR0_SIZE);
    if (s->backend_socket) {
        close(s->mailbox_fd);
        close(s->ddr1_fd);
        close(s->ddr0_fd);
    }
}

static void p1_reset(DeviceState *dev)
{
    ZCP1State *s = ZC_PCIE_P1(dev);
    if (!s->initialized) {
        s->initialized = true;
        return;
    }
    s->p2.mapping_version++;
    s->proxy_pinned = false;
    /* Reset blocks new writes until launcher has reaped/restarted backend. */
    s->active = false;
    if (s->p2.enabled) {
        p2_cancel(s);
        memset(s->p2.ch, 0, sizeof(s->p2.ch));
        memset(s->p2.atu, 0, sizeof(s->p2.atu));
        memset(s->p2.inbound, 0, sizeof(s->p2.inbound));
        p2_irq(s);
    }
    if (s->backend_socket) {
        s->reset_pending = true;
        s->target_epoch = s->epoch + 1;
        msix_reset(&s->parent_obj);
        s->last_done = s->notifications = 0;
        if (s->session_fd >= 0 && s->session_ready && !s->pending_type) {
            p1_session_send(s, P1_QUIESCE, NULL, 0, false);
        } else {
            p1_session_fail(s, "reset during negotiation");
            s->epoch = s->target_epoch;
        }
        return;
    }
    s->epoch++;
    s->last_done = s->notifications = s->error = 0;
    msix_reset(&s->parent_obj);
    p1_store(s->mailbox + 0x400 / 4, s->epoch);
}

static void p1_config_write(PCIDevice *pdev, uint32_t address,
                            uint32_t val, int len)
{
    ZCP1State *s = ZC_PCIE_P1(pdev);
    if (address < 0x28 && address + len > PCI_COMMAND) {
        s->p2.mapping_version++;
    }
    pci_default_write_config(pdev, address, val, len);
    msix_write_config(pdev, address, val, len);
    if (s->p5_flr) {
        pcie_cap_flr_write_config(pdev, address, val, len);
    }
}

static const Property props[] = {
    DEFINE_PROP_STRING("p5-fabric-socket", ZCP1State, p5.socket),
    DEFINE_PROP_BOOL("p5-flr", ZCP1State, p5_flr, false),
    DEFINE_PROP_BOOL("p4-isolation", ZCP1State, p4_isolation, false),
    DEFINE_PROP_BOOL("p3", ZCP1State, p3, false),
    DEFINE_PROP_BOOL("p3-allow", ZCP1State, p3_allow, true),
    DEFINE_PROP_UINT64("peer-base", ZCP1State, peer_base, 0x1000000000ULL),
    DEFINE_PROP_UINT64("peer-size", ZCP1State, peer_size, 0x1000000000ULL),
    DEFINE_PROP_BOOL("p2-trace", ZCP1State, p2.trace, false),
    DEFINE_PROP_BOOL("p2", ZCP1State, p2.enabled, false),
    DEFINE_PROP_STRING("backend-socket", ZCP1State, backend_socket),
    DEFINE_PROP_STRING("config-sha256", ZCP1State, config_sha256),
    DEFINE_PROP_UINT32("card-id", ZCP1State, card, 0),
    DEFINE_PROP_UINT32("session-timeout-ms", ZCP1State, timeout_ms, 30000),
    DEFINE_PROP_INT32("ddr0-fd", ZCP1State, ddr0_fd, -1),
    DEFINE_PROP_INT32("ddr1-fd", ZCP1State, ddr1_fd, -1),
    DEFINE_PROP_INT32("mailbox-fd", ZCP1State, mailbox_fd, -1),
};
static const VMStateDescription vmstate = {
    .name = TYPE_ZC_PCIE_P1,
    .unmigratable = 1,
};
static void p1_class_init(ObjectClass *klass, const void *data)
{
    PCIDeviceClass *pc = PCI_DEVICE_CLASS(klass);
    DeviceClass *dc = DEVICE_CLASS(klass);
    pc->realize = p1_realize;
    pc->exit = p1_exit;
    pc->config_write = p1_config_write;
    pc->vendor_id = 0x16c3;
    pc->device_id = 0xabcd;
    pc->revision = 1;
    pc->class_id = PCI_CLASS_OTHERS;
    device_class_set_props(dc, props);
    device_class_set_legacy_reset(dc, p1_reset);
    dc->vmsd = &vmstate;
    dc->desc = "Internal P1 shared single-die endpoint (software profile)";
}
static const TypeInfo p1_type = {
    .name = TYPE_ZC_PCIE_P1, .parent = TYPE_PCI_DEVICE,
    .instance_size = sizeof(ZCP1State), .class_init = p1_class_init,
    .interfaces = (const InterfaceInfo[]) { { INTERFACE_PCIE_DEVICE }, { } },
};
static void p1_register(void) { type_register_static(&p1_type); }
type_init(p1_register)
