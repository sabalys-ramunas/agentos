"""A small seccomp-bpf deny-list for isolated workloads, compiled here (no libseccomp).

bubblewrap loads it with `--seccomp FD`. It blocks the syscalls a reduced-capability root
could still use to reach outside its lane: new namespaces (unshare, setns, clone with
CLONE_NEW* flags), the kernel keyring (not namespaced), mount APIs, bpf, perf, io_uring,
userfaultfd, module and kexec loading, handle-based opens and clock changes. Everything
else is allowed; capabilities and namespaces do the rest. x86_64 only: on other
architectures no filter is built (the caller says so in the lane description).
"""

from __future__ import annotations

import errno
import platform
import struct

AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000

RET_KILL_PROCESS = 0x80000000
RET_ERRNO = 0x00050000
RET_ALLOW = 0x7FFF0000

# classic BPF opcodes
LD_W_ABS = 0x20
JEQ_K = 0x15
JGE_K = 0x35
JSET_K = 0x45
RET_K = 0x06

OFF_NR = 0
OFF_ARCH = 4
OFF_ARG0_LO = 16

CLONE_NS_FLAGS = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000
NR_CLONE = 56
NR_CLONE3 = 435

DENY_X86_64: dict[str, int] = {
    "vhangup": 153, "pivot_root": 155, "_sysctl": 156, "adjtimex": 159, "chroot": 161, "acct": 163,
    "settimeofday": 164, "mount": 165, "umount2": 166, "swapon": 167, "swapoff": 168, "reboot": 169,
    "sethostname": 170, "setdomainname": 171, "iopl": 172, "ioperm": 173, "create_module": 174,
    "init_module": 175, "delete_module": 176, "get_kernel_syms": 177, "query_module": 178,
    "quotactl": 179, "nfsservctl": 180, "syslog": 103, "lookup_dcookie": 212, "clock_settime": 227,
    "kexec_load": 246, "add_key": 248, "request_key": 249, "keyctl": 250, "unshare": 272,
    "perf_event_open": 298, "name_to_handle_at": 303, "open_by_handle_at": 304, "clock_adjtime": 305,
    "setns": 308, "finit_module": 313, "kexec_file_load": 320, "bpf": 321, "userfaultfd": 323,
    "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427, "open_tree": 428,
    "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433,
    "mount_setattr": 442, "quotactl_fd": 443,
}


def supported() -> bool:
    return platform.machine() in ("x86_64", "AMD64")


def _assemble(prog: list[tuple]) -> list[tuple[int, int, int, int]]:
    """Resolve ("label", name) markers and symbolic jump targets into a flat program."""
    labels: dict[str, int] = {}
    flat: list[tuple] = []
    for ins in prog:
        if ins[0] == "label":
            labels[ins[1]] = len(flat)
        else:
            flat.append(ins)
    out = []
    for i, (code, jt, jf, k) in enumerate(flat):
        def rel(target, i=i):
            if isinstance(target, str):
                off = labels[target] - (i + 1)
            else:
                off = target
            if not 0 <= off <= 255:
                raise ValueError("seccomp jump out of range")
            return off
        out.append((code, rel(jt), rel(jf), k))
    return out


def program(deny: dict[str, int] | None = None) -> list[tuple[int, int, int, int]]:
    deny = DENY_X86_64 if deny is None else deny
    prog: list[tuple] = [
        (LD_W_ABS, 0, 0, OFF_ARCH),
        (JEQ_K, 1, 0, AUDIT_ARCH_X86_64),
        (RET_K, 0, 0, RET_KILL_PROCESS),
        (LD_W_ABS, 0, 0, OFF_NR),
        (JGE_K, "deny", 0, X32_SYSCALL_BIT),
    ]
    for nr in sorted(set(deny.values())):
        prog.append((JEQ_K, "deny", 0, nr))
    prog += [
        (JEQ_K, "enosys", 0, NR_CLONE3),
        (JEQ_K, 0, "allow", NR_CLONE),
        (LD_W_ABS, 0, 0, OFF_ARG0_LO),
        (JSET_K, "deny", "allow", CLONE_NS_FLAGS),
        ("label", "allow"),
        (RET_K, 0, 0, RET_ALLOW),
        ("label", "deny"),
        (RET_K, 0, 0, RET_ERRNO | errno.EPERM),
        ("label", "enosys"),
        (RET_K, 0, 0, RET_ERRNO | errno.ENOSYS),
    ]
    return _assemble(prog)


def compile_filter(deny: dict[str, int] | None = None) -> bytes:
    """The filter as `struct sock_filter[]` bytes, ready for bwrap --seccomp."""
    return b"".join(struct.pack("=HBBI", *ins) for ins in program(deny))


def evaluate(prog: list[tuple[int, int, int, int]], *, nr: int, arch: int = AUDIT_ARCH_X86_64,
             args: tuple[int, ...] = ()) -> int:
    """Run the filter on one syscall (tests). Supports only the opcodes we emit."""
    data = struct.pack("=iIQ6Q", nr, arch, 0, *(list(args) + [0] * (6 - len(args))))
    acc = 0
    pc = 0
    while True:
        code, jt, jf, k = prog[pc]
        if code == LD_W_ABS:
            acc = struct.unpack_from("=I", data, k)[0]
            pc += 1
        elif code == JEQ_K:
            pc += 1 + (jt if acc == k else jf)
        elif code == JGE_K:
            pc += 1 + (jt if acc >= k else jf)
        elif code == JSET_K:
            pc += 1 + (jt if acc & k else jf)
        elif code == RET_K:
            return k
        else:
            raise ValueError(f"opcode {code:#x} not supported")
