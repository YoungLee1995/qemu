/*
 * ssv_test_app.c - T100 PCIe userspace test application
 *
 * Supports multiple PCIe endpoints discovered from the driver's
 * /sys/class/test_dma/ entries. Each endpoint gets persistent BAR2 and BAR4
 * mappings, a DMA buffer mmap, and a result hugepage.
 *
 * Usage:
 *   ./ssv_test_app [options]                    - Interactive mode
 *   ./ssv_test_app [options] <command> [args]   - Single command mode
 *
 * Options:
 *   --device <spec>   - Select PCIe device (default: first discovered EP)
 *                       <spec> can be: epN or BDF string
 *
 * Commands:
 *   fw-load  [firmware_file]         - Load firmware via PCIe
 *   img-load [image_file]            - Load multi-firmware image via PCIe
 *   file-load [file]                 - Load file to DMA buffer and notify EP
 *   get-result                       - Receive results until device stops
 *   set-device <spec>                - Switch PCIe device
 *   list-devices                     - List discovered PCIe endpoints
 *   help                             - Show help
 *   quit / exit                      - Exit interactive mode
 *
 * Build:
 *   gcc -O2 -o ssv_test_app ssv_test_app.c \
 *       -I$(PWD)/include/linux-hwe-5.4-headers-5.4.0-84/include
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <errno.h>

/* ------------------------------------------------------------------ */
/* Constants                                                          */
/* ------------------------------------------------------------------ */

#define MAX_CMD          128
#define MAX_LINE         256
#define SYSPATH_MAX      256
#define LINKPATH_MAX     4096

#define IMAGE_FILE_MAX_SIZE (2ULL * 1024 * 1024 * 1024)

#define BAR2_FW_OFFSET       0x000000U
#define BAR2_FW_SIZE         0x200000U
#define BAR2_IPC_OFFSET      0x200000U
#define BAR2_IPC_WINDOW_BASE 0x18103000U
#define BAR2_IPC_REG_OFFSET  0x0180U
#define EP_FW_SRAM_BASE      0x40000000ULL
#define EP_FW_SRAM_END       (EP_FW_SRAM_BASE + BAR2_FW_SIZE)

#define EP_DMA_ADDR_LOW   0x00
#define EP_DMA_ADDR_HIGH  0x04
#define EP_DMA_SIZE_LOW   0x08
#define EP_DMA_SIZE_HIGH  0x0C
#define EP_DMA_TRIGGER    0x10
#define EP_DMA_CRC_LOW    0x14
#define EP_DMA_CRC_HIGH   0x18
#define EP_DMA_MAGIC      0x5a5aa5a5U

#define EP_RESULT_ADDR_LOW   0x00
#define EP_RESULT_ADDR_HIGH  0x04
#define EP_RESULT_TRIGGER    0x10
#define EP_RESULT_MAGIC      0x72736c74U /* "rslt" in ASCII */
#define EP_RESULT_DONE       0x646F6E65U  /* "done" in ASCII */

#define HUGE_SIZE        (2 * 1024 * 1024) /* 2MB hugepage */
#define BUF_DISPLAY      32                          /* bytes to display per poll */

#define MAILBOX_OFFSET   0x19000
#define SRAM_OFFSET      0x20000
#define DDR_OFFSET       0x0

#define MAGIC_VALUE      0x12345678
#define BAR2_WRITE_VAL   0xa5ccccd8

/* IPC registers */
#define PCIE_IPC_BASE    0x18103180
#define IPC_REG_CMD      0x00
#define IPC_REG_STATUS   0x04
#define IPC_REG_DATA0    0x08
#define IPC_REG_DATA1    0x0C
#define IPC_CMD_FW       0x12

#define FW_FILE          "mcu_image.bin"
#define MODEL_FILE       "model.bin"
#define WEIGHT_FILE      "weight.bin"
#define INPUT_FILE       "input.bin"

#define MY_VENDOR_ID    0x16c3
#define MY_DEVICE_ID    0xabcd

#define rmb() __asm__ __volatile__("lfence":::"memory")
#define wmb() __asm__ __volatile__("sfence":::"memory")

/* image/firmware format */
#define IMAGE_HEADER_MAGIC   0x54313030U /* "T100" */
#define FW_TYPE_MCU          0x004D4355U         /* "MCU"  */
#define FW_TYPE_HSM          0x0048534DU         /* "HSM"  */
#define CONFIG_FW_MAX_COUNT  5U

#pragma pack(push, 1)
typedef struct {
    uint32_t type;
    uint32_t crc;
    uint32_t size;
    uint32_t dev_id;
    uint64_t load_addr;    /* fw blob Starting address(header Starting address) */
    uint64_t entry_addr;  /* Final jump address */
} firmware_header_t;

typedef struct {
    uint32_t magic;
    uint32_t crc;
    uint16_t version;
    uint16_t firmware_count;
    uint32_t size[CONFIG_FW_MAX_COUNT];
    uint64_t load_addr[CONFIG_FW_MAX_COUNT];
} image_header_t;
#pragma pack(pop)

static char link_target[LINKPATH_MAX];
static char sys_path[SYSPATH_MAX];

/* Forward declarations (used by per-EP lifecycle) */
static uint64_t virt_to_phys(void *virt);
static int read_u64_from_file(const char *path, uint64_t *value);

/* ------------------------------------------------------------------ */
/* Per-endpoint context                                                  */
/* ------------------------------------------------------------------ */

#define MAX_EPS 16

typedef struct {
    char     bdf[16];            /* PCI BDF string "0000:01:00.0"       */
    char     class_name[32];    /* Class entry name "test_dma_0"       */
    char     name[32];          /* Human name "ep0", "ep1", etc.        */
    int      minor;                  /* /dev/test_dma_<minor> minor number    */
    int      chip_index;       /* IPC chip index for stride            */
    volatile void *bar2;  /* mmap of resource2 (NULL if closed)   */
    int      bar2_fd;             /* BAR2 file descriptor (-1 if closed)  */
    uint64_t bar2_size;            /* BAR2 resource size */
    volatile void *bar4;  /* mmap of resource4 (NULL if closed)   */
    int      bar4_fd;             /* BAR4 file descriptor (-1 if closed)  */
    uint64_t bar4_size;            /* BAR4 resource size */
    void     *dma_buf;          /* mmap of /dev/test_dma_N (NULL)       */
    int      dma_fd;                /* DMA file descriptor (-1 if closed)   */
    uint64_t dma_phys;          /* Physical address from sysfs phys_addr */
    uint64_t dma_size;          /* Buffer size from sysfs buf_size       */
    void     *result_buf;    /* Hugepage for result reception (NULL)  */
    uint64_t result_phys;    /* Hugepage physical address            */
    int      active;                /* 1 if this EP is opened and in use    */
} ep_context_t;

