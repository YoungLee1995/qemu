/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Run as root inside QEMU Linux. Default P5 MCU+NPU image protocol only. */
#define _DEFAULT_SOURCE
#include <dirent.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
static void check(int ok,const char *message)
{
    if(!ok) { fprintf(stderr,"HOST_PCIE_FAIL: %s\n",message); exit(1); }
}
static volatile uint32_t *map(const char *device,unsigned bar,size_t size)
{
    char path[512]; snprintf(path,sizeof(path),"/sys/bus/pci/devices/%s/resource%u",device,bar);
    int fd=open(path,O_RDWR|O_SYNC); check(fd>=0,path);
    void *p=mmap(NULL,size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0); close(fd);
    check(p!=MAP_FAILED,"mmap PCI BAR"); return p;
}
int main(int argc,char **argv)
{
    check(argc==2,"usage: sudo ./host-test PCI_BDF (example: 0000:03:00.0)");
    setbuf(stdout,NULL);
    volatile uint32_t *bar=map(argv[1],0,0x400000);
    check(bar[0]==0x5a435031,"EP identity");
    for(unsigned i=0;i<6000 && bar[2]!=1;i++) usleep(10000);
    check(bar[2]==1,"transport connected (60 second timeout)");
    /* Discover BAR4's assigned PCI address; configure inbound window to DDR0. */
    char path[512]; snprintf(path,sizeof(path),"/sys/bus/pci/devices/%s/resource",argv[1]);
    FILE *f=fopen(path,"r"); check(f!=NULL,"resource table");
    unsigned long long base=0,end=0,flags=0;
    for(unsigned i=0;i<5;i++) check(fscanf(f,"%llx %llx %llx",&base,&end,&flags)==3,"BAR resource");
    fclose(f); check(base && end-base+1>=0x20020000,"BAR4 assigned");
    uint64_t address=base+0x20000000;
    volatile uint32_t *atu=bar+0x300100/4;
    atu[1]=0; atu[0]=0; atu[2]=address; atu[3]=address>>32;
    atu[4]=address+0x1ffff; atu[5]=0x20010000; atu[6]=0;
    __sync_synchronize(); atu[1]=1u<<31; __sync_synchronize();
    volatile uint32_t *bar4=map(argv[1],4,0x20020000);
    volatile uint32_t *service=bar4+0x10000000/4;
    volatile uint32_t *payload=bar4+0x20000000/4;
    for(unsigned iteration=1;iteration<=3;iteration++) {
        for(unsigned j=0;j<256;j++) { payload[j]=0x10203040u+j*37+iteration; payload[1024+j]=0; }
        volatile uint32_t *task=service+64;
        unsigned id=service[11]+1;
        task[1]=5; task[2]=1024; task[3]=0x20010000; task[4]=0;
        task[5]=0x20011000; task[6]=0; task[7]=0;
        __sync_synchronize(); task[0]=id; __sync_synchronize();
        for(unsigned j=0;j<60000 && service[11]!=id;j++) usleep(1000);
        __sync_synchronize();
        check(service[11]==id && !service[8],"MCU/NPU task completion/status");
        for(unsigned j=0;j<256;j++) check(payload[1024+j]==(payload[j]^0x5a5a5a5au),"NPU result bytes");
        printf("HOST_PCIE_TASK %u PASS (1024 bytes, same Spike process)\n",iteration);
    }
    munmap((void *)bar4,0x20020000); munmap((void *)bar,0x400000);
    return 0;
}
