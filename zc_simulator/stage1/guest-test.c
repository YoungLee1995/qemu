/* Internal experiment: Linux userspace BAR test, not a production driver. */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static volatile uint32_t *bar;
static void check(int ok, const char *what)
{
    if (!ok) {
        printf("STAGE1_FAIL: %s errno=%d\n", what, errno);
        exit(1);
    }
}

static unsigned read_number(const char *path)
{
    unsigned v = 0;
    FILE *f = fopen(path, "r");
    if (f) {
        if (fscanf(f, "%x", &v) != 1) {
            v = 0;
        }
        fclose(f);
    }
    return v;
}

static void read_exact_file(const char *path, void *p, size_t n)
{
    int fd = open(path, O_RDONLY);
    check(fd >= 0 && read(fd, p, n) == (ssize_t)n, path);
    close(fd);
}

int main(int argc, char **argv)
{
    char path[512], device[512] = {0};
    DIR *d = opendir("/sys/bus/pci/devices");
    struct dirent *ent;
    uint32_t args[32], packet[40], golden[8448];
    uint16_t command = 2, pci_status;
    unsigned count = 0, endpoints = 0;
    const unsigned cases[][6] = {
        {1, 160, 0, 1, 0x8204000b, 0},
        {2, 160, 0, 1, 0x8204000b, 0},
        {2, 160, 0, 1, 0x8204000b, 4},
        {0, 160, 0, 1, 0x8204000b, 4},
        {3, 159, 0, 1, 0x8204000b, 1},
        {4, 160, 4, 1, 0x8204000b, 2},
        {5, 160, 0, 1, 0, 3},
        {6, 160, 0, 1, 0x8204000b, 0},
        {7, 160, 0, 0, 0, 0},
    };
    check(d != NULL, "PCI sysfs");
    while ((ent = readdir(d))) {
        if (ent->d_name[0] == '.') {
            continue;
        }
        snprintf(path, sizeof(path), "/sys/bus/pci/devices/%s/vendor", ent->d_name);
        unsigned vendor = read_number(path);
        snprintf(path, sizeof(path), "/sys/bus/pci/devices/%s/device", ent->d_name);
        unsigned id = read_number(path);
        printf("PCI %s %04x:%04x\n", ent->d_name, vendor, id);
        if (vendor == 0x1234 && id == 0x11f0) {
            ++endpoints;
            snprintf(device, sizeof(device), "/sys/bus/pci/devices/%s", ent->d_name);
            snprintf(path, sizeof(path), "%s/resource", device);
            FILE *res = fopen(path, "r");
            unsigned long long first = 0, last = 0, flags = 0;
            check(res && fscanf(res, "%llx %llx %llx", &first, &last, &flags) == 3,
                  "EP BAR resource assigned");
            fclose(res);
            check(first != 0 && last - first + 1 == 65536,
                  "EP BAR size and address");
            printf("EP_BAR %s base=%llx size=%llu\n", ent->d_name, first,
                   last - first + 1);
        }
    }
    closedir(d);
    check(device[0], "EP enumerated");
    snprintf(path, sizeof(path), "%s/iommu_group", device);
    char group[512];
    ssize_t len = readlink(path, group, sizeof(group) - 1);
    check(len > 0, "EP IOMMU group");
    group[len] = 0;
    printf("IOMMU_GROUP %s\n", group);
    if (argc == 2 && !strcmp(argv[1], "topology8")) {
        check(endpoints == 8, "eight endpoints enumerated");
        printf("STAGE1_PASS topology8 endpoints=%u\n", endpoints);
        return 0;
    }
    snprintf(path, sizeof(path), "%s/config", device);
    int config = open(path, O_RDWR);
    check(config >= 0, "config open");
    check(pwrite(config, &command, 2, 4) == 2, "enable BAR decode");
    snprintf(path, sizeof(path), "%s/resource0", device);
    int fd = open(path, O_RDWR | O_SYNC);
    check(fd >= 0, "BAR open");
    bar = mmap(NULL, 65536, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    check(bar != MAP_FAILED, "BAR mmap");
    check(bar[0] == 0x5a435031 && bar[1] == 1, "BAR identity");
    check(bar[8] == 1, "simu connected");
    read_exact_file("/tensor-args.bin", args, sizeof(args));
    read_exact_file("/legacy-result.bin", golden, sizeof(golden));
    if (argc == 2) {
        packet[0] = 0x5041434b;
        packet[1] = 1;
        packet[2] = 160;
        packet[3] = 1;
        packet[4] = 0;
        packet[5] = 1;
        packet[6] = 0x8204000b;
        packet[7] = 0;
        memcpy(packet + 8, args, sizeof(args));
        for (unsigned i = 0; i < 40; ++i) {
            bar[0x100/4 + i] = packet[i];
        }
        bar[4] = 1;
        for (unsigned i = 0; bar[2] == 1 && i < 40000; ++i) {
            usleep(1000);
        }
        unsigned expected = !strcmp(argv[1], "timeout") ? 1 :
                            !strcmp(argv[1], "disconnect") ? 2 : 3;
        check(bar[2] == 3 && bar[3] == expected, "expected transport fault");
        check(bar[5] == 1, "fault IRQ pending");
        bar[6] = 1;
        check(bar[2] == 0 && bar[5] == 0, "fault ACK clear");
        printf("STAGE1_PASS fault=%s error=%u\n", argv[1], expected);
        return 0;
    }
    for (unsigned seq = 0; seq < sizeof(cases)/sizeof(cases[0]); ++seq) {
        const unsigned *c = cases[seq];
        packet[0] = 0x5041434b;
        packet[1] = 1;
        packet[2] = c[1];
        packet[3] = c[0];
        packet[4] = c[2];
        packet[5] = c[3];
        packet[6] = c[4];
        packet[7] = 0;
        memcpy(packet + 8, args, sizeof(args));
        for (unsigned i = 0; i < 40; ++i) {
            bar[0x100/4 + i] = packet[i];
        }
        bar[4] = 1;
        /* A second submission while busy must not overwrite the in-flight one. */
        bar[4] = 1;
        for (unsigned wait = 0; bar[2] == 1 && wait < 40000; ++wait) {
            usleep(1000);
        }
        check(bar[2] == 2 && bar[3] == 0, "transport completion");
        check(bar[5] == 1, "INTx pending register");
        check(pread(config, &pci_status, 2, 6) == 2 && (pci_status & 8),
              "PCI interrupt status asserted");
        check(bar[0x1000/4 + 1] == c[0] && bar[0x1000/4 + 2] == c[5],
              "runtime ID/status");
        if (c[5] == 0 && c[3] == 1) {
            ++count;
            check(bar[7] == 33824, "reply length");
            for (unsigned i = 0; i < 8448; ++i) {
                check(bar[0x1020/4 + i] == golden[i], "output matches legacy");
            }
        } else {
            check(bar[7] == 32, "error/STOP reply length");
        }
        check(bar[0x1000/4 + 3] == count && bar[0x1000/4 + 4] == count,
              "submission/execution counters");
        printf("COMMAND seq=%u id=%u status=%u executions=%u bytes=%u PASS\n",
               seq, c[0], c[5], count, bar[7]);
        bar[6] = 1;
        check(bar[2] == 0 && bar[5] == 0, "ACK clear");
        check(pread(config, &pci_status, 2, 6) == 2 && !(pci_status & 8),
              "PCI interrupt status cleared");
    }
    check(bar[9] == 9, "busy requests rejected");
    printf("STAGE1_PASS commands=9 tensor=3 rejected=5 busy_rejected=9\n");
    return 0;
}
