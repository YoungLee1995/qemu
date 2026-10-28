/* P2 integration fixture, not a production runtime/file IPC ABI. */
#include <stdint.h>
#define SHARED 0x4100100000UL
#define DDR 0x4000000000UL
#define INPUT (DDR+0x200000)
#define OUTPUT (DDR+0x210000)
#define EXTERNAL 0x18000000000UL
static volatile uint32_t *const c=(void *)SHARED;
static void fence(void){asm volatile("fence rw,rw":::"memory");}
#ifdef MCU
static int hdma(int read,uint64_t src,uint64_t dst,unsigned n){
    volatile uint32_t *r=(void *)(0x21380000UL+(read?0x100:0));
    r[0]=1;r[0x8c/4]=0xffffffff;r[0x34/4]=0x202;
    r[0x1c/4]=n;r[8]=src;r[9]=src>>32;r[10]=dst;r[11]=dst>>32;
    fence();r[1]=1;
    while(r[0x80/4]!=3)asm volatile("nop");
    return r[0x84/4]==1;
}
int main(void){
    unsigned last=0;
    fence();c[4]=1;
    for(;;){
        unsigned id=c[64],mode=c[65],n=c[66];
        if(!id||id==last)continue;
        fence();last=id;
        uint64_t src=((uint64_t)c[68]<<32)|c[67];
        uint64_t dst=((uint64_t)c[70]<<32)|c[69];
        unsigned status=0;
        if(mode<1||mode>4||!n||n>32768)status=1;
        if(!status&&(mode==2||mode==3)&&!hdma(1,src,INPUT,n))status=2;
        if(!status){
            c[0]=id;fence();
            while(c[1]!=id)asm volatile("nop");
            fence();
            if((mode==2||mode==4)&&!hdma(0,OUTPUT,dst,n))status=3;
        }
        c[8]=status;c[7]=id;fence();c[11]=id;
    }
}
#else
extern void dma_load(const void *);
extern void dma_store(const void *);
static void descriptor(uint64_t *d,uint64_t addr,unsigned n){
    for(unsigned i=0;i<16;i++)d[i]=0;
    d[1]=addr-DDR; /* existing relative-address descriptor mode */
    d[2]=0x100000;
    d[3]=(uint64_t)n<<32;d[4]=1;
    d[5]=d[6]=d[8]=1|((uint64_t)n<<16)|(1UL<<32)|(1UL<<48);
}
int main(void){
    unsigned last=0;uint64_t d[16] __attribute__((aligned(16)));
    fence();c[5]=1;
    for(;;){
        unsigned id=c[0];if(!id||id==last)continue;
        fence();unsigned mode=c[65],n=c[66];
        descriptor(d,(mode==1||mode==4)?EXTERNAL:INPUT,n);
        dma_load(d); /* original queue, DMA engine and enhanced fence */
        volatile unsigned char *p=(void *)0x10100000UL;
        for(unsigned i=0;i<n;i++)p[i]^=0x5a;
        descriptor(d,(mode==1||mode==3)?EXTERNAL+0x10000:OUTPUT,n);
        dma_store(d);
        fence();last=id;c[1]=id;
    }
}
#endif
