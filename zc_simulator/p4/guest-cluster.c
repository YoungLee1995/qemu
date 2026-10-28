/* Internal P4 guest workload using the adapted original host-demo driver. */
#define _GNU_SOURCE
#include <dirent.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#define CARDS 8
#define N 1024
struct card {
    char path[512], node[256];
    uint64_t iova, base[3];
    volatile uint32_t *bar, *service, *payload, *ipc;
    volatile unsigned char *buf;
    unsigned task;
    uint64_t irq_before;
} cards[CARDS];
static void check(int v,const char *s){if(!v){printf("P4_FAIL %s\n",s);exit(1);}}
static uint64_t number(const char *path,int hex){
    unsigned long long v;FILE *f=fopen(path,"r");check(f!=NULL,path);
    check(fscanf(f,hex?"%llx":"%llu",&v)==1,"sysfs number");fclose(f);return v;
}
static volatile void *mapping(const char *path,size_t len){
    int fd=open(path,O_RDWR);check(fd>=0,path);
    void *m=mmap(NULL,len,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);
    check(m!=MAP_FAILED,"mmap");return m;
}
static void fence(void){__sync_synchronize();}
static void atu(unsigned i,uint64_t target){
    volatile uint32_t *r=cards[i].bar+0x300000/4;
    r[1]=0;r[0]=0;r[2]=0;r[3]=0x180;r[4]=0x1ffff;r[5]=target;r[6]=target>>32;
    fence();r[1]=1u<<31;fence();
}
static uint64_t peer(unsigned i){return cards[i].base[2]+0x20000000;}
static unsigned submit(unsigned i,unsigned mode,uint64_t src,uint64_t dst,unsigned n){
    volatile uint32_t *p=cards[i].service+64;
    p[1]=mode;p[2]=n;p[3]=src;p[4]=src>>32;p[5]=dst;p[6]=dst>>32;p[7]=0;
    fence();p[0]=++cards[i].task;return cards[i].task;
}
static void finish(unsigned i){
    volatile uint32_t *s=cards[i].service;
    for(unsigned j=0;j<120000&&s[11]!=cards[i].task;j++)usleep(1000);
    fence();
    if(s[11]!=cards[i].task||s[8])printf("P4_DIAG card=%u task=%u done=%u status=%u stage=%u irq=%u\n",i,cards[i].task,s[11],s[8],s[13],s[12]);
    check(s[11]==cards[i].task&&s[8]==0,"task completion/status");
}
static uint32_t crc32(volatile unsigned char *p,unsigned n){
    uint32_t crc=~0u;while(n--){crc^=*p++;for(unsigned i=0;i<8;i++)crc=(crc>>1)^((crc&1)?0xedb88320u:0);}return ~crc;
}
static void ipc_wait(volatile uint32_t *ipc){
    for(unsigned j=0;j<120000&&ipc[4];j++)usleep(1000);
    fence();check(ipc[4]==0,"file IPC status");
}
static int compare(const void *a,const void *b){return strcmp(((const struct card *)a)->path,((const struct card *)b)->path);}
int main(void){
    setbuf(stdout,NULL);DIR *d=opendir("/sys/bus/pci/devices");check(d!=NULL,"pci sysfs");
    struct dirent *e;unsigned count=0;char path[1024];
    while((e=readdir(d))){
        if(e->d_name[0]=='.')continue;
        snprintf(path,sizeof(path),"/sys/bus/pci/devices/%s/vendor",e->d_name);
        if(number(path,1)!=0x16c3)continue;
        check(count<CARDS,"too many cards");snprintf(cards[count++].path,sizeof(cards[0].path),"/sys/bus/pci/devices/%s",e->d_name);
    }
    closedir(d);check(count==CARDS,"eight EPs");qsort(cards,CARDS,sizeof(cards[0]),compare);
    for(unsigned i=0;i<CARDS;i++){
        struct card *c=&cards[i];char actual[PATH_MAX],expected[PATH_MAX];
        check(realpath(c->path,expected)!=NULL,"real EP path");
        d=opendir("/sys/class/test_dma");check(d!=NULL,"driver class");
        while((e=readdir(d))){
            if(e->d_name[0]=='.')continue;
            snprintf(path,sizeof(path),"/sys/class/test_dma/%s/device",e->d_name);
            if(realpath(path,actual)&&!strcmp(actual,expected)){snprintf(c->node,sizeof(c->node),"/dev/%s",e->d_name);break;}
        }
        closedir(d);check(c->node[0]!=0,"original driver bound node");
        snprintf(path,sizeof(path),"%s/phys_addr",c->path);c->iova=number(path,0);
        check(c->iova<0x100000000ULL,"coherent IOVA outside reserved peer domain");
        snprintf(path,sizeof(path),"%s/irq_count",c->path);c->irq_before=number(path,0);
        snprintf(path,sizeof(path),"%s/resource",c->path);FILE *f=fopen(path,"r");check(f!=NULL,"resources");
        for(unsigned bar=0;bar<6;bar++){
            unsigned long long start,end,flags;check(fscanf(f,"%llx %llx %llx",&start,&end,&flags)==3,"resource line");
            if(bar%2==0){unsigned k=bar/2;uint64_t sizes[]={0x400000,0x2000000,0x800000000ULL};
                check(start&&end-start+1==sizes[k],"real BAR sizes");c->base[k]=start;
                if(bar==4)check(start>=0x100000000ULL&&end<0x10000000000ULL,"peer domain coverage");
                printf("P4_BAR card=%u bar=%u start=%llx end=%llx flags=%llx\n",i,bar,start,end,flags);
            }
        }
        fclose(f);
        c->buf=mapping(c->node,2097152);
        snprintf(path,sizeof(path),"%s/resource0",c->path);c->bar=mapping(path,0x400000);
        snprintf(path,sizeof(path),"%s/resource4",c->path);volatile uint32_t *b=mapping(path,0x20020000);
        c->service=b+0x10000000/4;c->payload=b+0x20000000/4;c->ipc=b+0x18103180/4;
        for(unsigned j=0;j<6000&&c->bar[2]!=1;j++)usleep(10000);
        check(c->bar[2]==1,"real runtime ready");
        volatile uint32_t *r=c->bar+0x300100/4;uint64_t a=peer(i);
        r[1]=0;r[0]=0;r[2]=a;r[3]=a>>32;r[4]=a+0x1ffff;r[5]=0x20010000;r[6]=0;fence();r[1]=1u<<31;
        printf("P4_PROBE card=%u path=%s node=%s iova=%llx\n",i,c->path,c->node,(unsigned long long)c->iova);
    }
    for(unsigned i=0;i<CARDS;i++)for(unsigned j=0;j<i;j++)for(unsigned a=0;a<3;a++)for(unsigned b=0;b<3;b++){
        uint64_t sizes[]={0x400000,0x2000000,0x800000000ULL};
        check(cards[i].base[a]+sizes[a]<=cards[j].base[b]||cards[j].base[b]+sizes[b]<=cards[i].base[a],"BAR overlap");
    }
    for(unsigned mode=1;mode<=4;mode++){
        for(unsigned i=0;i<CARDS;i++)atu(i,cards[i].iova);
        for(unsigned i=0;i<CARDS;i++){
            for(unsigned j=0;j<N;j++){cards[i].buf[j]=(j*37+i+mode)&255;cards[i].buf[0x10000+j]=0;}
            submit(i,mode,cards[i].iova,cards[i].iova+0x10000,N);
        }
        for(unsigned i=0;i<CARDS;i++){
            finish(i);for(unsigned j=0;j<N;j++)check(cards[i].buf[0x10000+j]==(unsigned char)(((j*37+i+mode)&255)^0x5a),"Host all bytes");
            printf("P4_HOST card=%u mode=%u PASS\n",i,mode);
        }
    }
    for(unsigned mode=1;mode<=4;mode++)for(unsigned shift=1;shift<CARDS;shift++){
        for(unsigned i=0;i<CARDS;i++)atu(i,peer((i+shift)%CARDS));
        for(unsigned i=0;i<CARDS;i++){
            unsigned dst=(i+shift)%CARDS;
            for(unsigned j=0;j<N/4;j++){cards[dst].payload[j]=j*1234567u+i+mode;cards[dst].payload[0x10000/4+j]=0;}
            submit(i,mode,peer(dst),peer(dst)+0x10000,N);
        }
        for(unsigned i=0;i<CARDS;i++){
            unsigned dst=(i+shift)%CARDS;finish(i);
            for(unsigned j=0;j<N/4;j++)check(cards[dst].payload[0x10000/4+j]==((j*1234567u+i+mode)^0x5a5a5a5a),"peer all bytes");
            printf("P4_PEER source=%u target=%u mode=%u PASS\n",i,dst,mode);
        }
    }
    for(unsigned i=0;i<CARDS;i++){
        volatile uint32_t *ipc=cards[i].ipc;
        for(unsigned j=0;j<N;j++)cards[i].buf[j]=(j*17+i)&255;
        uint64_t a=cards[i].iova;ipc[0]=a;ipc[1]=a>>32;ipc[2]=N;ipc[3]=0;ipc[5]=crc32(cards[i].buf,N);ipc[6]=0;
        fence();ipc[4]=0x5a5aa5a5;ipc_wait(ipc);
        a+=0x10000;ipc[0]=a;ipc[1]=a>>32;fence();ipc[4]=0x72736c74;ipc_wait(ipc);
        check(*(volatile uint32_t *)(cards[i].buf+0x10000)==0x646f6e65,"file done");
        for(unsigned j=0;j<N;j++)check(cards[i].buf[0x10008+j]==(unsigned char)((j*17+i)&255),"file result bytes");
        snprintf(path,sizeof(path),"%s/irq_count",cards[i].path);
        for(unsigned j=0;j<1000&&number(path,0)==cards[i].irq_before;j++)usleep(1000);
        check(number(path,0)>cards[i].irq_before,"MSI-X completion");check(cards[i].service[12]>0,"MCU local IRQ");
        printf("P4_FILE_IRQ card=%u PASS\n",i);
    }
    puts("P4_LINUX_PASS cards=8 host_tasks=32 peer_tasks=224 directed_pairs=56 modes=4 file_ipc=8");return 0;
}
