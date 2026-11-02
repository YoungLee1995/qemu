"""Deterministic PCIe transaction/credit model for the internal simulator.

Integer nanoseconds; no wall-clock sleeps. A delivery callback may execute the
actual QEMU DMA. Payload commits happen at destination delivery, not submission.
This is an explicit software model, not a cycle-accurate PCIe implementation.
"""
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
import heapq
import math


@dataclass(frozen=True)
class LinkConfig:
    generation: int = 4
    lanes: int = 16
    latency_ns: int = 100
    header_credits: int = 8
    data_credits: int = 256
    credit_return_ns: int = 100
    arbitration: str = 'round_robin'
    weights: tuple = (1,) * 8
    blocked_until_ns: int = 0

    def __post_init__(self):
        if self.generation not in (3, 4, 5) or self.lanes not in (1, 2, 4, 8, 16):
            raise ValueError('supported links: Gen3/4/5, x1/2/4/8/16')
        if min(self.latency_ns, self.credit_return_ns, self.blocked_until_ns) < 0:
            raise ValueError('negative latency')
        if self.header_credits < 1 or self.data_credits < 1:
            raise ValueError('credits must be positive')
        if self.arbitration not in ('round_robin', 'fixed_priority', 'weighted_round_robin'):
            raise ValueError('arbitration')
        if not self.weights or min(self.weights) < 1:
            raise ValueError('weights')

    @property
    def bytes_per_ns(self):
        # Gen3/4/5 use 128b/130b encoding; 8 bits per byte.
        return (8 * (2 ** (self.generation - 3))) * self.lanes * 128 / 130 / 8

    def serialization(self, payload_bytes):
        # Model overhead: 3/4-DW header represented by conservative 24B framing.
        return max(1, math.ceil((payload_bytes + 24) / self.bytes_per_ns))


@dataclass
class Request:
    id: int
    source: int
    target: int  # -1 = Host; peer otherwise
    address: int
    length: int
    write: bool
    submitted_ns: int
    deadline_ns: int
    epoch: int
    queue: int = 0
    state: str = 'pending'
    error: str = ''
    bytes_committed: int = 0
    outstanding: int = 0
    finished_ns: int | None = None
    packets: deque = field(default_factory=deque, repr=False)
    callback: object = field(default=None, repr=False)
    error_callback: object = field(default=None, repr=False)


@dataclass
class Packet:
    id: int
    request: int
    kind: str
    address: int
    length: int
    tag: int | None
    path: tuple
    stage: int = 0
    parent_packet: int | None = None
    submitted_ns: int = 0
    start_ns: int | None = None
    end_ns: int | None = None
    status: str = 'queued'


