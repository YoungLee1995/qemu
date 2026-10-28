/* Internal guest checks for the locally adapted toolchain demo module. */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <dirent.h>
#include <string.h>
static void check(int v,const char *s){if(!v){printf("HOST_PROBE_FAIL %s\n",s);exit(1);}}
static uint64_t read_number(const char *p,int hex){
    FILE *f=fopen(p,"r");unsigned long long n=0;
    check(f!=NULL,p);check(fscanf(f,hex?"%llx":"%llu",&n)==1,"number");fclose(f);return n;
}
int main(void){
    DIR *d=opendir("/sys/bus/pci/devices");struct dirent *e;char p[512],dev[512]={0};
    check(d!=NULL,"sysfs");
    while((e=readdir(d))){
        if(e->d_name[0]=='.')continue;
        snprintf(p,sizeof(p),"/sys/bus/pci/devices/%s/vendor",e->d_name);
        if(read_number(p,1)!=0x16c3)continue;
        snprintf(dev,sizeof(dev),"/sys/bus/pci/devices/%s",e->d_name);
        break;
    }closedir(d);check(dev[0],"demo device");
    snprintf(p,sizeof(p),"%s/buf_size",dev);check(read_number(p,0)==2097152,"coherent size");
    snprintf(p,sizeof(p),"%s/phys_addr",dev);uint64_t iova=read_number(p,0);check(iova!=0,"DMA address");
    int fd=open("/dev/test_dma_0",O_RDWR);check(fd>=0,"character node");
    volatile uint32_t *dma=mmap(NULL,2097152,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
    check(dma!=MAP_FAILED,"coherent mmap");dma[0]=0x1234abcd;dma[524287]=0x87654321;
    check(dma[0]==0x1234abcd&&dma[524287]==0x87654321,"coherent edge read/write");
    snprintf(p,sizeof(p),"%s/resource0",dev);int b=open(p,O_RDWR);check(b>=0,"BAR0");
    volatile uint32_t *bar=mmap(NULL,65536,PROT_READ|PROT_WRITE,MAP_SHARED,b,0);check(bar!=MAP_FAILED,"BAR0 mmap");
    /* Enumeration/probe may finish before the backend publishes ready. */
    for(unsigned n=0;n<3000 && bar[2]!=1;n++)usleep(10000);
    check(bar[2]==1,"backend software ready before IRQ injection");
    snprintf(p,sizeof(p),"%s/irq_count",dev);uint64_t before=read_number(p,0);
    bar[8]=0;__sync_synchronize();
    for(unsigned n=0;n<500&&read_number(p,0)==before;n++)usleep(10000);
    check(read_number(p,0)==before+1,"actual MSI-X handler");
    /* Mask vector31, latch PBA, then unmask and observe exactly one handler. */
    volatile uint32_t *v31=bar+(0x2000+31*16)/4;
    before=read_number(p,0);v31[3]=1;__sync_synchronize();bar[8]=31;
    __sync_synchronize();usleep(20000);
    check(read_number(p,0)==before,"masked vector not delivered");
    check(bar[0x3000/4]&(1u<<31),"masked PBA");
    v31[3]=0;__sync_synchronize();
    for(unsigned n=0;n<500&&read_number(p,0)==before;n++)usleep(10000);
    check(read_number(p,0)==before+1,"unmask delivers pending IRQ");
    check(!(bar[0x3000/4]&(1u<<31)),"unmask clears PBA");
    unsigned vectors=0;snprintf(p,sizeof(p),"%s/msi_irqs",dev);d=opendir(p);check(d!=NULL,"msi_irqs");
    while((e=readdir(d))) {
        if(e->d_name[0]!='.')vectors++;
    }
    closedir(d);
    check(vectors>=1&&vectors<=32,"vector count");
    munmap((void*)bar,65536);close(b);munmap((void*)dma,2097152);close(fd);
    printf("HOST_PROBE_PASS coherent=2097152 iova=%llx vectors=%u irq=2 mask_unmask=pass\n",(unsigned long long)iova,vectors);return 0;
}
