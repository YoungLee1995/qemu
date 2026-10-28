/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Internal P1 seqpacket control plane; included after ZCP1State definition. */
#define P1_HEADER 48
#define P1_CAPS 15
#define P1_MAX_PAYLOAD 65536
#define P1_HELLO 1
#define P1_HELLO_ACK 2
#define P1_MEM_SETUP 3
#define P1_MEM_READY 4
#define P1_START 5
#define P1_SOFTWARE_READY 6
#define P1_QUIESCE 7
#define P1_QUIESCED 8
#define P1_RESET 9
#define P1_RESET_ACK 10
#define P1_STOPPED 12
#define P1_ERROR 13
#define P1_PING 14
#define P1_PONG 15

static void p1_session_fail(ZCP1State *s, const char *reason)
{
    if (s->session_fd >= 0) {
        close(s->session_fd);
        s->session_fd = -1;
    }
    s->active = s->session_ready = false;
    if (s->p2.enabled) {
        p2_cancel(s);
    }
    s->error = 4;
    s->pending_type = 0;
    s->reset_pending = false;
    s->retry_at = qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 250;
    warn_report("P1 card %u session failed: %s", s->card, reason);
}

static bool p1_session_send(ZCP1State *s, uint16_t type,
                            const uint8_t *payload, size_t size, bool with_fds)
{
    uint8_t frame[P1_HEADER + 128] = { 0 };
    char ancillary[CMSG_SPACE(3 * sizeof(int))] = { 0 };
    struct iovec iov = { .iov_base = frame, .iov_len = P1_HEADER + size };
    struct msghdr msg = { .msg_iov = &iov, .msg_iovlen = 1 };
    int fds[3] = { s->ddr0_fd, s->ddr1_fd, s->mailbox_fd };
    assert(size <= 128);
    stl_le_p(frame, 0x5a435030);
    stw_le_p(frame + 4, 1);
    stw_le_p(frame + 6, type);
    stl_le_p(frame + 8, size);
    stl_le_p(frame + 12, s->card);
    stq_le_p(frame + 16, s->session);
    stq_le_p(frame + 24, s->epoch);
    stq_le_p(frame + 32, ++s->request);
    if (size) {
        memcpy(frame + P1_HEADER, payload, size);
    }
    if (with_fds) {
        struct cmsghdr *cmsg;
        msg.msg_control = ancillary;
        msg.msg_controllen = sizeof(ancillary);
        cmsg = CMSG_FIRSTHDR(&msg);
        cmsg->cmsg_level = SOL_SOCKET;
        cmsg->cmsg_type = SCM_RIGHTS;
        cmsg->cmsg_len = CMSG_LEN(sizeof(fds));
        memcpy(CMSG_DATA(cmsg), fds, sizeof(fds));
    }
    if (sendmsg(s->session_fd, &msg, MSG_DONTWAIT | MSG_NOSIGNAL) !=
        P1_HEADER + size) {
        p1_session_fail(s, "send/backpressure");
        return false;
    }
    s->pending_type = type;
    s->deadline = qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + s->timeout_ms;
    return true;
}

static bool p1_session_fresh_memory(ZCP1State *s)
{
    static const size_t sizes[] = { DDR0_SIZE, DDR1_SIZE, 4096 };
    int fds[3] = { -1, -1, -1 };
    void *maps[3] = { NULL, NULL, NULL };
    for (unsigned i = 0; i < 3; i++) {
        fds[i] = qemu_memfd_create("zc-p1-session", sizes[i], false, 0,
            F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL, NULL);
        if (fds[i] < 0) {
            goto fail;
        }
        maps[i] = mmap(NULL, sizes[i], PROT_READ | PROT_WRITE,
                       MAP_SHARED, fds[i], 0);
        if (maps[i] == MAP_FAILED) {
            maps[i] = NULL;
            goto fail;
        }
    }
    munmap(s->ddr0, DDR0_SIZE);
    munmap(s->ddr1, DDR1_SIZE);
    munmap(s->mailbox, 4096);
    close(s->ddr0_fd);
    close(s->ddr1_fd);
    close(s->mailbox_fd);
    s->ddr0 = maps[0];
    s->ddr1 = maps[1];
    s->mailbox = maps[2];
    s->ddr0_fd = fds[0];
    s->ddr1_fd = fds[1];
    s->mailbox_fd = fds[2];
    return true;
fail:
    for (unsigned i = 0; i < 3; i++) {
        if (maps[i]) {
            munmap(maps[i], sizes[i]);
        }
        if (fds[i] >= 0) {
            close(fds[i]);
        }
    }
    return false;
}

static void p1_session_connect(ZCP1State *s)
{
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    uint8_t payload[48] = { 0 };
    int fd;
    if (strlen(s->backend_socket) >= sizeof(addr.sun_path)) {
        p1_session_fail(s, "socket path too long");
        return;
    }
    memcpy(addr.sun_path, s->backend_socket, strlen(s->backend_socket) + 1);
    fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        p1_session_fail(s, "socket creation");
        return;
    }
    s->session_fd = fd;
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        p1_session_fail(s, "connect");
        return;
    }
    /* Session never repeats across reconnects or QEMU starts. */
    if (getrandom(&s->session, sizeof(s->session), 0) != sizeof(s->session) ||
        !s->session) {
        p1_session_fail(s, "session random source");
        return;
    }
    if (s->session_started && !p1_session_fresh_memory(s)) {
        p1_session_fail(s, "new session memory allocation");
        return;
    }
    msix_reset(&s->parent_obj);
    s->session_started = true;
    s->request = 0;
    s->last_done = s->notifications = 0;
    s->session_ready = s->active = false;
    stq_le_p(payload, P1_CAPS | (s->p2.enabled ? 16 : 0));
    stl_le_p(payload + 8, P1_MAX_PAYLOAD);
    memcpy(payload + 16, s->config_digest, 32);
    p1_session_send(s, P1_HELLO, payload, sizeof(payload), false);
}

