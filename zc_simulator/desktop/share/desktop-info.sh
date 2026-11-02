#!/bin/sh
printf 'Ubuntu 24.04 / q35 / TCG / 8-card platform

'
lspci -nn -d 16c3:abcd
printf '
Driver nodes:
'
ls /dev/test_dma_*
printf '
Shared files: /mnt/hostshare
Test: sudo /opt/t100/guest-cluster
'
printf '
Validated: Host DMA 32, peer DMA 224, async 3 x 56 PASS
'
exec bash
