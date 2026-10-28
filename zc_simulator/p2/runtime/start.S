.section .text.start,"ax"
.global _start
_start:
  csrw mie, zero
  li t0, 0x6600
  csrs mstatus, t0
  la sp, stack_top
  call main
1: j 1b

.section .bss
.balign 16
stack:
  .space 8192
stack_top:
