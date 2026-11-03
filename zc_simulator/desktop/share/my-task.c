/* Local experiment: one Host task using the existing Device XOR operation.
 * Reuse the reference driver's discovery/MMIO/DMA helpers; do not run its main.
 * Build: gcc -O2 -Wall my-task.c -o my-task
 * Run:   sudo ./my-task [card=0] [bytes=1024] [seed=7] [mode=2]
 * Modes (input/output engines): 1=NPU/NPU, 2=HDMA/HDMA,
 *                               3=HDMA/NPU, 4=NPU/HDMA.
 * Run exclusively: other reference tests share these buffers and mailboxes.
 */
#define main eight_card_reference_main
#include "guest-cluster.c"
#undef main

static unsigned argument(const char *text, unsigned maximum)
{
    char *end;
    unsigned long value = strtoul(text, &end, 0);
    check(*text && !*end && value <= maximum, "invalid argument");
    return (unsigned)value;
}

int main(int argc, char **argv)
{
    check(argc <= 5, "usage: my-task [card 0..7] [bytes 1..65535] [seed 0..255] [mode 1..4]");
    unsigned index = argc > 1 ? argument(argv[1], CARDS - 1) : 0;
    unsigned bytes = argc > 2 ? argument(argv[2], 65535) : 1024;
    unsigned seed = argc > 3 ? argument(argv[3], 255) : 7;
    unsigned mode = argc > 4 ? argument(argv[4], 4) : 2;
    check(bytes && mode, "bytes and mode must be nonzero");
    setbuf(stdout, NULL);

    /* Card index means PCI BDF order, not /dev/test_dma_N probe order. */
    char path[1024], expected[PATH_MAX], actual[PATH_MAX];
    DIR *directory = opendir("/sys/bus/pci/devices");
    check(directory != NULL, "PCI sysfs");
    struct dirent *entry;
    unsigned count = 0;
    while ((entry = readdir(directory))) {
        if (entry->d_name[0] == '.') continue;
        snprintf(path, sizeof(path), "/sys/bus/pci/devices/%s/vendor", entry->d_name);
        if (number(path, 1) != 0x16c3) continue;
        snprintf(path, sizeof(path), "/sys/bus/pci/devices/%s/device", entry->d_name);
        if (number(path, 1) != 0xabcd) continue;
        check(count < CARDS, "too many devices");
        snprintf(cards[count++].path, sizeof(cards[0].path),
                 "/sys/bus/pci/devices/%s", entry->d_name);
    }
    closedir(directory);
    check(index < count, "selected card present");
    qsort(cards, count, sizeof(cards[0]), compare);
    struct card *card = &cards[index];
    check(realpath(card->path, expected) != NULL, "PCI device path");
    directory = opendir("/sys/class/test_dma");
    check(directory != NULL, "driver class (run with driver loaded)");
    while ((entry = readdir(directory))) {
        if (entry->d_name[0] == '.') continue;
        snprintf(path, sizeof(path), "/sys/class/test_dma/%s/device", entry->d_name);
        if (realpath(path, actual) && !strcmp(actual, expected)) {
            check(strlen(entry->d_name) <= 250, "driver node name length");
            snprintf(card->node, sizeof(card->node), "/dev/%.250s", entry->d_name);
            break;
        }
    }
    closedir(directory);
    check(card->node[0], "driver bound to selected card");
    snprintf(path, sizeof(path), "%s/phys_addr", card->path);
    card->iova = number(path, 0);
    check(card->iova < 0x100000000ULL, "Host IOVA outside peer domain");
    card->buf = mapping(card->node, 2097152);
    snprintf(path, sizeof(path), "%s/resource0", card->path);
    card->bar = mapping(path, 0x400000);
    snprintf(path, sizeof(path), "%s/resource4", card->path);
    card->service = (volatile uint32_t *)mapping(path, 0x10001000) + 0x10000000 / 4;
    check(card->bar[2] == 1, "Backend transport ready");
    fence();
    check(card->service[64] == card->service[11], "mailbox idle (stop other tests)");
    card->task = card->service[11];
    if (card->task == UINT32_MAX) card->task = 0;
    atu(index, card->iova);

    /* Customize the input here. The existing Device operation XORs each byte
     * with 0x5a. A different Device algorithm requires a runtime change too. */
    for (unsigned j = 0; j < bytes; ++j) {
        card->buf[j] = (j * 37 + seed) & 255;
        card->buf[0x10000 + j] = 0;
    }
    fence();
    unsigned id = submit(index, mode, card->iova, card->iova + 0x10000, bytes);
    printf("MY_TASK_SUBMIT card=%u device=%s node=%s id=%u mode=%u bytes=%u seed=%u\n",
           index, card->path, card->node, id, mode, bytes, seed);
    finish(index);
    for (unsigned j = 0; j < bytes; ++j) {
        unsigned char expected_byte = ((j * 37 + seed) & 255) ^ 0x5a;
        if (card->buf[0x10000 + j] != expected_byte) {
            printf("MY_TASK_FAIL byte=%u got=%02x expected=%02x\n", j,
                   card->buf[0x10000 + j], expected_byte);
            return 1;
        }
    }
    printf("MY_TASK_PASS card=%u bytes=%u mode=%u\n", index, bytes, mode);
    return 0;
}