static ep_context_t ep_list[MAX_EPS];
static int          ep_count = 0;
static ep_context_t *current_ep = NULL;

/* ------------------------------------------------------------------ */
/* IPC register helper types and functions                            */
/* ------------------------------------------------------------------ */

typedef struct {
    volatile uint32_t *addr_low;
    volatile uint32_t *addr_high;
    volatile uint32_t *size_low;
    volatile uint32_t *size_high;
    volatile uint32_t *crc_low;
    volatile uint32_t *crc_high;
    volatile uint32_t *trigger;
} ipc_dma_regs_t;

typedef struct {
    volatile uint32_t *cmd;
    volatile uint32_t *status;
    volatile uint32_t *data0;
    volatile uint32_t *data1;
} ipc_cmd_regs_t;

typedef struct {
    volatile uint32_t *addr_low;
    volatile uint32_t *addr_high;
    volatile uint32_t *trigger;
} ipc_result_regs_t;

static ipc_dma_regs_t ipc_get_dma_regs(ep_context_t *ep)
{
    volatile uint8_t *base = (volatile uint8_t *)ep->bar4
        + PCIE_IPC_BASE;
    ipc_dma_regs_t r;
    r.addr_low   = (volatile uint32_t *)(base + EP_DMA_ADDR_LOW);
    r.addr_high  = (volatile uint32_t *)(base + EP_DMA_ADDR_HIGH);
    r.size_low   = (volatile uint32_t *)(base + EP_DMA_SIZE_LOW);
    r.size_high  = (volatile uint32_t *)(base + EP_DMA_SIZE_HIGH);
    r.crc_low    = (volatile uint32_t *)(base + EP_DMA_CRC_LOW);
    r.crc_high   = (volatile uint32_t *)(base + EP_DMA_CRC_HIGH);
    r.trigger    = (volatile uint32_t *)(base + EP_DMA_TRIGGER);
    return r;
}

static ipc_cmd_regs_t ipc_get_fw_ipc_regs(ep_context_t *ep)
{
    volatile uint8_t *base = (volatile uint8_t *)ep->bar2
        + BAR2_IPC_OFFSET + BAR2_IPC_REG_OFFSET;
    ipc_cmd_regs_t r;
    r.cmd    = (volatile uint32_t *)(base + IPC_REG_CMD);
    r.status = (volatile uint32_t *)(base + IPC_REG_STATUS);
    r.data0  = (volatile uint32_t *)(base + IPC_REG_DATA0);
    r.data1  = (volatile uint32_t *)(base + IPC_REG_DATA1);
    return r;
}

static ipc_result_regs_t ipc_get_result_regs(ep_context_t *ep)
{
    volatile uint8_t *base = (volatile uint8_t *)ep->bar4
        + PCIE_IPC_BASE;
    ipc_result_regs_t r;
    r.addr_low  = (volatile uint32_t *)(base + EP_RESULT_ADDR_LOW);
    r.addr_high = (volatile uint32_t *)(base + EP_RESULT_ADDR_HIGH);
    r.trigger   = (volatile uint32_t *)(base + EP_RESULT_TRIGGER);
    return r;
}

static void ipc_write_u64(volatile uint32_t *low, volatile uint32_t *high,
                           uint64_t val)
{
    *low  = (uint32_t)(val & 0xFFFFFFFFU);
    *high = (uint32_t)(val >> 32);
    wmb();
}

/* ------------------------------------------------------------------ */
/* Device selection                                                   */
/* ------------------------------------------------------------------ */

static void set_current_ep(ep_context_t *ep)
{
    current_ep = ep;
    printf("Active device: %s (BDF=%s, minor=%d)\n",
           ep->name, ep->bdf, ep->minor);
}

static ep_context_t *find_ep_by_name(const char *name)
{
    for (int i = 0; i < ep_count; i++) {
        if (strcmp(ep_list[i].name, name) == 0)
            return &ep_list[i];
    }
    return NULL;
}

static ep_context_t *find_ep_by_bdf(const char *bdf)
{
    for (int i = 0; i < ep_count; i++) {
        if (strcmp(ep_list[i].bdf, bdf) == 0)
            return &ep_list[i];
    }
    return NULL;
}

static ep_context_t *resolve_ep(const char *spec)
{
    ep_context_t *ep;

    /* "epN" format */
    ep = find_ep_by_name(spec);
    if (ep)
        return ep;

    /* Raw BDF string */
    ep = find_ep_by_bdf(spec);
    if (ep)
        return ep;

    fprintf(stderr, "Unknown device: %s (use epN or BDF)\n", spec);
    return NULL;
}

static int set_device(const char *spec)
{
    ep_context_t *ep = resolve_ep(spec);
    if (!ep)
        return 1;
    if (!ep->active) {
        fprintf(stderr, "EP %s is not active (open failed?)\n", ep->name);
        return 1;
    }
    set_current_ep(ep);
    return 0;
}

/* Build sysfs/device paths for a given BDF */
static void path_bar2(const char *bdf, char *buf, size_t sz)
{ snprintf(buf, sz, "/sys/bus/pci/devices/%s/resource2", bdf); }

static void path_bar4(const char *bdf, char *buf, size_t sz)
{ snprintf(buf, sz, "/sys/bus/pci/devices/%s/resource4", bdf); }

static void path_resource(const char *bdf, char *buf, size_t sz)
{ snprintf(buf, sz, "/sys/bus/pci/devices/%s/resource", bdf); }

static int read_resource_size(const char *bdf, unsigned int resource_index,
                              uint64_t *size_out)
{
    FILE *fp;
    char line[256];
    char path[MAX_CMD];
    unsigned long long start;
    unsigned long long end;
    unsigned long long flags;
    int fields;
    unsigned int index = 0;

    if (!size_out)
        return -1;

    path_resource(bdf, path, sizeof(path));
    fp = fopen(path, "r");
    if (!fp) {
        perror(path);
        return -1;
    }

    while (fgets(line, sizeof(line), fp)) {
        if (index++ != resource_index)
            continue;

        fields = sscanf(line, "%llx %llx %llx", &start, &end, &flags);
        if (fields < 2) {
            fprintf(stderr, "Failed to parse BAR%u resource line in %s\n",
                    resource_index, path);
            fclose(fp);
            return -1;
        }
        fclose(fp);

        if (end < start || start == 0) {
            fprintf(stderr, "BAR%u is unassigned in %s\n", resource_index,
                    path);
            return -1;
        }

        *size_out = (uint64_t)(end - start + 1ULL);
        return 0;
    }
    fclose(fp);

    fprintf(stderr, "BAR%u resource line missing in %s\n", resource_index,
            path);
    return -1;
}

