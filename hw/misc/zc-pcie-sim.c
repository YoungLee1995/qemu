/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Internal experiment only: mock PCIe endpoint for the single-die simulator. */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/bswap.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "chardev/char-fe.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "hw/pci/pci_device.h"
#include "hw/pci/pcie.h"
#include "migration/vmstate.h"
#include "qom/object.h"

#define TYPE_ZC_PCIE_SIM "zc-pcie-sim"
OBJECT_DECLARE_SIMPLE_TYPE(ZCSimState, ZC_PCIE_SIM)
#define PACKET_SIZE 160
#define REPLY_MAX (32 + 33792)

struct ZCSimState {
    PCIDevice parent_obj;
    MemoryRegion bar;
    CharFrontend chr;
    QEMUTimer *timer;
    uint8_t packet[PACKET_SIZE], tx[PACKET_SIZE], reply[REPLY_MAX];
    uint32_t status, error, irq, reply_len, rejected;
    unsigned tx_pos, rx_pos, expected;
    int64_t deadline;
    bool connected;
};

static void zc_finish(ZCSimState *s, unsigned status, unsigned error)
{
    s->status = status;
    s->error = error;
    s->irq = 1;
    timer_del(s->timer);
    pci_set_irq(&s->parent_obj, 1);
}

static void zc_tick(void *opaque)
{
    ZCSimState *s = opaque;
    int ret;

    if (s->status != 1) {
        return;
    }
    if (qemu_clock_get_ms(QEMU_CLOCK_REALTIME) >= s->deadline) {
        zc_finish(s, 3, 1);
        qemu_chr_fe_disconnect(&s->chr);
        return;
    }
    if (s->tx_pos < PACKET_SIZE) {
        ret = qemu_chr_fe_write(&s->chr, s->tx + s->tx_pos,
                               PACKET_SIZE - s->tx_pos);
        if (ret > 0) {
            s->tx_pos += ret;
        }
    }
    timer_mod(s->timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 10);
}

static int zc_can_read(void *opaque)
{
    ZCSimState *s = opaque;
    return s->status == 1 ? s->expected - s->rx_pos : 0;
}

static void zc_receive(void *opaque, const uint8_t *data, int size)
{
    ZCSimState *s = opaque;
    uint32_t payload;

    if (s->status != 1 || size > (int)(s->expected - s->rx_pos)) {
        return;
    }
    memcpy(s->reply + s->rx_pos, data, size);
    s->rx_pos += size;
    if (s->rx_pos == 32 && s->expected == 32) {
        payload = ldl_le_p(s->reply + 20);
        if (ldl_le_p(s->reply) != 0x5041434b ||
            ldl_le_p(s->reply + 4) != ldl_le_p(s->tx + 12) ||
            (payload != 0 && payload != 33792)) {
            zc_finish(s, 3, 3);
            qemu_chr_fe_disconnect(&s->chr);
            return;
        }
        s->expected = 32 + payload;
    }
    if (s->rx_pos == s->expected) {
        s->reply_len = s->expected;
        zc_finish(s, 2, 0);
    }
}

static void zc_event(void *opaque, QEMUChrEvent event)
{
    ZCSimState *s = opaque;
    if (event == CHR_EVENT_OPENED) {
        s->connected = true;
    } else if (event == CHR_EVENT_CLOSED) {
        s->connected = false;
        if (s->status == 1) {
            zc_finish(s, 3, 2);
        }
    }
}

static uint64_t zc_read(void *opaque, hwaddr addr, unsigned size)
{
    ZCSimState *s = opaque;
    if (addr >= 0x100 && addr + size <= 0x100 + PACKET_SIZE) {
        return ldl_le_p(s->packet + addr - 0x100);
    }
    if (addr >= 0x1000 && addr + size <= 0x1000 + s->reply_len) {
        return ldl_le_p(s->reply + addr - 0x1000);
    }
    switch (addr) {
    case 0: return 0x5a435031;
    case 4: return 1;
    case 8: return s->status;
    case 12: return s->error;
    case 20: return s->irq;
    case 28: return s->reply_len;
    case 32: return s->connected;
    case 36: return s->rejected;
    default: return 0;
    }
}

