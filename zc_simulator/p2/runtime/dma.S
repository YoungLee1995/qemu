.section .text
.global dma_load
.global dma_store
dma_load:
  li t0,32
  vsetvli zero,t0,e32,m2,ta,ma
  vle32.v v0,(a0)
  # Original DMA load, param, 8bit, 2D, descriptor v0/v1.
  .word 0x4020060b
  .word 0xf0000e0f
  ret
dma_store:
  li t0,32
  vsetvli zero,t0,e32,m2,ta,ma
  vle32.v v0,(a0)
  .word 0x4030060b
  .word 0xf0000e0f
  ret