static void path_phys_addr(const ep_context_t *ep, char *buf, size_t sz)
{ snprintf(buf, sz, "/sys/bus/pci/devices/%s/test_dma/%s/device/phys_addr", ep->bdf, ep->class_name); }

static void path_buf_size(const ep_context_t *ep, char *buf, size_t sz)
{ snprintf(buf, sz, "/sys/bus/pci/devices/%s/test_dma/%s/device/buf_size", ep->bdf, ep->class_name); }

/* ------------------------------------------------------------------ */
/* Endpoint discovery                                                 */
/* ------------------------------------------------------------------ */

static void init_ep_slot(ep_context_t *ep, const char *bdf, const char *class_name,
                          int minor, int chip_idx)
{
    snprintf(ep->bdf, sizeof(ep->bdf), "%s", bdf);
    snprintf(ep->class_name, sizeof(ep->class_name), "%s", class_name);
    snprintf(ep->name, sizeof(ep->name), "ep%d", minor);
    ep->minor      = minor;
    ep->chip_index = chip_idx;
    ep->bar2       = NULL;  ep->bar2_fd    = -1;
    ep->bar2_size  = 0;
    ep->bar4       = NULL;  ep->bar4_fd    = -1;
    ep->bar4_size  = 0;
    ep->dma_buf    = NULL;  ep->dma_fd     = -1;
    ep->dma_phys   = 0;     ep->dma_size   = 0;
    ep->result_buf = NULL;  ep->result_phys = 0;
    ep->active     = 0;
}

static int discover_endpoints(void)
{
    char bdf[16];
    char line[128];
    int minor;
    FILE *pipe;

    ep_count = 0;
    pipe = popen("ls /sys/class/test_dma/", "r");
    if (!pipe) {
        perror("popen ls /sys/class/test_dma");
        return -1;
    }

    while (ep_count < MAX_EPS && fgets(line, sizeof(line), pipe)) {
        /* Strip newline */
        char *nl = strchr(line, '\n');
        if (nl) *nl = '\0';
        /* Skip empty or non-matching entries */
        if (strncmp(line, "test_dma_", 9) != 0)
            continue;

        minor = atoi(line + 9);

        /* Read the "device" symlink inside the class entry to get PCI BDF.
           /sys/class/test_dma/test_dma_N/device -> ../../devices/pci<D/:<B>.<F>
           realpath resolves it to /sys/bus/pci/devices/<BDF> */
        snprintf(sys_path, sizeof(sys_path),
                 "/sys/class/test_dma/%s/device", line);
        char *resolved = realpath(sys_path, link_target);
        if (!resolved)
            continue;

        /* Extract BDF: realpath may return paths like
           /sys/devices/pci0000:00/0000:00:04.0/0000:01:00.0
           The BDF is always the last path component after "devices/" */
        char *p = strstr(link_target, "devices/");
        if (!p)
            continue;
        p += strlen("devices/");
        /* Walk to the last path component (skip intermediate dirs) */
        char *last = p;
        for (char *s = p; (s = strchr(s, '/')) != NULL; ) {
            last = s + 1;
            s = last;
        }
        size_t bdf_len = strlen(last);
        if (bdf_len == 0 || bdf_len >= sizeof(bdf))
            continue;
        memcpy(bdf, last, bdf_len);
        bdf[bdf_len] = '\0';

        init_ep_slot(&ep_list[ep_count], bdf, line, minor, ep_count);
        ep_count++;
    }
    pclose(pipe);

    /* Sort by minor for deterministic ordering */
    for (int i = 1; i < ep_count; i++) {
        ep_context_t tmp = ep_list[i];
        int j = i - 1;
        while (j >= 0 && ep_list[j].minor > tmp.minor) {
            ep_list[j + 1] = ep_list[j];
            j--;
        }
        ep_list[j + 1] = tmp;
    }

    /* Reassign names after sort so ep0/ep1 match minor order */
    for (int i = 0; i < ep_count; i++)
        snprintf(ep_list[i].name, sizeof(ep_list[i].name), "ep%d", ep_list[i].minor);

    return ep_count;
}

static int discover_endpoints_fallback(void)
{
    char vpath[SYSPATH_MAX], dpath[SYSPATH_MAX];
    char vbuf[32], dbuf[32];
    char line[64];
    int minor = 0;
    FILE *pipe;

    ep_count = 0;
    pipe = popen("ls /sys/bus/pci/devices/", "r");
    if (!pipe)
        return -1;

    while (ep_count < MAX_EPS && fgets(line, sizeof(line), pipe)) {
        char *nl = strchr(line, '\n');
        if (nl) *nl = '\0';
        if (line[0] == '\0')
            continue;

        snprintf(vpath, sizeof(vpath),
                 "/sys/bus/pci/devices/%s/vendor", line);
        snprintf(dpath, sizeof(dpath),
                 "/sys/bus/pci/devices/%s/device", line);

        FILE *vf = fopen(vpath, "r");
        if (!vf) continue;
        if (fscanf(vf, "%s", vbuf) != 1) { fclose(vf); continue; }
        fclose(vf);

        FILE *df = fopen(dpath, "r");
        if (!df) continue;
        if (fscanf(df, "%s", dbuf) != 1) { fclose(df); continue; }
        fclose(df);

        if (strcmp(vbuf, "0x16c3") != 0 || strcmp(dbuf, "0xabcd") != 0)
            continue;

        char cname[32];
        snprintf(cname, sizeof(cname), "test_dma_%d", minor);
        init_ep_slot(&ep_list[ep_count], line, cname, minor, ep_count);
        ep_count++;
        minor++;
    }
    pclose(pipe);
    return ep_count;
}

/* ------------------------------------------------------------------ */
/* Per-EP lifecycle                                                   */
/* ------------------------------------------------------------------ */

static void ep_dma_dev_path(const ep_context_t *ep, char *buf, size_t sz)
{
    snprintf(buf, sz, "/dev/test_dma_%d", ep->minor);
}

