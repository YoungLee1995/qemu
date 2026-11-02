# Original Host PCIe demo sources

The original ssv_test_app.c and ssv_test_driver.c were copied from the former
sibling simulator/host-demo project. They are now supplied by this QEMU
repository. No runtime download from that sibling repository is required.

The Ubuntu desktop uses the adapted driver in desktop/share/driver/ instead
of compiling this original driver against a modern kernel. Install and run
the original Host application with desktop/host_demo.py; the wrapper manages
its HugeTLB requirement. Device discovery is validated. The original file
transfer command protocol is not equivalent to the P5 task ABI and its
historical get-result roundtrip is not a passed acceptance test.
