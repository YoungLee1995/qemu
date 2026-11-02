/*
 * ssv_test_driver.c - T100 PCIe host test driver
 *
 * Device: VID=0x16c3, DID=0xabcd
 *
 * Supports one or more PCIe endpoint devices. Each device gets:
 *   - Its own DMA coherent buffer (exported via PCI sysfs)
 *   - A /dev/test_dma_<N> character node (mmap-able)
 *   - Up to 32 MSI-X vectors
 *
 * Build: make -C /lib/modules/$(uname -r)/build M=$(pwd) modules
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/pci.h>
#include <linux/interrupt.h>
#include <linux/dma-mapping.h>
#include <linux/slab.h>
#include <linux/init.h>
#include <linux/fs.h>
#include <linux/mm.h>
#include <linux/pfn.h>
#include <linux/kdev_t.h>
#include <linux/cdev.h>
#include <linux/mutex.h>
#include <linux/list.h>

#define MY_VENDOR_ID   0x16c3
#define MY_DEVICE_ID   0xabcd

#define DMA_BUFFER_SIZE  (2 * 1024 * 1024)
#define MAX_MSIX_VECTORS 32

#define CLASS_NAME  "test_dma"
#define MAX_DEVICES 16

/* ------------------------------------------------------------------ */
/* Per-device state                                                   */
/* ------------------------------------------------------------------ */

struct per_dev {
    struct list_head    node;
    struct pci_dev     *pdev;

    void       *dma_virt;
    dma_addr_t  dma_handle;
    size_t      dma_size;

    int         num_vectors;
    struct msix_entry msix_entries[MAX_MSIX_VECTORS];

    struct cdev    cdev;
    struct device *devnode;
    dev_t          devno;
    int            minor;
};

static struct class          *dma_class;
static dev_t                 base_devno;
static DEFINE_MUTEX(dev_lock);
static LIST_HEAD(dev_list);
static atomic_t next_minor = ATOMIC_INIT(0);

/* ------------------------------------------------------------------ */
/* Interrupt handler                                                  */
/* ------------------------------------------------------------------ */

static irqreturn_t ssv_msix_handler(int irq, void *dev_id)
{
    dev_info(dev_id, "MSI-X interrupt (IRQ %d)\n", irq);
    return IRQ_HANDLED;
}

/* ------------------------------------------------------------------ */
/* PCI sysfs attributes (under /sys/bus/pci/devices/<bdf>/)           */
/* ------------------------------------------------------------------ */

static ssize_t phys_addr_show(struct device *dev,
                              struct device_attribute *attr, char *buf)
{
    struct pci_dev    *pdev = to_pci_dev(dev);
    struct per_dev    *sd   = pci_get_drvdata(pdev);

    return scnprintf(buf, PAGE_SIZE, "%llu\n", (unsigned long long)sd->dma_handle);
}

static ssize_t buf_size_show(struct device *dev,
                             struct device_attribute *attr, char *buf)
{
    struct pci_dev    *pdev = to_pci_dev(dev);
    struct per_dev    *sd   = pci_get_drvdata(pdev);

    return scnprintf(buf, PAGE_SIZE, "%zu\n", (size_t)sd->dma_size);
}

static DEVICE_ATTR_RO(phys_addr);
static DEVICE_ATTR_RO(buf_size);

/* ------------------------------------------------------------------ */
/* Character device: open + mmap                                      */
/* ------------------------------------------------------------------ */

static int dma_buf_open(struct inode *inode, struct file *file)
{
    struct per_dev    *sd   = container_of(inode->i_cdev, struct per_dev, cdev);

    file->private_data = sd;
    return 0;
}

static int dma_buf_mmap(struct file *file, struct vm_area_struct *vma)
{
    struct per_dev    *sd = file->private_data;
    unsigned long      sz = vma->vm_end - vma->vm_start;
/*
    unsigned long      pfn;

    if (sz > sd->dma_size)
        return -EINVAL;

    pfn = PHYS_PFN(sd->dma_handle);
    vma->vm_page_prot = pgprot_noncached(vma->vm_page_prot);

    return remap_pfn_range(vma, vma->vm_start, pfn, sz, vma->vm_page_prot);
*/

    if (sz > sd->dma_size)
        return -EINVAL;

    return dma_mmap_coherent(&sd->pdev->dev, vma, sd->dma_virt,
                             sd->dma_handle, sd->dma_size);
}

