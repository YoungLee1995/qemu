#!/usr/bin/env python3
"""Protocol, flow-control, timing and queue invariants with analytic oracles."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'simulator/tools/p5'))
from fabric import Fabric, LinkConfig
from communication import Communication


class Protocol(unittest.TestCase):
    def test_write_split_boundary_no_completion(self):
        f = Fabric(cards=2, mps=128, ordering='pci_relaxed')
        seen = []
        r = f.submit(0, 1, 4093, 1031, True, lambda p: seen.append((p.address, p.length)))
        out = f.run()
        self.assertEqual(r.state, 'complete')
        self.assertEqual(sum(n for _, n in seen), 1031)
        self.assertTrue(all(a // 4096 == (a+n-1) // 4096 and n <= 128 for a, n in seen))
        self.assertFalse(any(p.kind == 'CplD' for p in f.packets.values()))
        self.assertTrue(out['credits_balanced'])

    def test_read_tag_reuse_split_completion(self):
        f = Fabric(cards=2, mps=128, mrrs=512, tags=1, ordering='pci_relaxed')
        r = f.submit(0, 1, 4090, 1800, False)
        f.run()
        reads = [p for p in f.packets.values() if p.kind == 'MemRd']
        cpls = [p for p in f.packets.values() if p.kind == 'CplD']
        self.assertEqual(r.bytes_committed, 1800)
        self.assertEqual(r.state, 'complete')
        self.assertGreater(len(cpls), len(reads))
        self.assertTrue(all(p.tag == 0 for p in reads))
        self.assertEqual(f.free_tags[0], {0})
        self.assertFalse(f.tag_left)
        for a, b in zip(reads, reads[1:]):
            self.assertGreaterEqual(b.submitted_ns, max(p.end_ns for p in cpls if p.parent_packet == a.id))

    def test_requester_tag_mismatch_reject(self):
        f = Fabric(cards=2)
        r = f.submit(0, 1, 0, 128, False)
        f.run(until_ns=250)
        # Mutate a real generated completion while in transit.
        p = next(p for p in f.packets.values() if p.kind == 'CplD')
        p.tag = 999
        f.run()
        self.assertEqual(r.state, 'error')
        self.assertIn('tag mismatch', r.error)

    def test_credit_exhaustion_return(self):
        c = LinkConfig(header_credits=1, data_credits=8, credit_return_ns=500)
        f = Fabric(cards=2, mps=128, ordering='pci_relaxed', default=c)
        r = f.submit(0, 1, 0, 2048, True)
        out = f.run()
        self.assertEqual(r.state, 'complete')
        self.assertGreater(out['links']['d0:up']['credit_stall_ns'], 0)
        self.assertEqual(out['links']['d0:up']['max_in_flight'], 1)
        self.assertTrue(out['credits_balanced'])
        self.assertTrue(all(0 <= v <= 1 for v in [out['links'][k]['utilization'] for k in out['links']]))

    def test_blocked_port_timeout_no_late_write(self):
        f = Fabric(cards=2, links={'d1:down': LinkConfig(blocked_until_ns=10000)})
        seen = []
        r = f.submit(0, 1, 0, 256, True, lambda p: seen.append(p.id), timeout_ns=1000)
        f.run()
        self.assertEqual(r.state, 'error')
        self.assertEqual(seen, [])
        self.assertTrue(f.report()['credits_balanced'])

    def test_read_timeout(self):
        f = Fabric(cards=2, links={'d0:down': LinkConfig(blocked_until_ns=10000)})
        r = f.submit(0, 1, 0, 512, False, timeout_ns=1000)
        f.run()
        self.assertEqual(r.error, 'Completion timeout')
        self.assertEqual(f.free_tags[0], set(range(8)))

    def test_reset_isolation(self):
        f = Fabric(cards=8)
        a = f.submit(0, 1, 0, 2048, True)
        b = f.submit(2, 3, 0, 2048, True)
        f.cancel(1)
        f.run()
        self.assertEqual((a.state, b.state), ('error', 'complete'))
        self.assertEqual(a.bytes_committed, 0)
        self.assertTrue(f.report()['credits_balanced'])

    def test_error_callback_and_no_success(self):
        f = Fabric(cards=2)
        def bad(p):
            raise PermissionError('unmapped')
        r = f.submit(0, 1, 0, 256, True, bad)
        f.run()
        self.assertEqual(r.state, 'error')
        self.assertIn('unmapped', r.error)
        self.assertEqual(r.bytes_committed, 0)

    def test_analytic_single_packet_latency(self):
        c = LinkConfig(latency_ns=137)
        f = Fabric(cards=2, default=c)
        r = f.submit(0, 1, 0, 128, True)
        f.run()
        # Exactly two store-and-forward links, no queuing.
        self.assertEqual(r.finished_ns, 2 * (c.serialization(128) + 137))
        f = Fabric(cards=2, default=c)
        r = f.submit(0, 1, 0, 128, False)
        f.run()
        self.assertEqual(r.finished_ns, 2*(c.serialization(0)+137)+2*(c.serialization(128)+137))

    def test_bandwidth_scaling(self):
        times = []
        for gen in (3, 4, 5):
            f = Fabric(cards=2, default=LinkConfig(generation=gen, lanes=4, latency_ns=0), ordering='pci_relaxed')
            r = f.submit(0, 1, 0, 65536, True)
            f.run()
            times.append(r.finished_ns)
        self.assertGreater(times[0], times[1])
        self.assertGreater(times[1], times[2])

    def test_shared_uplink_competition(self):
        f = Fabric(cards=8, ordering='pci_relaxed')
        rs = [f.submit(i, -1, i*65536, 65536, True) for i in range(8)]
        f.run()
        shared = max(r.finished_ns for r in rs)
        f = Fabric(cards=8, ordering='pci_relaxed')
        rs = [f.submit(i, (i+1)%8, i*65536, 65536, True) for i in range(8)]
        f.run()
        independent = max(r.finished_ns for r in rs)
        self.assertGreater(shared, independent * 3)

    def test_arbitration_three_policies(self):
        for policy in ('round_robin', 'fixed_priority', 'weighted_round_robin'):
            c = LinkConfig(arbitration=policy, weights=(2, 1, 1, 1, 1, 1, 1, 1))
            f = Fabric(cards=8, default=c, ordering='pci_relaxed')
            for i in range(8):
                f.submit(i, -1, 0, 8192, True)
            f.run()
            owners = [e['requester'] for e in f.trace if e['link'] == 'host:up']
            self.assertEqual(set(owners), set(range(8)))
            if policy == 'fixed_priority':
                self.assertEqual(owners[:8], [0]*8)
            if policy == 'round_robin':
                self.assertEqual(owners[:8], list(range(8)))
            if policy == 'weighted_round_robin':
                self.assertEqual(owners[:9], [0, 0, 1, 2, 3, 4, 5, 6, 7])

    def test_ordering_modes_and_repeatability(self):
        for ordering in ('strict', 'pci_relaxed', 'delayed'):
            def run():
                f = Fabric(cards=2, ordering=ordering)
                f.submit(0, 1, 13, 2048, False)
                f.submit(1, 0, 91, 2048, True)
                return f.run()
            self.assertEqual(run(), run())
        f = Fabric(cards=2, ordering='strict')
        r = f.submit(0, 1, 0, 1024, True)
        f.run()
        ps = list(f.packets.values())
        self.assertTrue(all(a.end_ns <= b.submitted_ns for a, b in zip(ps, ps[1:])))

    def test_configuration_and_message_transactions(self):
        f = Fabric(cards=2)
        seen=[]
        a=f.control(0,1,'CfgRd',0,lambda p:seen.append(p.kind))
        b=f.control(0,1,'CfgWr',4,lambda p:seen.append(p.kind))
        c=f.control(1,-1,'Message',0,lambda p:seen.append(p.kind))
        f.run()
        self.assertTrue(all(r.state=='complete' for r in (a,b,c)))
        self.assertEqual(sorted(seen),['Cpl','CplD','Message'])
        self.assertEqual(sum(p.kind=='Cpl' for p in f.packets.values()),1)
        self.assertFalse(any(p.kind in ('Cpl','CplD') and p.request==c.id for p in f.packets.values()))
        self.assertTrue(f.report()['credits_balanced'])
        # Config Write contains 4 data bytes and consumes NP data credit;
        # its Completion has no data payload. Config Read has no request data.
        c=f.cfg('d0:up')
        write_packet=next(p for p in f.packets.values() if p.kind=='CfgWr')
        read_packet=next(p for p in f.packets.values() if p.kind=='CfgRd')
        self.assertEqual(write_packet.end_ns,read_packet.end_ns+c.serialization(4))

    def test_invalid_configuration_and_request(self):
        for kw in ({'mps': 7}, {'tags': 0}, {'default': LinkConfig(data_credits=1)}):
            with self.assertRaises(ValueError):
                Fabric(**kw)
        f = Fabric(cards=2, max_pending=1)
        with self.assertRaises(ValueError):
            f.submit(0, 0, 0, 1, True)
        with self.assertRaises(ValueError):
            f.submit(0, 1, (1 << 64) - 2, 4, True)
        f.submit(0, 1, 0, 1, True)
        with self.assertRaises(BufferError):
            f.submit(1, 0, 0, 1, True)


class MemoryAdapter:
    def __init__(self):
        self.data = [bytearray(0x20000) for _ in range(8)]
        self.buffers = {}
    def prepare(self, t, data): self.buffers[t.sequence] = bytearray(data or bytes(t.length))
    def address(self, t): return t.offset
    def transfer(self, t, p):
        off = p.address - t.offset
        if t.write:
            self.data[t.target][p.address:p.address+p.length] = self.buffers[t.sequence][off:off+p.length]
        else:
            self.buffers[t.sequence][off:off+p.length] = self.data[t.target][p.address:p.address+p.length]
    def result(self, t): return bytes(self.data[t.target][t.offset:t.offset+t.length] if t.write else self.buffers[t.sequence])
    def reset(self, card): pass


class Queues(unittest.TestCase):
    def test_multiqueue_full_wrap_read_and_barrier(self):
        a = MemoryAdapter()
        c = Communication(a, Fabric(cards=8), depth=2)
        for round in range(5):
            jobs = [c.send(0, 1, bytes([i+round])*128, i*128, queue=i%2) for i in range(4)]
            with self.assertRaises(BufferError):
                c.send(0, 1, b'x', 8192)
            c.barrier()
            for i, t in enumerate(jobs):
                self.assertEqual(c.wait(t), bytes([i+round])*128)
        r = c.read(2, 1, 128)
        self.assertEqual(c.wait(r), bytes([4])*128)
        self.assertEqual(len({t.sequence for t in c.history}), 21)

    def test_overlap_reset_reuse(self):
        c = Communication(MemoryAdapter(), Fabric(cards=8))
        t = c.send(0, 1, b'a'*128)
        with self.assertRaises(BufferError):
            c.send(2, 1, b'b'*128)
        other = c.send(2, 3, b'c'*128)
        c.reset(0)
        self.assertEqual(t.state, 'error')
        self.assertEqual(c.wait(other), b'c'*128)
        new = c.send(0, 1, b'd'*128)
        self.assertEqual(c.wait(new), b'd'*128)


class Packages(unittest.TestCase):
    def test_package_hash_and_path_validation(self):
        import tempfile
        from run_tasks import package
        from tasks import TaskPackage
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'task'
            package(path)
            t=TaskPackage(path)
            self.assertEqual(len(t.expected),33792)
            spec=json.loads((path/'task.json').read_text())
            (path/'input.bin').write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError,'digest mismatch'):
                TaskPackage(path)
            spec['input']='../outside.bin'
            (path.parent/'outside.bin').write_bytes(b'x')
            spec['sha256']['../outside.bin']='dummy'
            (path/'task.json').write_text(json.dumps(spec))
            with self.assertRaisesRegex(ValueError,'inside package'):
                TaskPackage(path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
