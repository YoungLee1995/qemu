/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Native P1 zc_session support, included by spike_main/spike.cc. Linux only. */
#pragma once
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <vector>

#define HEADER 48
#define MAX_PAYLOAD 65536
#define CONTROL 0x100100000ULL
static volatile sig_atomic_t zc_stopping;
static FILE *zc_logfile;
static int zc_regions[3] = {-1, -1, -1};
static uint32_t *zc_mailbox, *zc_control;
static pid_t zc_child = -1;
static const uint64_t zc_bases[] = {0x20000000, 0x4000000000, 0x18019000};
static const uint64_t zc_sizes[] = {1ULL << 30, 1ULL << 38, 4096};
static uint64_t zc_session, zc_epoch, zc_request;
static uint32_t zc_card;
static const char *zc_model;
static char **zc_model_args;
static unsigned char zc_digest[32];

enum { HELLO=1, HELLO_ACK, MEM_SETUP, MEM_READY, START, SOFTWARE_READY,
       QUIESCE, QUIESCED, RESET, RESET_ACK, STOP, STOPPED, ERROR, PING, PONG };
enum { INITIAL, MEMORY, MEMORY_READY, READY, QUIET, FINISHED };
static uint16_t zc_get16(const void *p) { uint16_t v; memcpy(&v,p,2); return le16toh(v); }
static uint32_t zc_get32(const void *p) { uint32_t v; memcpy(&v,p,4); return le32toh(v); }
static uint64_t zc_get64(const void *p) { uint64_t v; memcpy(&v,p,8); return le64toh(v); }
static void zc_put16(void *p,uint16_t v) { v=htole16(v); memcpy(p,&v,2); }
static void zc_put32(void *p,uint32_t v) { v=htole32(v); memcpy(p,&v,4); }
static void zc_put64(void *p,uint64_t v) { v=htole64(v); memcpy(p,&v,8); }
static double zc_now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static void zc_event(const char *text, unsigned type)
{
    struct timespec t; clock_gettime(CLOCK_REALTIME,&t);
    fprintf(zc_logfile,"time=%lld.%09ld card=%u session=%" PRIu64 " epoch=%" PRIu64
            " request=%" PRIu64 " event=%s type=%u pid=%ld\n",
            (long long)t.tv_sec,t.tv_nsec,zc_card,zc_session,zc_epoch,zc_request,text,type,(long)zc_child);
    fflush(zc_logfile);
}
static void zc_signal_stop(int sig) { (void)sig; zc_stopping=1; }
static void zc_reap(void)
{
    if (zc_child < 0) return;
    kill(zc_child,SIGTERM);
    double end=zc_now()+2;
    int status;
    for (;;) {
        pid_t result = waitpid(zc_child,&status,WNOHANG);
        if (result < 0 && errno == EINTR) continue;
        if (result != 0) break;
        if (zc_now()>end) { kill(zc_child,SIGKILL); while(waitpid(zc_child,&status,0)<0 && errno==EINTR) {} break; }
        usleep(10000);
    }
    zc_event("reaped",0); zc_child=-1;
}
static void zc_unmap_regions(void)
{
    if(zc_mailbox) munmap(zc_mailbox,4096);
    if(zc_control) munmap(zc_control,65536);
    zc_mailbox=zc_control=NULL;
    for(unsigned i=0;i<3;i++) { if(zc_regions[i]>=0) close(zc_regions[i]); zc_regions[i]=-1; }
}
static void zc_clear_regions(void)
{
    memset(zc_mailbox,0,4096); memset(zc_control,0,65536);
    __atomic_store_n(zc_mailbox+0x400/4,htole32(zc_epoch),__ATOMIC_RELEASE);
    __atomic_store_n(zc_mailbox+0x404/4,htole32(zc_epoch),__ATOMIC_RELEASE);
}
static bool zc_reply(int fd,unsigned type,const void *data,unsigned size,unsigned flags)
{
    unsigned char frame[HEADER+128]={0};
    zc_put32(frame,0x5a435030); zc_put16(frame+4,1); zc_put16(frame+6,type);
    zc_put32(frame+8,size); zc_put32(frame+12,zc_card); zc_put64(frame+16,zc_session);
    zc_put64(frame+24,zc_epoch); zc_put64(frame+32,flags?0:zc_request); zc_put32(frame+40,flags);
    if(size) memcpy(frame+HEADER,data,size);
    zc_event("send",type);
    return send(fd,frame,HEADER+size,MSG_NOSIGNAL|MSG_DONTWAIT)==(ssize_t)(HEADER+size);
}
static bool zc_spawn(void)
{
    int count=0; while(zc_model_args[count]) count++;
    char ddr0[64],ddr1[64],mb[32],parent[32];
    snprintf(ddr0,sizeof(ddr0),"--p1-ddr0-fd=%d",zc_regions[0]);
    snprintf(ddr1,sizeof(ddr1),"--p1-ddr1-fd=%d",zc_regions[1]);
    snprintf(mb,sizeof(mb),"%d",zc_regions[2]);
    snprintf(parent,sizeof(parent),"%ld",(long)getpid());
    char **args=(char **)calloc(count+4,sizeof(*args));
    if(!args) return false;
    args[0]=(char *)zc_model; args[1]=ddr0; args[2]=ddr1;
    for(int i=0;i<count;i++) args[i+3]=zc_model_args[i];
    zc_child=fork();
    if(zc_child==0) {
        prctl(PR_SET_PDEATHSIG,SIGTERM);
        if(getppid()!=strtol(parent,NULL,10)) _exit(125);
        /* Parent owns the command terminal; runtime is a separate process group. */
        setpgid(0,0);
        signal(SIGINT,SIG_DFL); signal(SIGTERM,SIG_DFL); signal(SIGHUP,SIG_DFL);
        for(unsigned i=0;i<3;i++) fcntl(zc_regions[i],F_SETFD,0);
        setenv("ZC_P1_MAILBOX_FD",mb,1); setenv("ZC_P1_PARENT_PID",parent,1);
        setenv("ZC_P2_PCIE","1",1);
        execv(zc_model,args); perror("exec Spike model"); _exit(127);
    }
    free(args);
    if(zc_child<0) return false;
    zc_event("spawn",0);
    return true;
}
static bool zc_setup(const unsigned char *data,unsigned len,int *fds,unsigned nfds)
{
    if(len!=72 || nfds!=3) return false;
    for(unsigned i=0;i<3;i++) {
        struct stat st;
        int seals=fcntl(fds[i],F_GET_SEALS);
        int required=F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL;
        if(zc_get64(data+i*24)!=zc_bases[i] || zc_get64(data+i*24+8)!=zc_sizes[i] ||
           zc_get32(data+i*24+16)!=i || zc_get32(data+i*24+20) ||
           fstat(fds[i],&st) || (uint64_t)st.st_size!=zc_sizes[i] ||
           seals<0 || (seals&required)!=required) return false;
        for(unsigned j=0;j<i;j++) {
            struct stat other; if(fstat(fds[j],&other)) return false;
            if(st.st_dev==other.st_dev && st.st_ino==other.st_ino) return false;
        }
    }
    void *mb=mmap(NULL,4096,PROT_READ|PROT_WRITE,MAP_SHARED,fds[2],0);
    if(mb==MAP_FAILED) return false;
    void *ct=mmap(NULL,65536,PROT_READ|PROT_WRITE,MAP_SHARED,fds[1],CONTROL);
    if(ct==MAP_FAILED) { munmap(mb,4096); return false; }
    zc_mailbox=(uint32_t *)mb; zc_control=(uint32_t *)ct;
    for(unsigned i=0;i<3;i++) { zc_regions[i]=fds[i]; fds[i]=-1; }
    zc_clear_regions(); return true;
}
static void zc_serve(int fd)
{
    int state=INITIAL;
    double deadline=zc_now()+5;
    zc_session=zc_epoch=zc_request=0;
    while(!zc_stopping) {
        struct pollfd p={.fd=fd,.events=POLLIN};
        int result=poll(&p,1,20);
        if(result<0) { if(errno==EINTR) continue; break; }
        if(result>0) {
            unsigned char frame[HEADER+MAX_PAYLOAD], ancillary[CMSG_SPACE(16*sizeof(int))];
            struct iovec iov={.iov_base=frame,.iov_len=sizeof(frame)};
            struct msghdr msg={.msg_iov=&iov,.msg_iovlen=1,.msg_control=ancillary,.msg_controllen=sizeof(ancillary)};
            ssize_t n=recvmsg(fd,&msg,MSG_CMSG_CLOEXEC|MSG_DONTWAIT);
            int fds[16]; unsigned nfds=0; bool valid=true;
            if(n<0 && (errno==EINTR || errno==EAGAIN)) continue;
            for(struct cmsghdr *c=CMSG_FIRSTHDR(&msg);n>=0 && c;c=CMSG_NXTHDR(&msg,c)) {
                if(c->cmsg_level!=SOL_SOCKET || c->cmsg_type!=SCM_RIGHTS) { valid=false; continue; }
                size_t bytes=c->cmsg_len-CMSG_LEN(0);
                if(bytes%sizeof(int)) valid=false;
                for(size_t j=0;j<bytes/sizeof(int);j++) {
                    int v; memcpy(&v,(char *)CMSG_DATA(c)+j*sizeof(int),sizeof(v));
                    if(nfds<16) fds[nfds++]=v; else { close(v); valid=false; }
                }
            }
            valid=valid && n>=HEADER && !(msg.msg_flags&(MSG_TRUNC|MSG_CTRUNC));
            unsigned kind=valid?zc_get16(frame+6):0, len=valid?zc_get32(frame+8):0;
            uint64_t sid=valid?zc_get64(frame+16):0, ep=valid?zc_get64(frame+24):0, req=valid?zc_get64(frame+32):0;
            if(valid) valid=zc_get32(frame)==0x5a435030 && zc_get16(frame+4)==1 &&
                len<=MAX_PAYLOAD && n==HEADER+(ssize_t)len && zc_get32(frame+12)==zc_card &&
                sid && (!zc_session || sid==zc_session) && req>zc_request && !zc_get32(frame+40) && !zc_get32(frame+44) &&
                (kind==MEM_SETUP || !nfds) && ep<=UINT32_MAX;
            if(valid && zc_session) valid=kind==RESET ? state==QUIET && ep>zc_epoch : ep==zc_epoch;
            if(valid) {
                zc_session=sid; zc_epoch=ep; zc_request=req; zc_event("receive",kind);
                const unsigned char *data=frame+HEADER;
                uint32_t zero=0;
                if(kind==HELLO && state==INITIAL && len==48 && zc_get64(data)==31 &&
                   zc_get32(data+8)==MAX_PAYLOAD && !zc_get32(data+12) && !memcmp(data+16,zc_digest,32)) {
                    valid=zc_reply(fd,HELLO_ACK,data,len,0); state=MEMORY;
                } else if(kind==MEM_SETUP && state==MEMORY && zc_setup(data,len,fds,nfds)) {
                    valid=zc_reply(fd,MEM_READY,NULL,0,0); state=MEMORY_READY;
                } else if(kind==START && state==MEMORY_READY && !len && zc_spawn()) {
                    __atomic_store_n(zc_mailbox+0x408/4,htole32(1),__ATOMIC_RELEASE);
                    valid=zc_reply(fd,SOFTWARE_READY,NULL,0,0); state=READY;
                    puts("SPIKE_QEMU_CONNECTED (transport; Host must check firmware IPC readiness)"); fflush(stdout);
                } else if((kind==QUIESCE || kind==STOP) && !len &&
                          (state==READY || state==FINISHED || state==MEMORY_READY || state==QUIET)) {
                    zc_reap(); __atomic_store_n(zc_mailbox+0x408/4,0,__ATOMIC_RELEASE);
                    valid=zc_reply(fd,kind==QUIESCE?QUIESCED:STOPPED,&zero,4,0);
                    state=kind==QUIESCE?QUIET:FINISHED;
                } else if(kind==RESET && state==QUIET && !len) {
                    zc_reap(); zc_clear_regions(); valid=zc_reply(fd,RESET_ACK,NULL,0,0); state=MEMORY_READY;
                } else if(kind==PING && (state==READY || state==FINISHED) && !len) {
                    valid=zc_reply(fd,PONG,NULL,0,0);
                } else valid=false;
                deadline=zc_now()+30;
            }
            for(unsigned i=0;i<nfds;i++) if(fds[i]>=0) close(fds[i]);
            if(!valid) {
                zc_event(n==0?"disconnect":"protocol_error",kind);
                if(n>0 && zc_session) zc_reply(fd,ERROR,"invalid request/state/config",28,0);
                break;
            }
        }
        if(state==READY && zc_child>0) {
            int status; pid_t pid=waitpid(zc_child,&status,WNOHANG);
            if(pid==zc_child) {
                zc_child=-1; __atomic_store_n(zc_mailbox+0x408/4,0,__ATOMIC_RELEASE);
                zc_event("model_exit",0);
                if(!WIFEXITED(status) || WEXITSTATUS(status)) { zc_reply(fd,ERROR,"Spike exited",12,0); break; }
                uint32_t zero=0;
                if(!zc_reply(fd,STOPPED,&zero,4,1)) break;
                state=FINISHED;
            }
        }
        if(zc_now()>deadline) { zc_event("timeout",0); break; }
    }
    zc_reap();
    if (zc_mailbox) __atomic_store_n(zc_mailbox+0x408/4,0,__ATOMIC_RELEASE);
    zc_unmap_regions(); close(fd);
}
static void zc_pcie_usage(const char *program)
{
    fprintf(stderr,"Usage: %s --pcie-socket PATH --pcie-config-sha256 HEX [--pcie-card N] [--pcie-log FILE] MODEL_ARGS...\n"
        "Native QEMU PCIe session: listen, negotiate shared memory, then run the model.\n",program);
}
/* Returns -1 for the existing non-PCIe invocation. Child re-execs this binary
 * without the --pcie-* supervisor options, using negotiated region FDs. */
