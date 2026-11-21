import errno

from openajar.env import seccomp as sc

PROG = sc.program()
EPERM = sc.RET_ERRNO | errno.EPERM
CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
SIGCHLD = 17
CLONE_THREAD_FLAGS = 0x003D0F00


def ev(nr, **kw):
    return sc.evaluate(PROG, nr=nr, **kw)


def test_ordinary_syscalls_are_allowed():
    for nr in (0, 1, 2, 3, 9, 57, 59, 61, 101, 257, 435 - 1):   # read write open close mmap fork execve wait ptrace
        assert ev(nr) == sc.RET_ALLOW, nr
    assert ev(sc.NR_CLONE, args=(SIGCHLD,)) == sc.RET_ALLOW
    assert ev(sc.NR_CLONE, args=(CLONE_THREAD_FLAGS,)) == sc.RET_ALLOW


def test_escape_hatches_are_denied():
    for name in ("mount", "umount2", "unshare", "setns", "keyctl", "add_key", "bpf", "perf_event_open",
                 "io_uring_setup", "userfaultfd", "init_module", "finit_module", "kexec_load", "open_by_handle_at",
                 "pivot_root", "fsopen", "move_mount", "clock_settime"):
        assert ev(sc.DENY_X86_64[name]) == EPERM, name


def test_clone_into_new_namespaces_is_denied_and_clone3_falls_back():
    assert ev(sc.NR_CLONE, args=(SIGCHLD | CLONE_NEWUSER,)) == EPERM
    assert ev(sc.NR_CLONE, args=(CLONE_NEWNET,)) == EPERM
    assert ev(sc.NR_CLONE3) == sc.RET_ERRNO | errno.ENOSYS


def test_foreign_arch_is_killed_and_x32_denied():
    assert ev(1, arch=0x40000003) == sc.RET_KILL_PROCESS          # i386
    assert ev(1 | sc.X32_SYSCALL_BIT) == EPERM


def test_compiled_filter_is_sock_filter_array():
    data = sc.compile_filter()
    assert len(data) == 8 * len(PROG)
    assert all(0 <= jt <= 255 and 0 <= jf <= 255 for _, jt, jf, _ in PROG)
