/* P5 guest: original driver + C async communication library, real MMIO DMA. */
#define main p4_main
#include "p4-base.c"
#undef main
#include "zc_comm.h"
#include <time.h>
static uint64_t monotonic_ms(void)
{
    struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return (uint64_t)t.tv_sec*1000+t.tv_nsec/1000000;
}
int main(void)
{
    check(p4_main()==0,"P4 base");
    struct zc_queue queues[CARDS][2];
    for(unsigned i=0;i<CARDS;i++)for(unsigned q=0;q<2;q++)
        zc_queue_init(&queues[i][q],cards[i].bar+(0x380200+q*0x200)/4);
    /* Distinct source/target slots; both queues are admitted before progress. */
    for(unsigned round=0;round<3;round++) {
        struct zc_request *requests[CARDS][CARDS];
        for(unsigned i=0;i<CARDS;i++)for(unsigned dst=0;dst<CARDS;dst++) {
            if(i==dst)continue;
            unsigned off=0x10000+dst*0x1000;
            volatile uint32_t *src=cards[i].payload;
            for(unsigned j=0;j<520;j+=4) {
                uint32_t value=0;
                for(unsigned b=0;b<4;b++)value|=((i*31+dst*7+j+b+round)&255)<<(8*b);
                src[(off+j)/4]=value;
            }
            check(zc_submit(&queues[i][dst%2],0x20010000+off,
                            peer(dst)+i*0x1000,517,&requests[i][dst])==0,"async enqueue");
        }
        uint64_t deadline=monotonic_ms()+30000;
        unsigned pending;
        do {
            pending=0;
            for(unsigned i=0;i<CARDS;i++)for(unsigned q=0;q<2;q++) {
                zc_progress(&queues[i][q]); pending+=queues[i][q].count;
            }
            check(monotonic_ms()<deadline,"async deadline");
            if(pending)usleep(1000);
        } while(pending);
        for(unsigned i=0;i<CARDS;i++)for(unsigned dst=0;dst<CARDS;dst++) {
            if(i==dst)continue;
            check(requests[i][dst]->state==ZC_COMPLETE,"async completion");
            volatile uint32_t *out=cards[dst].payload;
            for(unsigned j=0;j<517;j++)check(((out[(i*0x1000+j)/4]>>(8*(j%4)))&255)==((i*31+dst*7+j+round)&255),"async bytes");
        }
        printf("P5_ASYNC_ROUND cards=%u round=%u pairs=%u PASS\n",CARDS,round,CARDS*(CARDS-1));
    }
    struct zc_queue *q=&queues[0][0];struct zc_request *r;
    for(unsigned i=0;i<ZC_QUEUE_DEPTH;i++)check(zc_submit(q,0x20010000,peer(1),128,&r)==0,"fill queue");
    check(zc_submit(q,0x20010000,peer(1),128,&r)==ZC_FULL,"bounded queue");
    zc_queue_reset(q);check(q->count==0,"reset queue");
    puts("P5_C_LIBRARY_PASS");return 0;
}