static int ep_open_bar4(ep_context_t *ep)
{
    char path[MAX_CMD];

    if (ep->bar4 != NULL)
        return 0; /* already open */

    path_bar4(ep->bdf, path, sizeof(path));
    printf("BAR4: %s\n", path);
    if (read_resource_size(ep->bdf, 4, &ep->bar4_size) != 0)
        return -1;
    if (ep->bar4_size > (uint64_t)(size_t)-1) {
        fprintf(stderr, "BAR4 resource is too large to map: %llu\n",
                (unsigned long long)ep->bar4_size);
        ep->bar4_size = 0;
        return -1;
    }
    ep->bar4_fd = open(path, O_RDWR);
    if (ep->bar4_fd < 0) {
        perror("open BAR4");
        return -1;
    }

    ep->bar4 = mmap(NULL, (size_t)ep->bar4_size, PROT_READ | PROT_WRITE,
                    MAP_SHARED, ep->bar4_fd, 0);
    if (ep->bar4 == MAP_FAILED) {
        perror("mmap BAR4");
        close(ep->bar4_fd);
        ep->bar4_fd = -1;
        ep->bar4_size = 0;
        ep->bar4 = NULL;
        return -1;
    }
    printf("EP %s: BAR4 mapped at %p (size=%llu)\n", ep->name,
           (void *)ep->bar4, (unsigned long long)ep->bar4_size);
    return 0;
}

static int ep_open_bar2(ep_context_t *ep)
{
    char path[MAX_CMD];

    if (ep->bar2 != NULL)
        return 0;

    path_bar2(ep->bdf, path, sizeof(path));
    printf("BAR2: %s\n", path);
    if (read_resource_size(ep->bdf, 2, &ep->bar2_size) != 0)
        return -1;
    if (ep->bar2_size > (uint64_t)(size_t)-1) {
        fprintf(stderr, "BAR2 resource is too large to map: %llu\n",
                (unsigned long long)ep->bar2_size);
        ep->bar2_size = 0;
        return -1;
    }
    ep->bar2_fd = open(path, O_RDWR);
    if (ep->bar2_fd < 0) {
        perror("open BAR2");
        return -1;
    }

    ep->bar2 = mmap(NULL, (size_t)ep->bar2_size, PROT_READ | PROT_WRITE,
                    MAP_SHARED, ep->bar2_fd, 0);
    if (ep->bar2 == MAP_FAILED) {
        perror("mmap BAR2");
        close(ep->bar2_fd);
        ep->bar2_fd = -1;
        ep->bar2_size = 0;
        ep->bar2 = NULL;
        return -1;
    }
    printf("EP %s: BAR2 mapped at %p (size=%llu)\n", ep->name,
           (void *)ep->bar2, (unsigned long long)ep->bar2_size);
    return 0;
}

static void ep_close_bar2(ep_context_t *ep)
{
    if (ep->bar2 != NULL) {
        munmap((void *)ep->bar2, (size_t)ep->bar2_size);
        ep->bar2 = NULL;
    }
    if (ep->bar2_fd >= 0) {
        close(ep->bar2_fd);
        ep->bar2_fd = -1;
    }
    ep->bar2_size = 0;
}

static void ep_close_bar4(ep_context_t *ep)
{
    if (ep->bar4 != NULL) {
        munmap((void *)ep->bar4, (size_t)ep->bar4_size);
        ep->bar4 = NULL;
    }
    if (ep->bar4_fd >= 0) {
        close(ep->bar4_fd);
        ep->bar4_fd = -1;
    }
    ep->bar4_size = 0;
}

static int ep_open_dma(ep_context_t *ep)
{
    char dev_path[MAX_CMD], sys_path[MAX_CMD];

    if (ep->dma_buf != NULL)
        return 0; /* already open */

    /* Read sysfs attributes */
    path_phys_addr(ep, sys_path, sizeof(sys_path));
    if (read_u64_from_file(sys_path, &ep->dma_phys) != 0)
        return -1;

    path_buf_size(ep, sys_path, sizeof(sys_path));
    if (read_u64_from_file(sys_path, &ep->dma_size) != 0)
        return -1;

    /* Open /dev/test_dma_<N> */
    ep_dma_dev_path(ep, dev_path, sizeof(dev_path));
    ep->dma_fd = open(dev_path, O_RDWR);
    if (ep->dma_fd < 0) {
        perror("open DMA device");
        return -1;
    }

    ep->dma_buf = mmap(NULL, (size_t)ep->dma_size, PROT_READ | PROT_WRITE,
                       MAP_SHARED, ep->dma_fd, 0);
    if (ep->dma_buf == MAP_FAILED) {
        perror("mmap DMA buffer");
        close(ep->dma_fd);
        ep->dma_fd = -1;
        ep->dma_buf = NULL;
        return -1;
    }
    printf("EP %s: DMA mapped at %p (phys=0x%016llx size=%llu)\n",
           ep->name, ep->dma_buf,
           (unsigned long long)ep->dma_phys,
           (unsigned long long)ep->dma_size);
    return 0;
}

static void ep_close_dma(ep_context_t *ep)
{
    if (ep->dma_buf != NULL) {
        munmap(ep->dma_buf, (size_t)ep->dma_size);
        ep->dma_buf = NULL;
    }
    if (ep->dma_fd >= 0) {
        close(ep->dma_fd);
        ep->dma_fd = -1;
    }
}

static int ep_alloc_result(ep_context_t *ep)
{
    if (ep->result_buf != NULL)
        return 0; /* already allocated */

    ep->result_buf = mmap(NULL, HUGE_SIZE, PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB, -1, 0);
    if (ep->result_buf == MAP_FAILED) {
        perror("mmap hugepage");
        printf("tips!!! Ensure hugepage support: sudo sysctl -w vm.nr_hugepages=1024\n");
        ep->result_buf = NULL;
        return -1;
    }
    memset(ep->result_buf, 0, HUGE_SIZE);

    ep->result_phys = virt_to_phys(ep->result_buf);
    if (ep->result_phys == 0) {
        fprintf(stderr, "EP %s: failed to get result phys addr\n", ep->name);
        munmap(ep->result_buf, HUGE_SIZE);
        ep->result_buf = NULL;
        return -1;
    }
    if ((ep->result_phys & (HUGE_SIZE - 1)) != 0) {
        fprintf(stderr, "EP %s: result phys not 2MB-aligned (0x%016llx)\n",
                ep->name, (unsigned long long)ep->result_phys);
        munmap(ep->result_buf, HUGE_SIZE);
        ep->result_buf = NULL;
        return -1;
    }
    printf("EP %s: result hugepage at %p phys=0x%016llx\n",
           ep->name, ep->result_buf, (unsigned long long)ep->result_phys);
    return 0;
}

