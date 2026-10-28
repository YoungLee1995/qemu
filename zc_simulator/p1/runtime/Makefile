RISCV_PREFIX ?= /data/nuclei/gcc/bin/riscv64-unknown-elf-
CC = $(RISCV_PREFIX)gcc
OBJCOPY = $(RISCV_PREFIX)objcopy
CFLAGS = -O2 -g -march=rv64gcv -mabi=lp64d -mcmodel=medany -msmall-data-limit=0 -ffreestanding -fno-builtin -fno-stack-protector -nostdlib -nostartfiles

all: mcu.bin npu.bin

mcu.elf: start.S runtime.c link.ld
	$(CC) $(CFLAGS) -DMCU -Wl,-T,link.ld -Wl,--defsym=LOAD_BASE=0x40000000 -Wl,--build-id=none -o $@ start.S runtime.c

npu.elf: start.S runtime.c execute.S link.ld
	$(CC) $(CFLAGS) -Wl,-T,link.ld -Wl,--defsym=LOAD_BASE=0x4000000000 -Wl,--build-id=none -o $@ start.S runtime.c execute.S

%.bin: %.elf
	$(OBJCOPY) -O binary $< $@
