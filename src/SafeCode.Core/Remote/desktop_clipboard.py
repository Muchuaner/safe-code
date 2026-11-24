"""One user-requested image, on one unambiguous XRDP/XFCE session. Input is never a filename."""
import ctypes as C
import json
import os
import posixpath
import pwd
import re
import select
import stat
import sys
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff")


def read_image(stream):
    line = stream.readline(1025)
    if len(line) > 1024 or not line.endswith(b"\n"):
        raise ValueError("header")
    header = json.loads(line)
    items = header.get("formats")
    if header.get("v") != 1 or not isinstance(items, list) or not 1 <= len(items) <= len(TYPES):
        raise ValueError("formats")
    targets = {}
    for item in items:
        mime, size = item["mime"], item["size"]
        if mime not in TYPES or mime in targets or type(size) is not int or size <= 0:
            raise ValueError("type/size")
        targets[mime] = size
    if sum(targets.values()) > MAX_BYTES:
        raise ValueError("size")
    result = {}
    for mime, size in targets.items():
        data = stream.read(size)
        if len(data) != size:
            raise ValueError("short payload")
        result[mime] = data
    if stream.read(1):
        raise ValueError("trailing data")
    return result


def select_session(processes, uid, home):
    candidates = set()
    for owner, comm, env in processes:
        if owner != uid or comm != "xfce4-session" or env.get("XRDP_SESSION") != "1":
            continue
        display = env.get("DISPLAY", "")
        if not re.fullmatch(r":[0-9]+(?:\.0)?", display):
            continue
        authority = env.get("XAUTHORITY") or posixpath.join(home, ".Xauthority")
        if not posixpath.isabs(authority):
            continue
        candidates.add((display.removesuffix(".0"), authority))
    if len(candidates) != 1:
        raise ValueError("no unique XRDP session")
    return candidates.pop()


def session_environment():
    def processes(uid):
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                if process.stat().st_uid != uid or (process / "comm").read_text().strip() != "xfce4-session":
                    continue
                values = (process / "environ").read_bytes().split(b"\0")
                env = dict(x.decode(errors="surrogateescape").split("=", 1) for x in values if b"=" in x)
                yield uid, "xfce4-session", env
            except (OSError, ValueError):
                continue

    uid = os.getuid()
    if uid == 0:
        raise ValueError("not a desktop user")
    home = pwd.getpwuid(uid).pw_dir
    display, authority = select_session(processes(uid), uid, home)
    info = os.stat(authority)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
        raise ValueError("X authority")
    os.environ.update(DISPLAY=display, XAUTHORITY=authority, HOME=home, GDK_BACKEND="x11",
                      NO_AT_BRIDGE="1")


class Target(C.Structure):
    _fields_ = [("target", C.c_char_p), ("flags", C.c_uint), ("info", C.c_uint)]


def gtk_api():
    gtk = C.CDLL("libgtk-3.so.0")
    gdk = C.CDLL("libgdk-3.so.0")
    ptr, sint, uint = C.c_void_p, C.c_int, C.c_uint
    get_cb = C.CFUNCTYPE(None, ptr, ptr, uint, ptr)
    clear_cb = C.CFUNCTYPE(None, ptr, ptr)
    def bind(lib, name, result, *params):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, list(params)
        return fn
    return (
        bind(gtk, "gtk_init_check", sint, ptr, ptr),
        bind(gdk, "gdk_atom_intern", ptr, C.c_char_p, sint),
        bind(gtk, "gtk_clipboard_get", ptr, ptr),
        bind(gtk, "gtk_clipboard_set_with_data", sint, ptr, C.POINTER(Target), uint, get_cb, clear_cb, ptr),
        bind(gtk, "gtk_selection_data_set", None, ptr, ptr, sint, ptr, sint),
        bind(gtk, "gtk_main", None), bind(gtk, "gtk_main_quit", None),
        bind(gdk, "gdk_display_get_default", ptr), bind(gdk, "gdk_display_sync", None, ptr),
        get_cb, clear_cb)


def serve(api, payload, ack_fd):
    init, atom, board, claim, set_data, main, quit_main, display, sync, get_cb, clear_cb = api
    if not init(None, None):
        return
    labels = [k.encode("ascii") for k in payload]
    targets = (Target * len(labels))(*(Target(label, 0, i) for i, label in enumerate(labels)))
    atoms = [atom(label, 0) for label in labels]
    values = list(payload.values())

    @get_cb
    def provide(_clipboard, selection, index, _):
        if index < len(values):
            value = values[index]
            set_data(selection, atoms[index], 8, C.cast(C.c_char_p(value), C.c_void_p), len(value))

    @clear_cb
    def replaced(_clipboard, _):
        quit_main()

    if not claim(board(atom(b"CLIPBOARD", 0)), targets, len(labels), provide, replaced, None):
        return
    sync(display())
    os.write(ack_fd, b"1")
    os.close(ack_fd)
    main()  # GTK serves large X11 INCR transfers too. Releases memory when the selection is replaced.


def main():
    session_environment()
    api = gtk_api()  # check runtime before asking Windows to send bytes
    print("SCIMAGE READY", flush=True)
    payload = read_image(sys.stdin.buffer)
    receive, send = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(receive)
            os.setsid()
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            os.close(null)
            serve(api, payload, send)
        finally:
            os._exit(0)
    os.close(send)
    try:
        if not select.select([receive], [], [], 8)[0] or os.read(receive, 1) != b"1":
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            raise RuntimeError("clipboard unavailable")
    finally:
        os.close(receive)
    print("SCIMAGE OWNED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("SCIMAGE ERROR", flush=True)
        sys.exit(1)
