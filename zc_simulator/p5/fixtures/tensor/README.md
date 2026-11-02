# Fixed Tensor regression inputs

Project-specific inputs formerly read from the sibling workspace: act_l1.bin,
wgt_l1.bin, wgt_scale_l1.bin, moe_index_l1.bin from the original
moe_bf16_sint4_33x29x34_fix/data fixture; parameters.bin from the firmware
packet_runtime_test/tensor-args.bin; expected.bin from the original validated
single-die Spike output. These fixtures allow run_tasks.py to run from a QEMU-only
checkout. They are test data, not downloaded software or generated desktop disks.
