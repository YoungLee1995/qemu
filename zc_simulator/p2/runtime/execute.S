.section .text
.global execute_tensor
execute_tensor:
  # Captured reference instruction uses v8/v9, SEW32, LMUL2, VL32.
  li t0, 32
  vsetvli zero, t0, e32, m2, ta, ma
  vle32.v v8, (a0)
  .word 0x8204000b
  # Existing enhanced fence waits for scheduler queues and devices to idle.
  .word 0xf0000e0f
  ret
