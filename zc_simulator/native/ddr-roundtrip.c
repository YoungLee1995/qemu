/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Guest-only standalone Host -> inbound -> DDR write/read diagnostic. */
#define _DEFAULT_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>
static void check(int ok,const char *msg) { if(!ok) { fprintf(stderr,"DDR_FAIL %s\n",msg); exit(1); } }
static volatile uint32_t *map(const char *bdf,int bar,size_t length) {
    char path[512];snprintf(path,sizeof(path),"/sys/bus/pci/devices/%s/resource%d",bdf,bar);
    int fd=open(path,O_RDWR|O_SYNC);check(fd>=0,path);
    void *p=mmap(NULL,length,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);
    check(p!=MAP_FAILED,"mmap");return p;
}
int main(int argc,char **argv) {
    check(argc==2,"usage: sudo ./ddr-roundtrip 0000:03:00.0");setbuf(stdout,NULL);
    volatile uint32_t *bar=map(argv[1],0,0x400000);
    check(bar[0]==0x5a435031 && bar[2]==1,"EP/transport not ready");
    char path[512];snprintf(path,sizeof(path),"/sys/bus/pci/devices/%s/resource",argv[1]);
    FILE *f=fopen(path,"r");check(f!=NULL,"resource");
    unsigned long long base=0,end=0,flags=0;
    for(int i=0;i<5;i++)check(fscanf(f,"%llx %llx %llx",&base,&end,&flags)==3,"BAR table");
    fclose(f);check(base && end-base+1>=0x20020000,"BAR4 size");
    volatile uint32_t *all=map(argv[1],4,0x20020000),*data=all+0x20018000/4;
    /* Reserved diagnostic DDR0 page. Stop other users before changing region 0. */
    uint64_t bus=base+0x20000000;
    volatile uint32_t *r=bar+0x300100/4;uint32_t saved[9];
    for(int i=0;i<9;i++)saved[i]=r[i];
    r[1]=0;__sync_synchronize();
    r[0]=0;r[2]=bus;r[3]=bus>>32;r[4]=bus+0x1ffff;r[5]=0x20010000;r[6]=0;r[8]=0;
    __sync_synchronize();r[1]=1u<<31;__sync_synchronize();
    unsigned failures=0;
    for(unsigned round=0;round<3;round++) {
        uint32_t expected[16];
        for(unsigned i=0;i<16;i++) {
            expected[i]=(0x12345678u+i*0x01020304u)^(round*0x55555555u);
            data[i]=expected[i];
            printf("actor=host pid=%ld op=write bar4_offset=0x%x device_addr=0x%x value=0x%08x\n",
                   (long)getpid(),0x20018000+i*4,0x20028000+i*4,expected[i]);
        }
        __sync_synchronize();
        for(unsigned i=0;i<16;i++) {
            uint32_t actual=data[i];
            printf("actor=host pid=%ld op=read bar4_offset=0x%x device_addr=0x%x value=0x%08x expected=0x%08x %s\n",
                   (long)getpid(),0x20018000+i*4,0x20028000+i*4,actual,expected[i],actual==expected[i]?"PASS":"FAIL");
            failures+=actual!=expected[i];
        }
    }
    r[1]=0;__sync_synchronize();for(int i=0;i<9;i++)if(i!=1)r[i]=saved[i];
    __sync_synchronize();r[1]=saved[1];__sync_synchronize();
    munmap((void *)all,0x20020000);munmap((void *)bar,0x400000);
    printf("DDR_ROUNDTRIP_%s rounds=3 bytes_per_round=64 mismatches=%u\n",failures?"FAIL":"PASS",failures);
    return failures?1:0;
}
