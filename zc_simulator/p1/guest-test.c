/* Internal P1 Linux test: shared mailbox -> MCU/NPU -> shared result. */
#define _GNU_SOURCE
#include <dirent.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <time.h>
static void check(int ok, const char *what) {
    if (!ok) { printf("P1_FAIL %s\n", what); exit(1); }
}
static uint64_t now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static void wait_value(volatile uint32_t *p, uint32_t value) {
    uint64_t deadline = now() + 30000;
    while (*p != value && now() < deadline) usleep(1000);
    check(*p == value, "bounded wait");
}
static void read_file(const char *p, void *data, size_t n) {
    int fd = open(p, O_RDONLY);
    check(fd >= 0 && read(fd, data, n) == (ssize_t)n, p); close(fd);
}
static unsigned number(const char *p) {
    FILE *f=fopen(p,"r"); unsigned v=0;
    if (f) { if (fscanf(f,"%x",&v)!=1) v=0; fclose(f); } return v;
}
int main(void) {
    DIR *dir=opendir("/sys/bus/pci/devices"); struct dirent *e;
    char dev[512]={0},path[600];
    check(dir!=NULL,"sysfs");
    while ((e=readdir(dir))) {
        snprintf(path,sizeof(path),"/sys/bus/pci/devices/%s/vendor",e->d_name);
        if (number(path)!=0x16c3) continue;
        snprintf(path,sizeof(path),"/sys/bus/pci/devices/%s/device",e->d_name);
        if (number(path)==0xabcd) snprintf(dev,sizeof(dev),"/sys/bus/pci/devices/%s",e->d_name);
    }
    closedir(dir); check(dev[0],"P1 PCI identity");
    snprintf(path,sizeof(path),"%s/resource",dev);
    FILE *f=fopen(path,"r"); check(f!=NULL,"resources");
    for (unsigned i=0;i<6;i++) {
        unsigned long long a,b,c;
        check(fscanf(f,"%llx %llx %llx",&a,&b,&c)==3,"BAR resource line");
        if (i==0||i==2||i==4) {
            uint64_t expected=i==0?0x400000ULL:i==2?0x2000000ULL:0x800000000ULL;
            check(a && b-a+1==expected,"BAR size assigned");
            printf("P1_BAR %u base=%llx size=%llu\n",i,a,b-a+1);
        }
    } fclose(f);
    snprintf(path,sizeof(path),"%s/iommu_group",dev);
    char group[512]; check(readlink(path,group,sizeof(group))>0,"IOMMU group");
    snprintf(path,sizeof(path),"%s/config",dev);
    int config=open(path,O_RDWR); uint16_t command=6;
    check(config>=0 && pwrite(config,&command,2,4)==2,"memory and bus master");
    for (unsigned i=0;i<6;i+=2) {
        uint32_t low,high;
        check(pread(config,&low,4,0x10+i*4)==4 &&
              pread(config,&high,4,0x14+i*4)==4,"BAR config read");
        check((low&15)==(i==4?12:4),"64-bit BAR type and prefetch");
        if(i==4) check(high!=0,"BAR4 above 4GiB");
    }
    unsigned char cap; check(pread(config,&cap,1,0x34)==1,"capabilities");
    unsigned found=0;
    for (unsigned n=0;cap && n<48;n++) {
        unsigned char bytes[2]; check(pread(config,bytes,2,cap)==2,"capability read");
        if (bytes[0]==0x11) found=cap;
        cap=bytes[1];
    }
    uint16_t flags; check(found && pread(config,&flags,2,found+2)==2,"MSI-X capability");
    check((flags&0x7ff)==31,"32 MSI-X vectors");
    volatile uint32_t *bar0,*bar2,*bar4;
    snprintf(path,sizeof(path),"%s/resource0",dev); int fd=open(path,O_RDWR);
    bar0=mmap(NULL,65536,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0); close(fd);
    snprintf(path,sizeof(path),"%s/resource2",dev);fd=open(path,O_RDWR);
    bar2=mmap(NULL,0x200000,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);
    snprintf(path,sizeof(path),"%s/resource4",dev);fd=open(path,O_RDWR);
    bar4=mmap(NULL,0x20000000,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);
    check(bar0!=MAP_FAILED&&bar2!=MAP_FAILED&&bar4!=MAP_FAILED,"BAR mmap");
    check(bar0[1]==2,"BAR layout profile version 2");
    wait_value(bar0+2,1);
    check(bar2[0]!=0,"firmware bytes visible via shared DDR0");
    bar2[0x1ff000/4]=0x1234abcd;
    check(bar2[0x1ff000/4]==0x1234abcd,"BAR DDR0 write");
    volatile uint32_t *mb=(void *)((char *)bar4+0x18019000);
    volatile uint32_t *c=(void *)((char *)bar4+0x10000000);
    /* Unused bank5 channel7: early SEND, INTEN, W1C and SEND reads. */
    volatile uint32_t *test=(void *)((char *)mb+5*0x68);
    test[1]=0; test[(16+7*12)/4]=1;
    check(test[0]==128 && test[(16+7*12)/4]==0,"early SEND latched");
    test[1]=128;check(test[0]==128,"enable preserves pending");
    test[0]=128;check(test[0]==0,"W1C");
    uint32_t args[32],golden[8448];
    read_file("/tensor-args.bin",args,sizeof(args));read_file("/legacy-result.bin",golden,sizeof(golden));
    unsigned cases[][6]={{1,160,0,1,0x8204000b,0},{2,160,0,1,0x8204000b,0},
        {2,160,0,1,0x8204000b,4},{0,160,0,1,0x8204000b,4},
        {3,159,0,1,0x8204000b,1},{4,160,4,1,0x8204000b,2},
        {5,160,0,1,0,3},{6,160,0,1,0x8204000b,0},{7,160,0,0,0,0}};
    unsigned executed=0;
    for(unsigned k=0;k<9;k++) {
        uint32_t packet[40]={0x5041434b,1,cases[k][1],cases[k][0],cases[k][2],cases[k][3],cases[k][4],0};
        memcpy(packet+8,args,sizeof(args));
        for(unsigned i=0;i<40;i++) c[0x100/4+i]=packet[i];
        __sync_synchronize();mb[2]=cases[k][0];mb[4]=1;
        /* id=0 and repeated id responses need a sequence discriminator. */
        wait_value(c+11,k+1);
        check(c[7]==cases[k][0]&&c[8]==cases[k][5],"response id/status");
        if(!cases[k][5]&&cases[k][3]) {
            executed++;
            check(c[12]==0x1234abcd && c[13]==0x1234abcd,"MCU and NPU see Host DDR0 write");
            check(c[10]==33792,"result length");
            for(unsigned i=0;i<8448;i++) check(c[0x1000/4+i]==golden[i],"full Tensor output");
        }
        check(c[9]==executed&&c[2]==executed,"execution count");
        printf("P1_TASK id=%u status=%u executed=%u\n",cases[k][0],c[8],c[2]);
    }
    close(config);
    printf("P1_PASS tensor=3 rejected=5 ready=software mailbox=shared DDR=shared\n");
    return 0;
}
