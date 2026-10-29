/* SPDX-License-Identifier: GPL-2.0-or-later */
static void p5_disconnect(ZCP1State *s)
{
    if (s->p5.fd >= 0) {
        close(s->p5.fd);
    }
    s->p5.fd = -1;
    s->p5.received = 0;
    s->p5.failed = true;
}

static void p5_poll(ZCP1State *s)
{
    P5State *f = &s->p5;
    if (!f->socket || f->failed) {
        return;
    }
    if (f->fd < 0) {
        struct sockaddr_un address = { .sun_family = AF_UNIX };
        if (strlen(f->socket) >= sizeof(address.sun_path)) {
            f->failed = true;
            return;
        }
        memcpy(address.sun_path, f->socket, strlen(f->socket) + 1);
        f->fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (f->fd < 0) {
            f->failed = true;
            return;
        }
        if (connect(f->fd, (struct sockaddr *)&address, sizeof(address)) < 0 &&
            errno != EINPROGRESS) {
            p5_disconnect(s);
            return;
        }
    }
    for (;;) {
        ssize_t n = recv(f->fd, f->input + f->received,
                         P5_FRAME - f->received, MSG_DONTWAIT);
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK ||
                      errno == EINTR)) {
            return;
        }
        if (n <= 0) {
            p5_disconnect(s);
            return;
        }
        f->received += n;
        if (f->received == P5_FRAME) {
            uint64_t token = ldq_le_p(f->input);
            unsigned slot = ldl_le_p(f->input + 8);
            if (slot < P5_SLOTS && f->slot[slot].token == token) {
                f->slot[slot].status = ldl_le_p(f->input + 12);
                f->slot[slot].granted = true;
            }
            f->received = 0;
        }
    }
}

/* -1 = fabric failure, 0 = pending, 1 = admitted. */
static int p5_admit(ZCP1State *s, unsigned slot, uint64_t addr,
                     unsigned len, bool write)
{
    P5State *f = &s->p5;
    P5Slot *p = &f->slot[slot];
    uint8_t frame[P5_FRAME] = { 0 };
    int peer = -1;
    ssize_t n;
    if (!f->socket) {
        return 1;
    }
    p5_poll(s);
    if (f->failed) {
        return -1;
    }
    if (p->granted) {
        int result = p->status ? -1 : 1;
        memset(p, 0, sizeof(*p));
        return result;
    }
    if (p->sent) {
        if (qemu_clock_get_ms(QEMU_CLOCK_REALTIME) > p->deadline) {
            memset(p, 0, sizeof(*p));
            return -1;
        }
        return 0;
    }
    if (!p->token) {
        p->token = ++f->sequence;
    }
    for (GSList *it = p3_endpoints; it; it = it->next) {
        ZCP1State *t = it->data;
        uint64_t bar = pci_get_bar_addr(&t->parent_obj, 4);
        if (bar != PCI_BAR_UNMAPPED && addr >= bar &&
            addr - bar < 0x800000000ULL) {
            peer = t->card;
            break;
        }
    }
    stq_le_p(frame, p->token);
    stl_le_p(frame + 8, slot);
    stl_le_p(frame + 12, s->card);
    stl_le_p(frame + 16, peer);
    stl_le_p(frame + 20, write);
    stq_le_p(frame + 24, addr);
    stl_le_p(frame + 32, len);
    stl_le_p(frame + 36, s->epoch);
    stq_le_p(frame + 40, s->session);
    n = send(f->fd, frame, sizeof(frame), MSG_DONTWAIT | MSG_NOSIGNAL);
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
        return 0;
    }
    /* Partial control frames fail closed rather than risking desync. */
    if (n != sizeof(frame)) {
        p5_disconnect(s);
        return -1;
    }
    p->sent = true;
    p->deadline = qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 4000;
    return 0;
}

static void p5_cancel(ZCP1State *s)
{
    memset(s->p5.slot, 0, sizeof(s->p5.slot));
    if (s->p5.fd < 0) {
        s->p5.failed = false;
    }
}
