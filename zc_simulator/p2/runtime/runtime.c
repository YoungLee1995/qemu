/* P2 integration fixture, not a production runtime/file IPC ABI. */
#include <stdint.h>
#define SHARED 0x4100100000UL
#define DDR 0x4000000000UL
#define INPUT (DDR+0x200000)
#define OUTPUT (DDR+0x210000)
#define FILE_BUFFER (DDR+0x400000)
#define EXTERNAL 0x18000000000UL
static volatile uint32_t *const c=(void *)SHARED;
static void fence(void){asm volatile("fence rw,rw":::"memory");}
#ifdef MCU
volatile unsigned irq_count, irq_status;
void local_irq(void){

    volatile uint32_t *w=(void *)0x21380000UL,*r=(void *)0x21380100UL;
    unsigned status=w[0x84/4]|r[0x84/4];
    if(!status){*(volatile unsigned char *)(0x21000UL+60*4)=0;return;}
    irq_status=status;
    w[0x8c/4]=w[0x84/4];r[0x8c/4]=r[0x84/4];
    *(volatile unsigned char *)(0x21000UL+60*4)=0;
    ++irq_count;c[12]=irq_count;
}
extern void irq_entry(void);
static void irq_init(void){
    volatile unsigned char *irq=(void *)(0x20000UL+0x1000+60*4);
    asm volatile("csrw mtvec,%0"::"r"(irq_entry));
    irq[2]=0;irq[3]=255;irq[1]=1;
    asm volatile("csrsi mstatus,8");
}

static int hdma(int read,uint64_t src,uint64_t dst,unsigned n){
    volatile uint32_t *r=(void *)(0x21380000UL+(read?0x100:0));
    r[0]=1;r[0x8c/4]=0xffffffff;r[0x34/4]=0x202;
    r[0x1c/4]=n;r[8]=src;r[9]=src>>32;r[10]=dst;r[11]=dst>>32;
    c[13]=10+read;
    unsigned before=irq_count;
    r[0x88/4]=(1<<4)|(1<<6);
    fence();r[1]=1;
    unsigned polls=0;
    while(irq_count==before){
        if(++polls==1000000u){r[0]=0;return 0;}
        asm volatile("nop");
    }
    c[13]=20+read;c[14]=irq_status;
    return (irq_status&5)==1;

}
static uint32_t crc32(const volatile unsigned char *p,unsigned n){
    uint32_t crc=~0u;
    while(n--){crc^=*p++;for(unsigned i=0;i<8;i++)crc=(crc>>1)^((crc&1)?0xedb88320u:0);}
    return ~crc;
}
static void file_ipc(void){
    volatile uint32_t *ipc=(void *)0x18103180UL;
    static unsigned valid_size;
    unsigned trigger=ipc[4];
    if(trigger!=0x5a5aa5a5&&trigger!=0x72736c74)return;
    uint64_t addr=((uint64_t)ipc[1]<<32)|ipc[0];
    unsigned n=ipc[2],status=0;
    if(trigger==0x5a5aa5a5){
        valid_size=0;
        if(ipc[3]||!n||n>(2097152-8)||ipc[6])status=1;
        else if(!hdma(1,addr,FILE_BUFFER,n))status=2;
        else if(crc32((void *)FILE_BUFFER,n)!=ipc[5])status=3;
        else {valid_size=n;}
    }else{
        if(!valid_size)status=4;
        else{
            volatile uint32_t *header=(void *)(FILE_BUFFER-8);
            header[0]=0;header[1]=valid_size;
            if(!hdma(0,FILE_BUFFER-8,addr,valid_size+8))status=2;
            else{header[0]=0x646f6e65;fence();if(!hdma(0,FILE_BUFFER-8,addr,4))status=2;}
        }
    }
    ipc[4]=status?(0x80000000u|status):0;fence();
}
int main(void){
    irq_init();
    unsigned last=0;
    fence();c[4]=1;
    for(;;){
        file_ipc();
        unsigned id=c[64],mode=c[65],n=c[66];
        if(!id||id==last)continue;
        fence();last=id;
        uint64_t src=((uint64_t)c[68]<<32)|c[67];
        uint64_t dst=((uint64_t)c[70]<<32)|c[69];
        unsigned tensor=c[71],out_n=tensor?33792:n;
        unsigned status=0;
        if(mode<1||mode>4||!n||n>65536||tensor>1||
           (tensor&&n!=0x9200))status=1;
        if(!status&&(mode==2||mode==3)&&!hdma(1,src,INPUT,n))status=2;
        if(!status){
            c[0]=id;fence();
            while(c[1]!=id)asm volatile("nop");
            fence();
            status=c[9];
            if(!status&&(mode==2||mode==4)&&!hdma(0,OUTPUT,dst,out_n))status=3;
        }
        c[8]=status;c[7]=id;fence();c[11]=id;
    }
}
#else
extern void dma_load(const void *);
extern void dma_store(const void *);
extern void execute_tensor(const void *);
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
        fence();unsigned mode=c[65],n=c[66],tensor=c[71];
        volatile uint32_t *error=(void *)(0x21381000UL+16*4);
        *error=0;
        descriptor(d,(mode==1||mode==4)?EXTERNAL:INPUT,n);
        if(tensor)d[2]=0;
        dma_load(d); /* original queue, DMA engine and enhanced fence */
        if(*error){c[9]=4;fence();last=id;c[1]=id;continue;}
        if(tensor){
            volatile uint64_t *out=(void *)0x10009200UL;
            for(unsigned i=0;i<33792/8;i++)out[i]=0;
            execute_tensor((const void *)(SHARED+0x180));
            n=33792;
        }else{
            volatile unsigned char *p=(void *)0x10100000UL;
            for(unsigned i=0;i<n;i++)p[i]^=0x5a;
        }
        descriptor(d,(mode==1||mode==3)?EXTERNAL+0x10000:OUTPUT,n);
        if(tensor)d[2]=0x9200;
        dma_store(d);
        c[9]=*error?5:0;fence();last=id;c[1]=id;
    }
}
#endif
