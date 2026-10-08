"""Windows-only process identity, singleton guards and cooperative stop events."""
import ctypes as c
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parent.parent
KEY = hashlib.sha256(str(ROOT).casefold().encode()).hexdigest()[:16]
k = c.WinDLL('kernel32', use_last_error=True)
nt = c.WinDLL('ntdll')
for name, result, args in (
    ('OpenProcess', w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
    ('CloseHandle', w.BOOL, [w.HANDLE]),
    ('CreateMutexW', w.HANDLE, [c.c_void_p, w.BOOL, w.LPCWSTR]),
    ('ReleaseMutex', w.BOOL, [w.HANDLE]),
    ('CreateEventW', w.HANDLE, [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]),
    ('OpenEventW', w.HANDLE, [w.DWORD, w.BOOL, w.LPCWSTR]),
    ('SetEvent', w.BOOL, [w.HANDLE]),
    ('WaitForSingleObject', w.DWORD, [w.HANDLE, w.DWORD]),
    ('ReadProcessMemory', w.BOOL, [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.c_void_p]),
    ('GetProcessTimes', w.BOOL, [w.HANDLE, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p]),
    ('TerminateProcess', w.BOOL, [w.HANDLE, w.UINT]),
    ('CreateToolhelp32Snapshot', w.HANDLE, [w.DWORD, w.DWORD]),
):
    fn = getattr(k, name); fn.restype = result; fn.argtypes = args
nt.NtQueryInformationProcess.argtypes = [w.HANDLE, w.ULONG, c.c_void_p, w.ULONG, c.c_void_p]
nt.NtQueryInformationProcess.restype = w.LONG

class ProcessEntry(c.Structure):
    _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                ('heap', c.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                ('exe', w.WCHAR * 260)]
for name in ('Process32FirstW', 'Process32NextW'):
    getattr(k, name).argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
    getattr(k, name).restype = w.BOOL

class PBI(c.Structure):
    _fields_ = [('reserved', c.c_void_p), ('peb', c.c_void_p),
                ('reserved2', c.c_void_p * 2), ('pid', c.c_size_t), ('reserved3', c.c_void_p)]

def read(handle, address, size):
    buf = c.create_string_buffer(size)
    if not k.ReadProcessMemory(handle, address, buf, size, None):
        raise c.WinError(c.get_last_error())
    return buf.raw

def identity(pid):
    handle = k.OpenProcess(0x410, False, pid)
    if not handle: return None
    try:
        times = [c.c_ulonglong() for _ in range(4)]
        if not k.GetProcessTimes(handle, *(c.byref(t) for t in times)): return None
        pbi = PBI(); wow = c.c_size_t()
        if nt.NtQueryInformationProcess(handle, 0, c.byref(pbi), c.sizeof(pbi), None): return None
        nt.NtQueryInformationProcess(handle, 26, c.byref(wow), c.sizeof(wow), None)
        ptr = 4 if wow.value else c.sizeof(c.c_void_p)
        peb = wow.value or pbi.peb
        params = int.from_bytes(read(handle, peb + (0x20 if ptr == 8 else 0x10), ptr), 'little')
        def unicode_at(offset):
            data = read(handle, params + offset, 16 if ptr == 8 else 8)
            length = int.from_bytes(data[:2], 'little')
            address = int.from_bytes(data[8:16] if ptr == 8 else data[4:8], 'little')
            return read(handle, address, length).decode('utf-16-le') if length else ''
        return {'pid': pid, 'created': times[0].value,
                'cwd': unicode_at(0x38 if ptr == 8 else 0x24),
                'command': unicode_at(0x70 if ptr == 8 else 0x40)}
    except OSError:
        return None
    finally: k.CloseHandle(handle)

shell = c.WinDLL('shell32')
shell.CommandLineToArgvW.argtypes = [w.LPCWSTR, c.POINTER(c.c_int)]
shell.CommandLineToArgvW.restype = c.POINTER(w.LPWSTR)
k.LocalFree.argtypes = [c.c_void_p]

def service_of(info):
    count = c.c_int(); argv = shell.CommandLineToArgvW(info['command'], c.byref(count))
    if not argv: return None
    try: args = [argv[i] for i in range(count.value)]
    finally: k.LocalFree(argv)
    for arg in args[1:]:
        if arg in ('-c', '-m'): break
        if arg.startswith('-'): continue
        if Path(arg).name.casefold() in ('batch_console.py', 'chain_daemon.py'):
            path = Path(arg) if Path(arg).is_absolute() else Path(info['cwd']) / arg
            for name in ('batch_console', 'chain_daemon'):
                if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(ROOT / 'console' / (name + '.py'))):
                    return name
        # -c arguments are Python code, not script paths.
        if arg in ('-c', '-m'): break
    return None