static void p1_session_poll(ZCP1State *s)
{
    uint8_t frame[P1_HEADER + P1_MAX_PAYLOAD];
    uint8_t payload[72] = { 0 };
    char ancillary[CMSG_SPACE(16 * sizeof(int))];
    struct iovec iov = { .iov_base = frame, .iov_len = sizeof(frame) };
    struct msghdr msg = { .msg_iov = &iov, .msg_iovlen = 1,
                         .msg_control = ancillary,
                         .msg_controllen = sizeof(ancillary) };
    int64_t now = qemu_clock_get_ms(QEMU_CLOCK_REALTIME);
    ssize_t n;
    uint16_t type;
    uint32_t length, flags;
    uint64_t request;
    bool unexpected_fds = false;
    if (s->session_fd < 0) {
        if (now >= s->retry_at) {
            p1_session_connect(s);
        }
        return;
    }
    n = recvmsg(s->session_fd, &msg, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        if (s->pending_type && now > s->deadline) {
            p1_session_fail(s, "reply/software-ready timeout");
        } else if (!s->pending_type && now >= s->heartbeat_at &&
                   s->session_ready) {
            p1_session_send(s, P1_PING, NULL, 0, false);
        }
        return;
    }
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); n >= 0 && c;
         c = CMSG_NXTHDR(&msg, c)) {
        unexpected_fds = true;
        if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
            size_t count = (c->cmsg_len - CMSG_LEN(0)) / sizeof(int);
            int *fds = (int *)CMSG_DATA(c);
            for (size_t i = 0; i < count; i++) {
                close(fds[i]);
            }
        }
    }
    if (n < P1_HEADER || unexpected_fds ||
        (msg.msg_flags & (MSG_TRUNC | MSG_CTRUNC))) {
        p1_session_fail(s, "disconnect/truncation/ancillary data");
        return;
    }
    type = lduw_le_p(frame + 6);
    length = ldl_le_p(frame + 8);
    request = ldq_le_p(frame + 32);
    flags = ldl_le_p(frame + 40);
    if (ldl_le_p(frame) != 0x5a435030 || lduw_le_p(frame + 4) != 1 ||
        length > P1_MAX_PAYLOAD || n != P1_HEADER + length ||
        ldl_le_p(frame + 12) != s->card ||
        ldq_le_p(frame + 16) != s->session ||
        ldq_le_p(frame + 24) != s->epoch || ldl_le_p(frame + 44)) {
        p1_session_fail(s, "invalid response identity/header");
        return;
    }
    if (flags == 1 && !request && type == P1_STOPPED && length == 4 &&
        ldl_le_p(frame + P1_HEADER) == 0) {
        /* Normal software STOP event; leave session live for reset. */
        s->active = false;
        s->software_stopped = true;
        return;
    }
    if (flags || !s->pending_type || request != s->request ||
        type == P1_ERROR) {
        p1_session_fail(s, "error/duplicate/unmatched response");
        return;
    }
    if (s->pending_type == P1_HELLO && type == P1_HELLO_ACK && length == 48) {
        uint64_t caps = P1_CAPS | (s->p2.enabled ? 16 : 0);
        if (ldq_le_p(frame + P1_HEADER) != caps ||
            ldl_le_p(frame + P1_HEADER + 8) != P1_MAX_PAYLOAD ||
            ldl_le_p(frame + P1_HEADER + 12) ||
            memcmp(frame + P1_HEADER + 16, s->config_digest, 32)) {
            p1_session_fail(s, "capability/config digest mismatch");
            return;
        }
        for (unsigned i = 0; i < 3; i++) {
            static const uint64_t base[] = { 0x20000000, 0x4000000000,
                                             0x18019000 };
            static const uint64_t size[] = { DDR0_SIZE, DDR1_SIZE, 4096 };
            stq_le_p(payload + i * 24, base[i]);
            stq_le_p(payload + i * 24 + 8, size[i]);
            stl_le_p(payload + i * 24 + 16, i);
        }
        p1_session_send(s, P1_MEM_SETUP, payload, sizeof(payload), true);
    } else if (s->pending_type == P1_MEM_SETUP && type == P1_MEM_READY &&
               !length) {
        p1_session_send(s, P1_START, NULL, 0, false);
    } else if (s->pending_type == P1_START && type == P1_SOFTWARE_READY &&
               !length) {
        s->pending_type = 0;
        s->session_ready = true;
        s->software_stopped = s->reset_pending = false;
        s->heartbeat_at = now + 1000;
        s->error = 0;
    } else if (s->pending_type == P1_QUIESCE && type == P1_QUIESCED &&
               length == 4 && !ldl_le_p(frame + P1_HEADER)) {
        /* Peer reaped old backend; epoch may advance and memory be reused. */
        s->epoch = s->target_epoch;
        p1_session_send(s, P1_RESET, NULL, 0, false);
    } else if (s->pending_type == P1_RESET && type == P1_RESET_ACK && !length) {
        s->last_done = s->notifications = 0;
        s->reset_pending = false;
        s->session_ready = false;
        p1_session_send(s, P1_START, NULL, 0, false);
    } else if (s->pending_type == P1_PING && type == P1_PONG && !length) {
        s->pending_type = 0;
        s->heartbeat_at = now + 1000;
    } else {
        p1_session_fail(s, "unexpected reply kind/payload/state");
    }
}