static int zc_pcie_entry(int argc,char **argv)
{
    bool enabled=false;
    for(int i=1;i<argc;i++) if(!strncmp(argv[i],"--pcie-",7)) enabled=true;
    if(!enabled) return -1;
    const char *path=NULL,*hex=NULL,*logpath=NULL;
    char executable[4096];
    ssize_t length=readlink("/proc/self/exe",executable,sizeof(executable)-1);
    if(length<0 || length==(ssize_t)sizeof(executable)-1) { perror("executable path"); return 1; }
    executable[length]=0; zc_model=executable;
    std::vector<char *> arguments;
    for(int i=1;i<argc;i++) {
        const char *key=argv[i];
        if(!strncmp(key,"--pcie-",7)) {
            if(i+1>=argc) { zc_pcie_usage(argv[0]); return 2; }
            const char *value=argv[++i];
            if(!strcmp(key,"--pcie-socket")) path=value;
            else if(!strcmp(key,"--pcie-config-sha256")) hex=value;
            else if(!strcmp(key,"--pcie-log")) logpath=value;
            else if(!strcmp(key,"--pcie-card")) {
                char *end; unsigned long n=strtoul(value,&end,10);
                if(!*value || *end || n>7) { zc_pcie_usage(argv[0]); return 2; } zc_card=n;
            } else { zc_pcie_usage(argv[0]); return 2; }
        } else {
            if(!strncmp(key,"--p1-ddr",8) || !strcmp(key,"-d")) {
                fprintf(stderr,"Native PCIe mode negotiates FDs; -d is not supported by this entry yet.\n"); return 2;
            }
            arguments.push_back(argv[i]);
        }
    }
    if(!path || !hex || strlen(hex)!=64 || arguments.empty()) { zc_pcie_usage(argv[0]); return 2; }
    for(unsigned j=0;j<32;j++) {
        char b[3]={hex[j*2],hex[j*2+1],0},*end;
        unsigned long v=strtoul(b,&end,16);
        if(*end || strspn(b,"0123456789abcdefABCDEF")!=2) { zc_pcie_usage(argv[0]); return 2; } zc_digest[j]=v;
    }
    arguments.push_back(NULL); zc_model_args=arguments.data();
    struct sockaddr_un addr={}; addr.sun_family=AF_UNIX;
    if(strlen(path)>=sizeof(addr.sun_path)) { fprintf(stderr,"socket path too long\n"); return 2; }
    strcpy(addr.sun_path,path);
    umask(077);
    zc_logfile=logpath?fopen(logpath,"a"):stderr;
    if(!zc_logfile) { perror("interaction log"); return 1; }
    if(zc_logfile!=stderr) fcntl(fileno(zc_logfile),F_SETFD,FD_CLOEXEC);
    int server=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0);
    if(server<0) { perror("socket"); return 1; }
    if(bind(server,(struct sockaddr *)&addr,sizeof(addr))) { perror("bind (existing socket is never overwritten)"); close(server); return 1; }
    if(listen(server,4)) { perror("listen"); close(server); unlink(path); return 1; }
    signal(SIGINT,zc_signal_stop); signal(SIGTERM,zc_signal_stop); signal(SIGHUP,zc_signal_stop);
    printf("SPIKE_WAIT_QEMU socket=%s card=%u\n",path,zc_card); fflush(stdout);
    while(!zc_stopping) {
        struct pollfd p={}; p.fd=server; p.events=POLLIN;
        int result=poll(&p,1,100);
        if(result<0) { if(errno==EINTR) continue; perror("poll"); break; }
        if(result>0) { int fd=accept4(server,NULL,NULL,SOCK_CLOEXEC); if(fd>=0) zc_serve(fd); }
    }
    close(server); unlink(path); if(zc_logfile!=stderr) fclose(zc_logfile);
    return 0;
}
