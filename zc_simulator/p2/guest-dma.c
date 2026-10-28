/* Real Linux coherent IOVA, MCU proxy and original NPU DMA integration. */
#define _GNU_SOURCE
#include <dirent.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
static void check(int v,const char *s){if(!v){printf("P2_FAIL %s\n",s);exit(1);}}
static uint64_t number(const char *p,int hex){
    unsigned long long v=0;FILE *f=fopen(p,"r");check(f!=NULL,p);
    check(fscanf(f,hex?"%llx":"%llu",&v)==1,"sysfs value");fclose(f);return v;
}
static volatile uint32_t *mapbar(const char *dev,unsigned bar,size_t n){
    char path[512];snprintf(path,sizeof(path),"%s/resource%u",dev,bar);
    int fd=open(path,O_RDWR);check(fd>=0,"open BAR");
    void *p=mmap(NULL,n,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);
    check(p!=MAP_FAILED,"map BAR");return p;
}
int main(void){
    char dev[512]={0},path[1024];struct dirent *e;DIR *d=opendir("/sys/bus/pci/devices");check(d!=NULL,"sysfs");
    while((e=readdir(d))){
        if(e->d_name[0]=='.')continue;
        snprintf(path,sizeof(path),"/sys/bus/pci/devices/%s/vendor",e->d_name);
        if(number(path,1)==0x16c3){snprintf(dev,sizeof(dev),"/sys/bus/pci/devices/%s",e->d_name);break;}
    }
    closedir(d);check(dev[0]!=0,"find EP");
    snprintf(path,sizeof(path),"%s/phys_addr",dev);uint64_t iova=number(path,0);
    int fd=open("/dev/test_dma_0",O_RDWR);check(fd>=0,"coherent node");
    volatile unsigned char *buf=mmap(NULL,2097152,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);check(buf!=MAP_FAILED,"coherent mmap");
    volatile uint32_t *bar=mapbar(dev,0,4<<20),*service=mapbar(dev,4,0x10001000);
    service+=0x10000000/4;
    for(unsigned i=0;i<3000&&bar[2]!=1;i++)usleep(10000);
    check(bar[2]==1,"software ready");
    volatile uint32_t *atu=bar+0x300000/4;
    atu[0]=0;atu[2]=0;atu[3]=0x180;atu[4]=0x1ffff;atu[8]=0x180;
    atu[5]=iova;atu[6]=iova>>32;__sync_synchronize();atu[1]=1u<<31;
    unsigned n=12345;
    snprintf(path,sizeof(path),"%s/irq_count",dev);uint64_t irq_before=number(path,0);
    printf("P2_IOVA=%llx\n",(unsigned long long)iova);
    for(unsigned id=1;id<=5;id++){
        unsigned mode=id==5?1:id;
        for(unsigned i=0;i<n;i++){buf[i]=(i*37+id)&255;buf[0x10000+i]=0;}
        volatile uint32_t *p=service+0x100/4;
        p[1]=mode;p[2]=n;p[3]=iova;p[4]=iova>>32;p[5]=iova+0x10000;p[6]=(iova+0x10000)>>32;
        __sync_synchronize();p[0]=id;
        for(unsigned i=0;i<10000&&service[11]!=id;i++)usleep(1000);
        __sync_synchronize();check(service[11]==id,"task deadline");check(service[8]==0,"task status");
        for(unsigned i=0;i<n;i++)check(buf[0x10000+i]==(unsigned char)(((i*37+id)&255)^0x5a),"whole output");
        if(id==1)check(bar[0x380000/4]==0&&bar[0x380100/4]==0,"NPU independent of HDMA");
        printf("P2_TASK mode=%u id=%u bytes=%u PASS\n",mode,id,n);
    }
    for(unsigned i=0;i<500&&number(path,0)==irq_before;i++)usleep(1000);
    check(number(path,0)>irq_before,"real completion MSI-X");
    puts("P2_LINUX_PASS tasks=5 modes=4 coherent_iommu=1 msix=1");
    return 0;
}
