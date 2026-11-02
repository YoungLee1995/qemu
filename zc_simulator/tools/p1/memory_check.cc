// Internal shared_mem_t boundary/alias regression, linked to the actual model.
#include "devices.h"
#include <sys/mman.h>
#include <unistd.h>
#include <climits>
#include <cstdint>
#include <cstdio>
#include <stdexcept>

static void require(bool value, const char *name) {
  if (!value) throw std::runtime_error(name);
}
int main() {
  for (reg_t size : {reg_t(1) << 30, reg_t(1) << 38}) {
    int fd = memfd_create("p1-memory-check", MFD_CLOEXEC);
    require(fd >= 0 && ftruncate(fd, size) == 0, "memfd");
    {
      shared_mem_t first(fd, size), second(fd, size);
      uint8_t data[128], output[128];
      for (unsigned i = 0; i < sizeof(data); i++) data[i] = i * 37;
      for (reg_t start : {reg_t(0), reg_t(4093), size - sizeof(data)}) {
        require(first.store(start, sizeof(data), data), "valid store");
        require(second.load(start, sizeof(output), output), "valid load");
        require(memcmp(data, output, sizeof(data)) == 0, "same backing");
      }
      require(!first.store(size - 1, 2, data), "end overflow");
      require(!first.store(UINT64_MAX - 15, 32, data), "address overflow");
      require(!first.load(0, size + 1, output), "oversized load");
      require(!first.store(size + 1, 0, data), "out of range zero length");
      require(first.load(size, 0, output), "empty end read");
      first.contents(size - 1)[0] = 0x5a;
      require(second.load(size - 1, 1, output) && output[0] == 0x5a,
              "contents aliases load/store");
      bool threw = false;
      try { first.contents(size); } catch (const std::out_of_range &) { threw = true; }
      require(threw, "contents rejects end");
      printf("SHARED_MEMORY_PASS size=%llu\n", (unsigned long long)size);
    }
    close(fd);
  }
}