static void ep_free_result(ep_context_t *ep)
{
    if (ep->result_buf != NULL) {
        munmap(ep->result_buf, HUGE_SIZE);
        ep->result_buf = NULL;
        ep->result_phys = 0;
    }
}

static int ep_open(ep_context_t *ep)
{
    if (ep_open_bar2(ep) != 0)
        return -1;
    if (ep_open_bar4(ep) != 0)
    {
        ep_close_bar2(ep);
        return -1;
    }
    if (ep_open_dma(ep) != 0) {
        ep_close_bar4(ep);
        ep_close_bar2(ep);
        return -1;
    }
    if (ep_alloc_result(ep) != 0) {
        ep_close_dma(ep);
        ep_close_bar4(ep);
        ep_close_bar2(ep);
        return -1;
    }
    ep->active = 1;
    return 0;
}

static void ep_close(ep_context_t *ep)
{
    ep_free_result(ep);
    ep_close_dma(ep);
    ep_close_bar4(ep);
    ep_close_bar2(ep);
    ep->active = 0;
}

static void ep_open_all(void)
{
    for (int i = 0; i < ep_count; i++) {
        if (ep_open(&ep_list[i]) != 0) {
            fprintf(stderr, "Failed to open EP %s (%s), skipping\n",
                    ep_list[i].name, ep_list[i].bdf);
        }
    }
}

static void ep_close_all(void)
{
    for (int i = 0; i < ep_count; i++) {
        ep_close(&ep_list[i]);
    }
}

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

static uint64_t virt_to_phys(void *virt)
{
    long page_size = sysconf(_SC_PAGE_SIZE);
    uint64_t page_frame = (uint64_t)virt / page_size;
    uint64_t offset     = (uint64_t)virt % page_size;
    int fd;
    uint64_t entry;
    ssize_t ret;

    fd = open("/proc/self/pagemap", O_RDONLY);
    if (fd < 0) {
        perror("open /proc/self/pagemap");
        return 0;
    }
    ret = pread(fd, &entry, sizeof(entry), page_frame * sizeof(entry));
    close(fd);
    if (ret != (ssize_t)sizeof(entry)) {
        fprintf(stderr, "Failed to read pagemap entry\n");
        return 0;
    }
    if (!(entry & (1ULL << 63))) {
        fprintf(stderr, "Page not present\n");
        return 0;
    }
    return (entry & ((1ULL << 54) - 1)) * page_size + offset;
}

static void print_buffer(const void *buf)
{
    int i;
    printf("[");
    for (i = 0; i < BUF_DISPLAY && i < 4096; i++) {
        printf("%02x", ((const unsigned char *)buf)[i]);
        if ((i + 1) % 4 == 0 && i < BUF_DISPLAY - 1)
            printf(" ");
    }
    printf("]\n");
}

static int read_u64_from_file(const char *path, uint64_t *value)
{
    FILE *fp;
    unsigned long long tmp;

    fp = fopen(path, "r");
    if (!fp) {
        perror(path);
        return -1;
    }

    if (fscanf(fp, "%llu", &tmp) != 1) {
        fprintf(stderr, "Failed to parse number from %s\n", path);
        fclose(fp);
        return -1;
    }

    fclose(fp);
    *value = (uint64_t)tmp;
    return 0;
}

static uint32_t crc32_update(uint32_t crc, const uint8_t *buf, size_t len)
{
    size_t index;

    crc = ~crc;
    for (index = 0; index < len; index++) {
        int bit;

        crc ^= buf[index];
        for (bit = 0; bit < 8; bit++) {
            if (crc & 1)
                crc = (crc >> 1) ^ 0xEDB88320U;
            else
                crc >>= 1;
        }
    }

    return ~crc;
}

static int read_full(int fd, void *buf, size_t len)
{
    uint8_t *p = (uint8_t *)buf;
    size_t done = 0;

    while (done < len) {
        ssize_t n = read(fd, p + done, len - done);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (n == 0) {
            return -1;
        }
        done += (size_t)n;
    }
    return 0;
}

/* Load a file into a malloc'd buffer.  On success, *buf_out points to the
   malloc'd buffer (caller must free()) and *sz_out is the file size.
   On error, *buf_out is NULL and returns -1.  */
static int load_file_to_buffer(const char *path, uint8_t **buf_out,
                               size_t *sz_out, size_t max_sz)
{
    int fd = -1;
    struct stat st;
    uint8_t *buf = NULL;

    *buf_out = NULL;
    *sz_out = 0;

    fd = open(path, O_RDONLY);
    if (fd < 0) {
        perror(path);
        goto fail;
    }

    if (fstat(fd, &st) < 0) {
        perror(path);
        goto fail;
    }

    if (st.st_size <= 0) {
        fprintf(stderr, "%s: file is empty\n", path);
        goto fail;
    }

    if ((uint64_t)st.st_size > max_sz) {
        fprintf(stderr, "%s: file too large (%lld bytes, max %llu)\n",
                path, (long long)st.st_size, (unsigned long long)max_sz);
        goto fail;
    }

    buf = (uint8_t *)malloc((size_t)st.st_size);
    if (!buf) {
        perror("malloc");
        goto fail;
    }

    if (read_full(fd, buf, (size_t)st.st_size) != 0) {
        fprintf(stderr, "%s: read failed\n", path);
        goto fail;
    }

    close(fd);
    *buf_out = buf;
    *sz_out = (size_t)st.st_size;
    return 0;

fail:
    if (fd >= 0)
        close(fd);
    free(buf);
    return -1;
}

/* infinite loop wait, no timeout */
static void wait_reg_eq_forever(volatile uint32_t *reg, uint32_t expect)
{
    while (*reg != expect) {
        usleep(1000);
    }
}

/* -------------------------------------------------------------------- */
/* notify_ep_dma_info: write DMA info to IPC registers on given device  */
/* -------------------------------------------------------------------- */

