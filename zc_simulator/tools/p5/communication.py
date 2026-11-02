"""Bounded software queues and async communication over a supplied DMA adapter.

The interface is a simulator-owned library, not a new hardware queue ABI. The
adapter uses the existing demo HDMA registers. No CCL/collective is required.
"""
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class Ticket:
    sequence: int
    source: int
    target: int
    queue: int
    epoch: int
    offset: int
    length: int
    write: bool
    state: str = 'queued'
    error: str = ''
    request: object = None


class Communication:
    def __init__(self, adapter, fabric, depth=8):
        if depth < 1:
            raise ValueError('depth')
        self.adapter, self.fabric, self.depth = adapter, fabric, depth
        self.queues = defaultdict(deque)
        self.sequence = 0
        self.epochs = [0] * fabric.cards
        self.busy_ranges = []
        self.history = []

    def submit(self, source, target, offset, length, data=None, queue=0,
               timeout_ns=10_000_000):
        if source not in range(self.fabric.cards) or target not in range(-1, self.fabric.cards) or source == target:
            raise ValueError('source/target')
        if queue < 0 or offset < 0 or length <= 0 or offset + length > 0x20000:
            raise ValueError('queue/payload range')
        if data is not None and len(data) != length:
            raise ValueError('payload length')
        key = (source, queue)
        if len(self.queues[key]) >= self.depth:
            raise BufferError('software queue full')
        # Reject overlapping active receive ranges. Independent queues do not
        # silently race for the same destination buffer.
        owner = (('host', source) if target == -1 else target) if data is not None else source
        if any(c == owner and offset < end and offset + length > start
               for _, c, start, end in self.busy_ranges):
            raise BufferError('receive range already in flight')
        self.sequence += 1
        t = Ticket(self.sequence, source, target, queue, self.epochs[source], offset,
                   length, data is not None)
        self.adapter.prepare(t, data)
        self.busy_ranges.append((t.sequence, owner, offset, offset + length))
        self.queues[key].append(t)
        self.history.append(t)
        t.timeout_ns = timeout_ns
        return t

    def send(self, source, target, data, offset=0, queue=0, timeout_ns=10_000_000):
        return self.submit(source, target, offset, len(data), data, queue, timeout_ns)

    def read(self, source, target, length, offset=0, queue=0, timeout_ns=10_000_000):
        return self.submit(source, target, offset, length, None, queue, timeout_ns)

    def _release(self, t):
        self.busy_ranges[:] = [r for r in self.busy_ranges if r[0] != t.sequence]

    def progress(self):
        # Queue order is FIFO; different queues/cards are concurrently admitted.
        active = []
        for key in sorted(self.queues):
            q = self.queues[key]
            if q and q[0].state == 'queued':
                t = q[0]
                t.state = 'running'
                t.request = self.fabric.submit(t.source, t.target,
                    self.adapter.address(t), t.length, t.write,
                    callback=lambda p, t=t: self.adapter.transfer(t, p),
                    queue=t.queue, timeout_ns=t.timeout_ns)
                active.append(t)
        self.fabric.run()
        for q in self.queues.values():
            if not q:
                continue
            t = q[0]
            if t.state == 'running' and t.request.state != 'pending':
                t.state, t.error = t.request.state, t.request.error
                self._release(t)
                q.popleft()
        return bool(active)

    def wait(self, t):
        while t.state in ('queued', 'running'):
            if not self.progress() and t.state in ('queued', 'running'):
                raise RuntimeError('queue made no progress')
        if t.state != 'complete':
            raise RuntimeError(t.error)
        return self.adapter.result(t)

    def barrier(self):
        while any(self.queues.values()):
            self.progress()
        if any(t.state == 'error' for t in self.history):
            raise RuntimeError('communication barrier has failed requests')

    def reset(self, card):
        self.epochs[card] += 1
        self.fabric.cancel(card)
        self.adapter.reset(card)
        for key, q in self.queues.items():
            survivors = deque()
            for t in q:
                if t.source == card or t.target == card:
                    t.state, t.error = 'error', 'reset'
                    self._release(t)
                else:
                    survivors.append(t)
            self.queues[key] = survivors
