OUTPUT_ARCH(riscv)
ENTRY(_start)
SECTIONS {
  . = LOAD_BASE;
  .text : { *(.text.start) *(.text*) }
  .rodata : { *(.rodata*) *(.srodata*) }
  .data : { *(.data*) *(.sdata*) }
  . = ALIGN(16);
  .bss : { *(.bss*) *(.sbss*) *(COMMON) }
}