static int notify_ep_dma_info(ep_context_t *ep, uint64_t phys_addr,
                               uint64_t file_size, uint64_t file_crc)
{
    ipc_dma_regs_t regs = ipc_get_dma_regs(ep);

    printf("Notify EP %s: addr=0x%016llx size=0x%016llx crc=0x%016llx magic=0x%08x\n",
           ep->name,
           (unsigned long long)phys_addr,
           (unsigned long long)file_size,
           (unsigned long long)file_crc,
           EP_DMA_MAGIC);

    ipc_write_u64(regs.addr_low, regs.addr_high, phys_addr);
    ipc_write_u64(regs.size_low, regs.size_high, file_size);
    ipc_write_u64(regs.crc_low,  regs.crc_high,  file_crc);
    *regs.trigger = EP_DMA_MAGIC;
    __sync_synchronize();
    printf("ipc dma trigger: 0x%x\n", *regs.trigger);

    return 0;
}

/* ------------------------------------------------------------------ */
/* cmd_image_load: Load multi-firmware image via BAR2 on given device */
/* ------------------------------------------------------------------ */

static int cmd_image_load(ep_context_t *ep, const char *fw_file)
{
    int ret = 1;
    uint8_t *file_buf = NULL;
    size_t file_size = 0;
    ipc_cmd_regs_t regs;

    image_header_t *img;
    size_t img_off;
    uint16_t fw_cnt;
    uint16_t i;

    if (!fw_file)
        fw_file = FW_FILE;

    if (load_file_to_buffer(fw_file, &file_buf, &file_size,
                            IMAGE_FILE_MAX_SIZE) != 0)
        goto out;

    if (file_size < sizeof(image_header_t)) {
        fprintf(stderr, "file too small for image_header_t\n");
        goto out;
    }

    img = (image_header_t *)file_buf;
    if (img->magic != IMAGE_HEADER_MAGIC) {
        fprintf(stderr, "bad image magic: 0x%08x\n", img->magic);
        goto out;
    }

    fw_cnt = img->firmware_count;
    if (fw_cnt == 0 || fw_cnt > CONFIG_FW_MAX_COUNT) {
        fprintf(stderr, "unsupported firmware_count=%u (max=%u)\n", fw_cnt, CONFIG_FW_MAX_COUNT);
        goto out;
    }

    regs = ipc_get_fw_ipc_regs(ep);

    printf("EP %s: image load chip_index=%d IPC=BAR2+0x%x\n",
           ep->name, ep->chip_index,
           BAR2_IPC_OFFSET + BAR2_IPC_REG_OFFSET);

    img_off = sizeof(image_header_t);

    for (i = 0; i < fw_cnt; i++) {
        uint32_t fw_sz = img->size[i];
        firmware_header_t *fw_hdr;
        uint64_t fw_addr_to_send;

        if (fw_sz < sizeof(firmware_header_t)) {
            fprintf(stderr, "fw[%u] invalid size=%u\n", i, fw_sz);
            goto out;
        }
        if (img_off + fw_sz > file_size) {
            fprintf(stderr, "fw[%u] out of file range\n", i);
            goto out;
        }

        fw_hdr = (firmware_header_t *)(file_buf + img_off);

        if (fw_hdr->type != FW_TYPE_MCU && fw_hdr->type != FW_TYPE_HSM) {
            fprintf(stderr, "fw[%u] unsupported type=0x%08x\n", i, fw_hdr->type);
            goto out;
        }
        if (fw_hdr->size != fw_sz) {
            fprintf(stderr, "fw[%u] size mismatch hdr=%u img=%u\n", i, fw_hdr->size, fw_sz);
            goto out;
        }
        if (fw_hdr->load_addr != img->load_addr[i]) {
            fprintf(stderr, "fw[%u] load_addr mismatch hdr=0x%llx img=0x%llx\n",
                    i, (unsigned long long)fw_hdr->load_addr,
                    (unsigned long long)img->load_addr[i]);
            goto out;
        }

        fw_addr_to_send = img->load_addr[i];

        if (fw_addr_to_send < EP_FW_SRAM_BASE ||
            fw_addr_to_send >= EP_FW_SRAM_END ||
            fw_sz > EP_FW_SRAM_END - fw_addr_to_send) {
            fprintf(stderr, "fw[%u] BAR2 SRAM overflow addr=0x%llx size=0x%x\n",
                    i, (unsigned long long)fw_addr_to_send, fw_sz);
            goto out;
        }

        memcpy((void *)((volatile uint8_t *)ep->bar2 +
                        (fw_addr_to_send - EP_FW_SRAM_BASE)),
               (const void *)(file_buf + img_off), fw_sz);
        wmb();

        printf("Ensure PCIE_IPC_REG_COMMAND (EP) is 0.\n");
        wait_reg_eq_forever(regs.cmd, 0);

        ipc_write_u64(regs.data0, regs.data1, fw_addr_to_send);
        __sync_synchronize();

        *regs.cmd = IPC_CMD_FW;
        __sync_synchronize();

        printf("wait for ep clear PCIE_IPC_REG_COMMAND\n");
        wait_reg_eq_forever(regs.cmd, 0);

        printf("fw[%u] sent: type=0x%08x size=0x%x load_addr=0x%llx entry_addr=0x%llx status=0x%08x\n",
               i, fw_hdr->type, fw_sz,
               (unsigned long long)fw_hdr->load_addr,
               (unsigned long long)fw_hdr->entry_addr,
               *regs.status);

        img_off += fw_sz;
    }

    ret = 0;

out:
    free(file_buf);
    return ret;
}

/* ------------------------------------------------------------------ */
/* fw-load: Load firmware via BAR2 SRAM window, configure IPC         */
/* ------------------------------------------------------------------ */

static int cmd_fw_load(ep_context_t *ep, const char *fw_file)
{
    int ret = 1;
    uint8_t *buffer = NULL;
    size_t file_size = 0;
    void *target;
    firmware_header_t *fw_hdr;
    uint64_t fw_addr;
    uint64_t bar2_offset;
    ipc_cmd_regs_t regs;

    if (!fw_file)
        fw_file = FW_FILE;

    if (load_file_to_buffer(fw_file, &buffer, &file_size, BAR2_FW_SIZE) != 0)
        goto out;

    printf("EP %s: Firmware file: %s (%zu bytes)\n",
           ep->name, fw_file, file_size);

    if (file_size < sizeof(*fw_hdr)) {
        fprintf(stderr, "Firmware file is too small for its header\n");
        goto out;
    }

    fw_hdr = (firmware_header_t *)buffer;
    fw_addr = fw_hdr->load_addr;
    if (fw_hdr->size != file_size || fw_addr < EP_FW_SRAM_BASE ||
        fw_addr > EP_FW_SRAM_END || file_size > EP_FW_SRAM_END - fw_addr) {
        fprintf(stderr, "Invalid firmware load range: addr=0x%016llx size=%zu\n",
                (unsigned long long)fw_addr, file_size);
        goto out;
    }

    bar2_offset = fw_addr - EP_FW_SRAM_BASE;
    target = (uint8_t *)ep->bar2 + bar2_offset;
    memcpy(target, buffer, file_size);
    wmb();
    printf("EP %s: Firmware written to BAR2+0x%llx (EP 0x%016llx)\n",
           ep->name, (unsigned long long)bar2_offset,
           (unsigned long long)fw_addr);

    regs = ipc_get_fw_ipc_regs(ep);

    printf("Before: DATA0=0x%08x, DATA1=0x%08x\n", *regs.data0, *regs.data1);
    ipc_write_u64(regs.data0, regs.data1, fw_addr);
    *regs.cmd = IPC_CMD_FW;
    __sync_synchronize();
    printf("EP %s: IPC configured: DATA0=0x%08x, DATA1=0x%08x, CMD=0x%02x\n",
           ep->name, *regs.data0, *regs.data1, *regs.cmd);

    /* Verify first word */
    {
        uint32_t first_word;
        memcpy(&first_word, target, sizeof(first_word));
        printf("Firmware first word: 0x%08x\n", first_word);
    }

    ret = 0;

out:
    free(buffer);
    return ret;
}

/* ------------------------------------------------------------------ */
/* file-load: Host initiated file load to DMA buffer, notify EP       */
/* ------------------------------------------------------------------ */

static int cmd_file_load(ep_context_t *ep, const char *arg)
{
    uint8_t *file_buf = NULL;
    size_t file_size = 0;
    uint64_t file_crc;
    uint64_t dma_buf_crc;

    if (!arg || !strlen(arg)) {
        fprintf(stderr, "Usage: file-load <file>\n");
        return 1;
    }

    if (load_file_to_buffer(arg, &file_buf, &file_size, ep->dma_size) != 0)
        return 1;

    printf("EP %s: file=%s, size=%zu, dma_phys=0x%016llx, dma_buf_size=%llu\n",
           ep->name, arg, file_size,
           (unsigned long long)ep->dma_phys,
           (unsigned long long)ep->dma_size);

    file_crc = (uint64_t)crc32_update(0, file_buf, file_size);
    memcpy(ep->dma_buf, file_buf, file_size);
    wmb();

    dma_buf_crc = (uint64_t)crc32_update(0, (const uint8_t *)ep->dma_buf,
                                         file_size);
    free(file_buf);
    file_buf = NULL;

    printf("EP %s: Copied %zu bytes into DMA buffer, src_crc32=0x%08llx dma_buf_crc32=0x%08llx\n",
           ep->name, file_size,
           (unsigned long long)file_crc,
           (unsigned long long)dma_buf_crc);

    if (notify_ep_dma_info(ep, ep->dma_phys, (uint64_t)file_size, file_crc) != 0)
        return 1;

    return 0;
}

/* ---------------------------------------------------------------------- */
/* Helper: signal device and wait for result (blocks until done/timeout)  */
/* Returns 0 on success, 1 on timeout (device not ready)                  */
/* timeout_s: max seconds to wait for EP_RESULT_DONE                      */
/* ---------------------------------------------------------------------- */
static int recv_one_result(ep_context_t *ep, int timeout_s)
{
    ipc_result_regs_t regs = ipc_get_result_regs(ep);
    uint8_t *rx_buf = (uint8_t *)ep->dma_buf;
    uint64_t rx_phys = ep->dma_phys;
    uint64_t rx_size = ep->dma_size;
    uint32_t recived_magic = 0;
    int elapsed = 0;

    if (!rx_buf || rx_phys == 0 || rx_size < 8) {
        fprintf(stderr, "EP %s: invalid DMA receive buffer\n", ep->name);
        return 1;
    }

    memset(rx_buf, 0, (size_t)rx_size);
    ipc_write_u64(regs.addr_low, regs.addr_high, rx_phys);
    *regs.trigger = EP_RESULT_MAGIC;
    __sync_synchronize();

    /* Poll for EP_RESULT_DONE with timeout */
    while (elapsed < timeout_s) {
        rmb();
        memcpy(&recived_magic, rx_buf, sizeof(recived_magic));
        if (recived_magic == EP_RESULT_DONE)
            return 0;
        sleep(1);
        elapsed++;
    }

    fprintf(stderr, "Timeout after %ds waiting for result (last magic=0x%08x)\n",
            timeout_s, recived_magic);
    return 1;
}

/* ------------------------------------------------------------------ */
/* get-result: receive results in a loop until device stops           */
/* ------------------------------------------------------------------ */
static int cmd_get_result(ep_context_t *ep)
{
    int result_count = 0;
    uint8_t *rx_buf = (uint8_t *)ep->dma_buf;
    uint64_t rx_phys = ep->dma_phys;
    uint64_t rx_size = ep->dma_size;

    if (!rx_buf || rx_phys == 0 || rx_size < 8) {
        fprintf(stderr, "EP %s: DMA buffer is not ready for get-result\n", ep->name);
        return 1;
    }

    printf("EP %s: Result buffer uses DMA coherent memory at %p, phys=0x%016llx size=%llu\n",
           ep->name, rx_buf,
           (unsigned long long)rx_phys,
           (unsigned long long)rx_size);

    while (1) {
        printf("\n--- Waiting for result #%d (EP %s) ---\n", result_count + 1, ep->name);
        if (recv_one_result(ep, 30 * 60) != 0) {
            //printf("Device not ready, receive complete.\n");
            //break;
            continue;
        }

        uint32_t rslt_size;
        uint32_t recived_magic;
        memcpy(&recived_magic, rx_buf, sizeof(recived_magic));
        rslt_size = *((uint32_t *)rx_buf + 1);

        printf("Result #%d: magic=0x%08x size=0x%x\n", result_count + 1,
               recived_magic, rslt_size);
        {
            uint32_t display_len = rslt_size < BUF_DISPLAY ? rslt_size : BUF_DISPLAY;
            if (rslt_size > rx_size - 8)
                display_len = BUF_DISPLAY;
            for (uint32_t i = 0; i < display_len; i++) {
                printf("0x%02x ", *(rx_buf + 8 + i));
            }
            if (rslt_size > display_len)
                printf("... (%u more bytes)", rslt_size - display_len);
        }
        printf("\n");
        result_count++;
    }

    printf("\nTotal results received: %d\n", result_count);
    return 0;
}

