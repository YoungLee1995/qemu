/* Internal P1 test fixture: existing Tensor instruction, mailbox task entry.
 * Not a replacement business runtime or host-demo file protocol. */
#include <stdint.h>
#define SHARED 0x4100100000UL
#define PACKET (SHARED + 0x100)
#define COPY (SHARED + 0x1000)
#define BYTES 33792
#define MAGIC 0x5041434bU
static volatile uint32_t *const shared = (void *)SHARED;
static volatile unsigned char *const packet = (void *)PACKET;
static volatile uint32_t *const mbox = (void *)0x18019000UL;
static void barrier(void) { asm volatile("fence rw,rw" ::: "memory"); }
static uint32_t get32(unsigned off) {
  uint32_t v = 0;
  for (unsigned i = 0; i < 4; ++i) v |= (uint32_t)packet[off+i] << (8*i);
  return v;
}
static void finish(unsigned slot) {
  *(volatile uint32_t *)(0x7c000010UL + slot*4) = 0;
  for (;;) asm volatile("nop");
}
#ifdef MCU
int main(void) {
  unsigned submitted = 0, last = 0, replies = 0;
  mbox[1] = 1; /* MCU bank0 channel0 */
  barrier(); shared[4] = 1; /* software MCU ready */
  for (;;) {
    if (!(mbox[0] & 1)) continue;
    barrier();
    unsigned id = get32(12), kind = get32(20), status = 0;
    if (get32(0) != MAGIC || get32(4) != 1 || get32(8) != 160 || get32(28)) status = 1;
    else if (get32(16)) status = 2;
    else if (kind != 0 && (kind != 1 || get32(24) != 0x8204000bU)) status = 3;
    else if (!id || id == 0xffffffffU || id <= last) status = 4;
    mbox[0] = 1; /* consume host pending, W1C */
    if (!status && kind == 1) {
      ++submitted;
      shared[0] = id; barrier();
      mbox[(2*0x68+8)/4] = id;
      mbox[(2*0x68+16)/4] = 1; /* MCU -> NPU0 bank2 channel0 */
      while (shared[1] != id) asm volatile("nop");
      barrier(); last = id;
    }
    shared[8] = status;
    shared[9] = submitted;
    shared[10] = !status && kind == 1 ? BYTES : 0;
    barrier(); shared[7] = id;
    barrier(); shared[11] = ++replies; /* publish response sequence after data */
    if (!status && kind == 0) {
      shared[0] = 0xffffffffU; barrier();
      mbox[(2*0x68+16)/4] = 1;
      finish(0);
    }
  }
}
#else
extern void execute_tensor(const void *args);
int main(void) {
  unsigned last = 0;
  mbox[(2*0x68+4)/4] = 1;
  barrier(); shared[5] = 1; /* software NPU ready */
  for (;;) {
    if (!(mbox[(2*0x68)/4] & 1)) continue;
    barrier(); unsigned id = shared[0];
    mbox[(2*0x68)/4] = 1;
    if (id == 0xffffffffU) finish(2);
    if (!id || id == last) continue;
    volatile uint64_t *local = (void *)0x10009200UL;
    volatile uint64_t *copy = (void *)COPY;
    for (unsigned i=0; i<BYTES/8; ++i) local[i] = 0;
    execute_tensor((const void *)(PACKET+32));
    for (unsigned i=0; i<BYTES/8; ++i) copy[i] = local[i];
    last = id; shared[2] += 1;
    barrier(); shared[1] = id;
  }
}
#endif