static void zc_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    ZCSimState *s = opaque;
    if (addr >= 0x100 && addr + size <= 0x100 + PACKET_SIZE) {
        stl_le_p(s->packet + addr - 0x100, value);
    } else if (addr == 24) {
        if (s->status != 1) {
            s->status = s->error = s->irq = s->reply_len = 0;
            pci_set_irq(&s->parent_obj, 0);
        }
    } else if (addr == 16 && value == 1) {
        if (s->status != 0) {
            s->rejected++;
            return;
        }
        s->status = 1;
        s->rx_pos = s->tx_pos = 0;
        s->expected = 32;
        memcpy(s->tx, s->packet, PACKET_SIZE);
        if (!s->connected) {
            zc_finish(s, 3, 2);
            return;
        }
        s->deadline = qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 30000;
        qemu_chr_fe_accept_input(&s->chr);
        zc_tick(s);
    }
}

static const MemoryRegionOps zc_ops = {
    .read = zc_read,
    .write = zc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static void zc_realize(PCIDevice *pdev, Error **errp)
{
    ZCSimState *s = ZC_PCIE_SIM(pdev);
    if (!qemu_chr_fe_backend_connected(&s->chr)) {
        error_setg(errp, "zc-pcie-sim requires a chardev");
        return;
    }
    if (pcie_endpoint_cap_init(pdev, 0x80) < 0) {
        error_setg(errp, "cannot initialize PCIe capability");
        return;
    }
    pci_config_set_interrupt_pin(pdev->config, 1);
    s->timer = timer_new_ms(QEMU_CLOCK_REALTIME, zc_tick, s);
    memory_region_init_io(&s->bar, OBJECT(s), &zc_ops, s, "zc-sim-bar0", 65536);
    pci_register_bar(pdev, 0, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar);
    qemu_chr_fe_set_handlers(&s->chr, zc_can_read, zc_receive, zc_event,
                            NULL, s, NULL, true);
}

static void zc_exit(PCIDevice *pdev)
{
    ZCSimState *s = ZC_PCIE_SIM(pdev);
    qemu_chr_fe_set_handlers(&s->chr, NULL, NULL, NULL,
                            NULL, NULL, NULL, false);
    timer_free(s->timer);
    pcie_cap_exit(pdev);
}

static void zc_reset(DeviceState *dev)
{
    ZCSimState *s = ZC_PCIE_SIM(dev);
    if (s->status == 1) {
        qemu_chr_fe_disconnect(&s->chr);
    }
    s->status = s->error = s->irq = s->reply_len = s->rejected = 0;
    s->rx_pos = s->tx_pos = 0;
    timer_del(s->timer);
    memset(s->packet, 0, sizeof(s->packet));
    pci_set_irq(&s->parent_obj, 0);
}

static const Property zc_properties[] = {
    DEFINE_PROP_CHR("chardev", ZCSimState, chr),
};

static const VMStateDescription zc_vmstate = {
    .name = TYPE_ZC_PCIE_SIM,
    .unmigratable = 1,
};

static void zc_class_init(ObjectClass *klass, const void *data)
{
    PCIDeviceClass *pc = PCI_DEVICE_CLASS(klass);
    DeviceClass *dc = DEVICE_CLASS(klass);
    pc->realize = zc_realize;
    pc->exit = zc_exit;
    pc->vendor_id = PCI_VENDOR_ID_QEMU;
    pc->device_id = 0x11f0;
    pc->revision = 1;
    pc->class_id = PCI_CLASS_OTHERS;
    device_class_set_props(dc, zc_properties);
    device_class_set_legacy_reset(dc, zc_reset);
    dc->desc = "Internal mock PCIe/single-die simulator endpoint";
    dc->vmsd = &zc_vmstate;
    dc->user_creatable = true;
}

static const TypeInfo zc_type = {
    .name = TYPE_ZC_PCIE_SIM,
    .parent = TYPE_PCI_DEVICE,
    .instance_size = sizeof(ZCSimState),
    .class_init = zc_class_init,
    .interfaces = (const InterfaceInfo[]) { { INTERFACE_PCIE_DEVICE }, { } },
};

static void zc_register(void)
{
    type_register_static(&zc_type);
}
type_init(zc_register)