/* ------------------------------------------------------------------ */
/* list-devices command                                               */
/* ------------------------------------------------------------------ */

static void cmd_list_devices(void)
{
    printf("Discovered endpoints: %d\n", ep_count);
    for (int i = 0; i < ep_count; i++) {
        ep_context_t *ep = &ep_list[i];
        printf("  %s: BDF=%s minor=%d chip_index=%d %s",
               ep->name, ep->bdf, ep->minor, ep->chip_index,
               ep->active ? "[active]" : "[inactive]");
        if (ep == current_ep)
            printf("  <<< current");
        printf("\n");
    }
    if (ep_count == 0)
        printf("  (none -- is the driver loaded?)\n");
}

/* ------------------------------------------------------------------ */
/* main                                                               */
/* ------------------------------------------------------------------ */

static void print_help(void)
{
    printf("Available commands:\n");
    printf("  fw-load [firmware_file]       - Load firmware via PCIe\n");
    printf("  img-load [image_file]         - Load multi-firmware image via PCIe\n");
    printf("  file-load [file]              - Load file to DMA buffer and notify EP\n");
    printf("  get-result                    - Receive results until device stops\n");
    printf("  set-device <spec>             - Switch PCIe device (epN or BDF)\n");
    printf("  list-devices                  - List discovered PCIe endpoints\n");
    printf("  help                          - Show help\n");
    printf("  quit                          - Exit the program\n");
    printf("\nDevice specs: ep0, ep1, ... or BDF string\n");
}

/* Dispatch a command. Returns 0 on success, 1 on error, -1 for quit/exit. */
static int dispatch_command(const char *cmd, const char *arg)
{
    if (strcmp(cmd, "fw-load") == 0)
        return cmd_fw_load(current_ep, strlen(arg) ? arg : NULL);
    else if (strcmp(cmd, "img-load") == 0)
        return cmd_image_load(current_ep, strlen(arg) ? arg : NULL);
    else if (strcmp(cmd, "file-load") == 0)
        return cmd_file_load(current_ep, arg);
    else if (strcmp(cmd, "get-result") == 0)
        return cmd_get_result(current_ep);
    else if (strcmp(cmd, "set-device") == 0) {
        if (!strlen(arg))
            fprintf(stderr, "Usage: set-device <spec> (epN or BDF)\n");
        else
            set_device(arg);
        return 0;
    } else if (strcmp(cmd, "list-devices") == 0) {
        cmd_list_devices();
        return 0;
    } else if (strcmp(cmd, "help") == 0) {
        print_help();
        return 0;
    } else if (strcmp(cmd, "quit") == 0 || strcmp(cmd, "exit") == 0) {
        return -1;
    } else {
        printf("Unknown command: %s. Type 'help' for available commands.\n", cmd);
        return 1;
    }
}

static int parse_line(char *line, char *cmd, char *arg)
{
    char *tok;
    cmd[0] = arg[0] = '\0';

    /* Skip leading spaces */
    while (*line == ' ' || *line == '\t') line++;
    if (*line == '\0' || *line == '\n') return 0;

    /* Extract command */
    tok = line;
    while (*tok != ' ' && *tok != '\t' && *tok != '\n' && *tok != '\0') tok++;
    size_t len = (size_t) (tok - line);
    if (len >= (size_t)(sizeof(char) * MAX_CMD)) len = MAX_CMD - 1;
    memcpy(cmd, line, len);
    cmd[len] = '\0';

    /* Skip to argument */
    while (*tok == ' ' || *tok == '\t') tok++;
    if (*tok == '\n') *tok = '\0';
    if (*tok){
        /* Skip trailing newline */
        char *nl = strchr(tok, '\n');
        if (nl) *nl = '\0';
        size_t arg_len = strlen(tok);
        if (arg_len >= MAX_CMD) arg_len = MAX_CMD - 1;
        memcpy(arg, tok, arg_len);
        arg[arg_len] = '\0';
    }

    return 1;
}


int main(int argc, char *argv[])
{
    char line[MAX_LINE];
    char cmd[MAX_CMD];
    char arg[MAX_CMD];

    if ((argc >= 2) && ((strcmp(argv[1], "--help") == 0) || (strcmp(argv[1], "-h") == 0))) {
        print_help();
        return 0;
    }

    /* Discover endpoints from driver sysfs */
    if (discover_endpoints() <= 0) {
        fprintf(stderr, "Primary discovery failed, trying fallback scan\n");
        if (discover_endpoints_fallback() <= 0) {
            fprintf(stderr, "No PCIe endpoints found.\n");
            return 1;
        }
    }
    printf("Discovered %d PCIe endpoint(s)\n", ep_count);

    /* Open all discovered endpoints */
    ep_open_all();

    /* Default: select first active endpoint */
    current_ep = NULL;
    for (int i = 0; i < ep_count; i++) {
        if (ep_list[i].active) {
            set_current_ep(&ep_list[i]);
            break;
        }
    }

    /* Parse --device flag from argv */
    int cmd_start = 1;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--device") == 0 && i + 1 < argc) {
            if (set_device(argv[i + 1]) != 0) {
                fprintf(stderr, "Failed to set device: %s\n", argv[i + 1]);
                ep_close_all();
                return 1;
            }
            argc--;
            memmove(&argv[i], &argv[i + 2], (argc - i) * sizeof(char *));
            argv[argc] = NULL;
            i--;
        } else {
            cmd_start = i;
            break;
        }
    }

    if (!current_ep) {
        fprintf(stderr, "No active endpoint available\n");
        ep_close_all();
        return 1;
    }

    if (argc > cmd_start) {
        int rc;
        const char *cmd_arg = (argc > cmd_start + 1) ? argv[cmd_start + 1] : "";
        rc = dispatch_command(argv[cmd_start], cmd_arg);
        if (rc == -1)
            rc = 0;
        ep_close_all();
        return rc;
    }

    /* Interactive mode */
    printf("T100 test app [%s]. Type 'help' for available commands.\n", current_ep->name);

    while (1) {
        printf("ssv_test> ");
        fflush(stdout);

        if (!fgets(line, sizeof(line), stdin))
            break;
        if (!parse_line(line, cmd, arg))
            continue;

        if (dispatch_command(cmd, arg) == -1)
            break;
    }

    ep_close_all();
    printf("Bye.\n");
    return 0;
}