def processes():
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    entry = ProcessEntry(); entry.size = c.sizeof(entry); out = []
    try:
        ok = k.Process32FirstW(snapshot, c.byref(entry))
        while ok:
            if entry.exe.casefold() in ('python.exe', 'pythonw.exe', 'python3.exe'):
                info = identity(entry.pid)
                if info:
                    service = service_of(info) or registered_service(info)
                    if service: out.append({**info, 'service': service})
            ok = k.Process32NextW(snapshot, c.byref(entry))
    finally: k.CloseHandle(snapshot)
    return out

def event_name(info, service):
    return 'Local\\AutoDrama_' + KEY + '_stop_' + service + '_' + str(info['pid']) + '_' + str(info['created'])

def registered_service(info):
    for service in ('batch_console', 'chain_daemon'):
        event = k.OpenEventW(0x100000, False, event_name(info, service))
        if event:
            k.CloseHandle(event)
            return service
    return None

class Mutex:
    def __init__(self, name, timeout=0):
        self.handle = k.CreateMutexW(None, False, 'Local\\AutoDrama_' + KEY + '_' + name)
        if not self.handle: raise c.WinError(c.get_last_error())
        result = k.WaitForSingleObject(self.handle, timeout)
        self.owned = result in (0, 0x80)
        if result not in (0, 0x80, 258): raise c.WinError(c.get_last_error())
    def close(self):
        if self.owned: k.ReleaseMutex(self.handle)
        k.CloseHandle(self.handle)

class Service:
    def __init__(self, name):
        self.lock = Mutex(name)
        if not self.lock.owned:
            self.lock.close(); raise SystemExit('短剧控制台组件已运行：' + name)
        if any(p['service'] == name and p['pid'] != os.getpid() for p in processes()):
            self.lock.close(); raise SystemExit('短剧控制台组件已运行：' + name)
        self.stopped = threading.Event()
        self.handle = k.CreateEventW(None, True, False, event_name(identity(os.getpid()), name))
        if not self.handle: raise c.WinError(c.get_last_error())
        def wait():
            k.WaitForSingleObject(self.handle, 0xFFFFFFFF)
            self.stopped.set()
        threading.Thread(target=wait, daemon=True).start()
    def close(self):
        k.SetEvent(self.handle)
        self.lock.close()
        # The waiter can still be scheduled here; leave the event handle to process exit.

def request_stop(info):
    current = identity(info['pid'])
    if not current or current['created'] != info['created'] or (service_of(current) or registered_service(current)) != info['service']: return
    event = k.OpenEventW(2, False, event_name(info, info['service']))
    if event:
        try: k.SetEvent(event)
        finally: k.CloseHandle(event)

def force_stop(info):
    current = identity(info['pid'])
    if not current or current['created'] != info['created'] or (service_of(current) or registered_service(current)) != info['service']: return
    handle = k.OpenProcess(1 | 0x1000, False, info['pid'])
    if not handle: raise c.WinError(c.get_last_error())
    try:
        times = [c.c_ulonglong() for _ in range(4)]
        if not k.GetProcessTimes(handle, *(c.byref(t) for t in times)) or times[0].value != info['created']: return
        if not k.TerminateProcess(handle, 1): raise c.WinError(c.get_last_error())
    finally: k.CloseHandle(handle)
