BASE = ../../../p2/runtime
RISCV_PREFIX ?= riscv64-unknown-elf-
CC = $(RISCV_PREFIX)gcc
OBJCOPY = $(RISCV_PREFIX)objcopy
CFLAGS = -O2 -g -march=rv64gcv -mabi=lp64d -mcmodel=medany -msmall-data-limit=0 -ffreestanding -fno-builtin -fno-stack-protector -nostdlib -nostartfiles
all: mcu.bin npu.bin
mcu.elf: $(BASE)/start.S runtime.c $(BASE)/link.ld
	$(CC) $(CFLAGS) -DMCU -Wl,-T,$(BASE)/link.ld -Wl,--defsym=LOAD_BASE=0x40000000 -Wl,--build-id=none -o $@ $(BASE)/start.S runtime.c
npu.elf: $(BASE)/start.S runtime.c $(BASE)/dma.S $(BASE)/execute.S $(BASE)/link.ld
	$(CC) $(CFLAGS) -Wl,-T,$(BASE)/link.ld -Wl,--defsym=LOAD_BASE=0x4000000000 -Wl,--build-id=none -o $@ $(BASE)/start.S runtime.c $(BASE)/dma.S $(BASE)/execute.S
%.bin: %.elf
	$(OBJCOPY) -O binary $< $@