static const struct file_operations dma_buf_fops = {
    .owner = THIS_MODULE,
    .open  = dma_buf_open,
    .mmap  = dma_buf_mmap,
};

/* ------------------------------------------------------------------ */
/* Helper: find per_dev by minor                                      */
/* ------------------------------------------------------------------ */
/* Probe                                                              */
/* ------------------------------------------------------------------ */

static int ssv_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
    struct per_dev *sd;
    int ret, i, nirqs = 0;

    sd = kzalloc(sizeof(*sd), GFP_KERNEL);
    if (!sd)
        return -ENOMEM;

    sd->pdev = pdev;
    pci_set_drvdata(pdev, sd);

    ret = pci_enable_device(pdev);
    if (ret)
        goto err_free;
    pci_set_master(pdev);

    ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(64));
    if (ret) {
        ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(32));
        if (ret) {
            dev_err(&pdev->dev, "failed to set DMA mask\n");
            goto err_pci;
        }
        dev_warn(&pdev->dev, "falling back to 32-bit DMA mask\n");
    }

    /* DMA buffer -- zeroed by dma_alloc_coherent */
    sd->dma_virt = dma_alloc_coherent(&pdev->dev, DMA_BUFFER_SIZE,
                                      &sd->dma_handle, GFP_KERNEL);
    if (!sd->dma_virt) {
        dev_err(&pdev->dev, "DMA alloc failed\n");
        ret = -ENOMEM;
        goto err_pci;
    }
    sd->dma_size = DMA_BUFFER_SIZE;
    dev_info(&pdev->dev, "DMA: virt=%p, phys=%pa\n",
             sd->dma_virt, &sd->dma_handle);

    /* MSI-X */
    for (i = 0; i < MAX_MSIX_VECTORS; i++)
        sd->msix_entries[i].entry = i;

    ret = pci_enable_msix_range(pdev, sd->msix_entries, 1, MAX_MSIX_VECTORS);
    if (ret < 0) {
        dev_err(&pdev->dev, "MSI-X failed (%d)\n", ret);
        goto err_dma;
    }
    sd->num_vectors = ret;

    for (i = 0; i < sd->num_vectors; i++) {
        ret = request_irq(sd->msix_entries[i].vector, ssv_msix_handler, 0,
                          "ssv_msix", pdev);
        if (ret) {
            dev_err(&pdev->dev, "IRQ %d failed at vector %d\n",
                    sd->msix_entries[i].vector, i);
            goto err_irq;
        }
        nirqs++;
    }

    /* sysfs: create custom attributes on the already-registered PCI device */
    ret = device_create_file(&pdev->dev, &dev_attr_phys_addr);
    if (ret)
        dev_warn(&pdev->dev, "failed to create phys_attr (%d)\n", ret);
    ret = device_create_file(&pdev->dev, &dev_attr_buf_size);
    if (ret)
        dev_warn(&pdev->dev, "failed to create buf_size attr (%d)\n", ret);

    /* character device -- pick next minor from our reserved range */
    sd->minor = atomic_inc_return(&next_minor) - 1;
    if (sd->minor >= MAX_DEVICES) {
        dev_err(&pdev->dev, "Exceeded max devices (%d)\n", MAX_DEVICES);
        ret = -ENOSPC;
        atomic_dec(&next_minor);
        goto err_sysfs;
    }
    sd->devno = MKDEV(MAJOR(base_devno), sd->minor);

    cdev_init(&sd->cdev, &dma_buf_fops);
    ret = cdev_add(&sd->cdev, sd->devno, 1);
    if (ret) {
        dev_err(&pdev->dev, "cdev_add failed\n");
        goto err_sysfs;
    }

    sd->devnode = device_create(dma_class, &pdev->dev, sd->devno, NULL,
                                "test_dma_%d", sd->minor);
    if (IS_ERR(sd->devnode)) {
        dev_err(&pdev->dev, "device_create failed\n");
        ret = PTR_ERR(sd->devnode);
        goto err_cdev;
    }

    mutex_lock(&dev_lock);
    list_add_tail(&sd->node, &dev_list);
    mutex_unlock(&dev_lock);

    dev_info(&pdev->dev, "loaded -> /dev/test_dma_%d  (%d MSI-X vectors)\n",
             sd->minor, sd->num_vectors);
    return 0;

err_cdev:
    cdev_del(&sd->cdev);
    atomic_dec(&next_minor);
err_sysfs:
    device_remove_file(&pdev->dev, &dev_attr_phys_addr);
    device_remove_file(&pdev->dev, &dev_attr_buf_size);
err_irq:
    while (nirqs-- > 0)
        free_irq(sd->msix_entries[nirqs].vector, pdev);
    if (sd->num_vectors > 0)
        pci_disable_msix(pdev);
err_dma:
    dma_free_coherent(&pdev->dev, sd->dma_size,
                      sd->dma_virt, sd->dma_handle);
err_pci:
    pci_disable_device(pdev);
err_free:
    kfree(sd);
    return ret;
}

/* ------------------------------------------------------------------ */
/* Remove                                                             */
/* ------------------------------------------------------------------ */

static void ssv_remove(struct pci_dev *pdev)
{
    struct per_dev *sd = pci_get_drvdata(pdev);
    int i;

    mutex_lock(&dev_lock);
    list_del(&sd->node);
    mutex_unlock(&dev_lock);

    device_destroy(dma_class, sd->devno);
    cdev_del(&sd->cdev);
    atomic_dec(&next_minor);

    device_remove_file(&pdev->dev, &dev_attr_phys_addr);
    device_remove_file(&pdev->dev, &dev_attr_buf_size);

    for (i = 0; i < sd->num_vectors; i++)
        free_irq(sd->msix_entries[i].vector, pdev);
    if (sd->num_vectors > 0)
        pci_disable_msix(pdev);

    dma_free_coherent(&pdev->dev, sd->dma_size,
                      sd->dma_virt, sd->dma_handle);
    pci_disable_device(pdev);

    kfree(sd);
    dev_info(&pdev->dev, "removed\n");
}

/* ------------------------------------------------------------------ */
/* PCI table + driver                                                 */
/* ------------------------------------------------------------------ */

static const struct pci_device_id ssv_ids[] = {
    { PCI_DEVICE(MY_VENDOR_ID, MY_DEVICE_ID) },
    { },
};
MODULE_DEVICE_TABLE(pci, ssv_ids);

static struct pci_driver ssv_pci_driver = {
    .name     = "ssv_test",
    .id_table = ssv_ids,
    .probe    = ssv_probe,
    .remove   = ssv_remove,
};
/* ------------------------------------------------------------------ */
/* Module init / exit                                                 */
/* ------------------------------------------------------------------ */

static int __init ssv_module_init(void)
{
    int ret;

    dma_class = class_create(THIS_MODULE, CLASS_NAME);
    if (IS_ERR(dma_class))
        return PTR_ERR(dma_class);

    ret = alloc_chrdev_region(&base_devno, 0, MAX_DEVICES, CLASS_NAME);
    if (ret) {
        class_destroy(dma_class);
        return ret;
    }

    ret = pci_register_driver(&ssv_pci_driver);
    if (ret) {
        unregister_chrdev_region(base_devno, MAX_DEVICES);
        class_destroy(dma_class);
        return ret;
    }

    return 0;
}

static void __exit ssv_module_exit(void)
{
    pci_unregister_driver(&ssv_pci_driver);
    unregister_chrdev_region(base_devno, MAX_DEVICES);
    class_destroy(dma_class);
}

module_init(ssv_module_init);
module_exit(ssv_module_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Linden");
MODULE_DESCRIPTION("SSV PCIe test driver (DMA + MSI-X, multi-device)");