class Fabric:
    """One switch, full-duplex downstream links and a shared Host uplink.

    MemWr consumes Posted, MemRd Non-Posted, CplD Completion credits. Each
    direction has independent H/D pools. A tag remains busy until all split
    completions return. Writes have software DMA completion, never PCIe Cpl.
    """
    def __init__(self, cards=8, mps=256, mrrs=512, tags=8, ordering='strict',
                 default=None, links=None, max_pending=4096):
        if cards not in (1, 2, 8) or mps not in (128, 256, 512, 1024, 2048, 4096):
            raise ValueError('cards/MPS')
        if mrrs not in (128, 256, 512, 1024, 2048, 4096) or tags < 1:
            raise ValueError('MRRS/tags')
        if ordering not in ('strict', 'pci_relaxed', 'delayed'):
            raise ValueError('ordering')
        self.cards, self.mps, self.mrrs, self.tag_count = cards, mps, mrrs, tags
        self.ordering, self.max_pending = ordering, max_pending
        self.default = default or LinkConfig()
        self.config = dict(links or {})
        for c in [self.default, *self.config.values()]:
            if c.data_credits * 16 < mps:
                raise ValueError('Completion/Posted pool cannot fit one MPS packet')
        self.now = 0
        self.epochs = [0] * cards
        self.requests = {}
        self.packets = {}
        self.events = []
        self.serial = 0
        self.pending = defaultdict(deque)
        self.waiting = defaultdict(deque)
        self.free_tags = {i: set(range(tags)) for i in range(cards)}
        self.tag_left = {}
        self.link_busy = defaultdict(int)
        self.credit = {}
        self.rr = defaultdict(int)
        self.stats = defaultdict(lambda: dict(packets=0, bytes=0, wire_ns=0,
            credit_stall_ns=0, arbitration_wait_ns=0, max_buffer_packets=0,
            in_flight=0, max_in_flight=0, credit_consumed=0, credit_returned=0))
        self.trace = []
        self._next_request = self._next_packet = 0

    def cfg(self, name):
        return self.config.get(name, self.default)

    def path(self, source, target):
        return (f'd{source}:up', 'host:up') if target == -1 else (f'd{source}:up', f'd{target}:down')

    @staticmethod
    def reverse(path):
        return tuple(x.rsplit(':', 1)[0] + (':down' if x.endswith(':up') else ':up') for x in reversed(path))

    def event(self, when, kind, data):
        self.serial += 1
        heapq.heappush(self.events, (when, self.serial, kind, data))

    def submit(self, source, target, address, length, write, callback=None,
               queue=0, timeout_ns=10_000_000, error_callback=None, operation=None):
        if operation is not None and operation not in ('MemWr', 'MemRd', 'CfgRd', 'CfgWr', 'Message'):
            raise ValueError('operation')
        if source not in self.epochs_range() or target not in range(-1, self.cards) or target == source:
            raise ValueError('source/target')
        if address < 0 or length <= 0 or address + length > 1 << 64 or timeout_ns <= 0:
            raise ValueError('address/length/timeout')
        if sum(r.state == 'pending' for r in self.requests.values()) >= self.max_pending:
            raise BufferError('submission queue full')
        self._next_request += 1
        r = Request(self._next_request, source, target, address, length, write,
                    self.now, self.now + timeout_ns, self.epochs[source], queue,
                    callback=callback, error_callback=error_callback)
        r.operation = operation or ('MemWr' if write else 'MemRd')
        self.requests[r.id] = r
        pos = 0
        while pos < length:
            a = address + pos
            n = min(length - pos, (self.mps if write else self.mrrs), 4096 - a % 4096)
            r.packets.append((a, n))
            pos += n
        self.event(r.deadline_ns, 'timeout', r.id)
        self._issue(r)
        return r

    def control(self, source, target, operation, address=0, callback=None):
        if operation not in ('CfgRd', 'CfgWr', 'Message'):
            raise ValueError('control transaction type')
        return self.submit(source, target, address, 4, operation == 'Message',
                           callback, operation=operation)

    def epochs_range(self):
        return range(self.cards)

    def _packet(self, r, kind, a, n, tag, path, parent=None):
        self._next_packet += 1
        p = Packet(self._next_packet, r.id, kind, a, n, tag, path,
                   parent_packet=parent, submitted_ns=self.now)
        self.packets[p.id] = p
        self._enqueue(p)
        return p

    def _issue(self, r):
        if r.state != 'pending':
            return
        # Strict keeps one transaction per request in flight. Distinct requests
        # remain concurrent; callers needing dependencies use wait/barrier.
        while r.packets and (not r.outstanding or self.ordering != 'strict'):
            if not r.write and not self.free_tags[r.source]:
                break
            tag = None
            if not r.write:
                tag = min(self.free_tags[r.source])
                self.free_tags[r.source].remove(tag)
            a, n = r.packets.popleft()
            r.outstanding += 1
            p = self._packet(r, r.operation, a, n, tag, self.path(r.source, r.target))
            if not r.write:
                self.tag_left[(r.source, tag)] = [r.id, n, p.id]
        if not r.packets and not r.outstanding:
            r.state, r.finished_ns = 'complete', self.now

    def _enqueue(self, p):
        link = p.path[p.stage]
        self.pending[link].append(p.id)
        self.stats[link]['max_buffer_packets'] = max(self.stats[link]['max_buffer_packets'], len(self.pending[link]))
        self.event(self.now, 'pump', link)

    def _pool(self, link, kind):
        pool = 'Cpl' if kind in ('CplD', 'Cpl') else ('P' if kind in ('MemWr', 'Message') else 'NP')
        key = (link, pool)
        c = self.cfg(link)
        if key not in self.credit:
            self.credit[key] = [c.header_credits, c.data_credits]
        return key

    def _pump(self, link):
        q = self.pending[link]
        q = deque(i for i in q if self.requests[self.packets[i].request].state == 'pending')
        self.pending[link] = q
        if not q:
            return
        cfg = self.cfg(link)
        ready = max(self.link_busy[link], cfg.blocked_until_ns)
        if ready > self.now:
            self.event(ready, 'pump', link)
            return
        eligible = []
        for pid in q:
            p = self.packets[pid]
            key = self._pool(link, p.kind)
            need = 0 if p.kind in ('MemRd', 'CfgRd', 'Cpl') else (p.length + 15) // 16
            if self.credit[key][0] and self.credit[key][1] >= need:
                eligible.append(pid)
        if not eligible:
            return  # credit-return events resume this port
        owners = sorted({self.requests[self.packets[i].request].source for i in eligible})
        if cfg.arbitration == 'fixed_priority':
            owner = owners[0]
        else:
            ring = [i for i in range(self.cards) for _ in range(cfg.weights[i % len(cfg.weights)] if cfg.arbitration == 'weighted_round_robin' else 1)]
            cursor = self.rr[link] % len(ring)
            while ring[cursor] not in owners:
                cursor = (cursor + 1) % len(ring)
            owner = ring[cursor]
            self.rr[link] = cursor + 1
        pid = next(i for i in eligible if self.requests[self.packets[i].request].source == owner)
        q.remove(pid)
        p = self.packets[pid]
        key = self._pool(link, p.kind)
        need = 0 if p.kind in ('MemRd', 'CfgRd', 'Cpl') else (p.length + 15) // 16
        self.credit[key][0] -= 1
        self.credit[key][1] -= need
        wire = cfg.serialization(0 if p.kind in ('MemRd', 'CfgRd', 'Cpl') else p.length)
        arrive = self.now + wire + cfg.latency_ns
        if self.ordering == 'delayed':
            arrive += cfg.latency_ns
        self.link_busy[link] = self.now + wire
        p.start_ns = self.now if p.start_ns is None else p.start_ns
        st = self.stats[link]
        st['packets'] += 1
        st['bytes'] += 0 if p.kind in ('MemRd', 'CfgRd', 'Cpl') else p.length
        st['wire_ns'] += wire
        st['in_flight'] += 1
        st['max_in_flight'] = max(st['max_in_flight'], st['in_flight'])
        st['credit_consumed'] += 1 + need
        self.trace.append(dict(event='link_send', time_ns=self.now, packet=pid,
            request=p.request, kind=p.kind, link=link, requester=owner,
            completer=self.requests[p.request].target, tag=p.tag, address=p.address,
            bytes=p.length, queue=self.requests[p.request].queue))
        self.event(arrive, 'arrive', pid)
        self.event(arrive + cfg.credit_return_ns, 'credit', (key, need))
        self.event(self.link_busy[link], 'pump', link)

    def _fail(self, r, error):
        if r.state != 'pending':
            return
        r.state, r.error, r.finished_ns = 'error', error, self.now
        for key, value in list(self.tag_left.items()):
            if value[0] == r.id:
                self.free_tags[key[0]].add(key[1])
                del self.tag_left[key]
        for p in self.packets.values():
            if p.request == r.id and p.status != 'delivered':
                p.status = error
        if r.error_callback:
            r.error_callback(r)

    def cancel(self, source, reason='reset'):
        self.epochs[source] += 1
        for r in self.requests.values():
            if r.source == source or r.target == source:
                self._fail(r, reason)

    def _arrive(self, p):
        r = self.requests[p.request]
        if r.state != 'pending':
            return
        p.stage += 1
        if p.stage < len(p.path):
            self._enqueue(p)
            return
        p.end_ns, p.status = self.now, 'delivered'
        try:
            if p.kind in ('MemWr', 'MemRd', 'CfgRd', 'CfgWr', 'Message'):
                # Callback checks real route/session/IOMMU/permissions and moves
                # exactly this fragment through QEMU, never a Host staging copy.
                if r.callback and p.kind in ('MemWr', 'Message'):
                    r.callback(p)
                if p.kind in ('MemWr', 'Message'):
                    r.bytes_committed += p.length
                    r.outstanding -= 1
                else:
                    pos = 0
                    while pos < p.length:
                        n = min(self.mps, p.length - pos)
                        self._packet(r, 'Cpl' if p.kind == 'CfgWr' else 'CplD', p.address + pos, n, p.tag,
                                     self.reverse(p.path), p.id)
                        pos += n
            else:
                key = (r.source, p.tag)
                state = self.tag_left.get(key)
                if not state or state[0] != r.id or state[2] != p.parent_packet:
                    raise RuntimeError('Completion requester/tag mismatch')
                if r.callback:
                    r.callback(p)
                state[1] -= p.length
                r.bytes_committed += p.length
                if not state[1]:
                    del self.tag_left[key]
                    self.free_tags[r.source].add(p.tag)
                    r.outstanding -= 1
            self._issue(r)
            for other in self.requests.values():
                if other.source == r.source:
                    self._issue(other)
        except Exception as exc:
            self._fail(r, f'UR:{exc}')

    def run(self, until_ns=None, report=True):
        while self.events:
            when, seq, kind, data = heapq.heappop(self.events)
            if until_ns is not None and when > until_ns:
                heapq.heappush(self.events, (when, seq, kind, data))
                break
            # Ignore stale timeout events without advancing reported sim time.
            if kind == 'timeout' and self.requests[data].state != 'pending':
                continue
            previous = self.now
            self.now = when
            for link, q in self.pending.items():
                if q:
                    st = self.stats[link]
                    st['arbitration_wait_ns'] += when - previous
                    if all(not self.credit[self._pool(link, self.packets[i].kind)][0] or
                           self.credit[self._pool(link, self.packets[i].kind)][1] <
                           (0 if self.packets[i].kind in ('MemRd', 'CfgRd', 'CfgWr') else (self.packets[i].length + 15) // 16)
                           for i in q):
                        st['credit_stall_ns'] += when - previous
            if kind == 'pump':
                self._pump(data)
            elif kind == 'arrive':
                self._arrive(self.packets[data])
            elif kind == 'credit':
                key, need = data
                self.credit[key][0] += 1
                self.credit[key][1] += need
                st = self.stats[key[0]]
                st['in_flight'] -= 1
                st['credit_returned'] += 1 + need
                self.event(self.now, 'pump', key[0])
            elif kind == 'timeout':
                self._fail(self.requests[data], 'Completion timeout' if not self.requests[data].write else 'DMA timeout')
        return self.report() if report else None

    def report(self):
        requests = []
        for r in self.requests.values():
            d = {k: v for k, v in vars(r).items() if k not in ('packets', 'callback', 'error_callback')}
            requests.append(d)
        latencies=[r.finished_ns-r.submitted_ns for r in self.requests.values() if r.finished_ns is not None]
        pools={f'{link}/{pool}':dict(header_free=values[0],data_free=values[1],
                    header_capacity=self.cfg(link).header_credits,data_capacity=self.cfg(link).data_credits)
               for (link,pool),values in self.credit.items()}
        balanced=all(v['header_free']==v['header_capacity'] and v['data_free']==v['data_capacity'] for v in pools.values())
        return dict(schema=1, model='P5-discrete-event-v1', time_ns=self.now,
            summary=dict(requests=len(self.requests),packets=len(self.packets),
                errors=sum(r.state=='error' for r in self.requests.values()),
                timeout=sum('timeout' in r.error for r in self.requests.values()),
                unsupported=sum(r.error.startswith('UR:') for r in self.requests.values()),
                average_request_latency_ns=sum(latencies)/len(latencies) if latencies else 0,
                max_request_latency_ns=max(latencies,default=0)), credit_pools=pools,
            config=dict(cards=self.cards, mps=self.mps, mrrs=self.mrrs,
                        tags=self.tag_count, ordering=self.ordering,
                        default=asdict(self.default), links={k: asdict(v) for k, v in self.config.items()}),
            requests=requests, packets=[asdict(p) for p in self.packets.values()],
            links={k: dict(v, utilization=(v['wire_ns'] / self.now if self.now else 0)) for k, v in self.stats.items()},
            credits_balanced=balanced and all(v['credit_consumed'] == v['credit_returned'] and v['in_flight'] == 0 for v in self.stats.values()),
            trace=self.trace)
