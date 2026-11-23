"""Run on any platform; the optional GTK/Xvfb integration uses only an isolated virtual display."""
import ctypes as C
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch
import zlib

SCRIPT = Path(__file__).resolve().parents[1] / "src/SafeCode.Core/Remote/desktop_clipboard.py"
spec = importlib.util.spec_from_file_location("image_clip", SCRIPT)
clip = importlib.util.module_from_spec(spec)
if os.name == "nt":  # pure wire tests never use Unix accounts
    with patch.dict(sys.modules, {"pwd": type(sys)("pwd")}):
        spec.loader.exec_module(clip)
else:
    spec.loader.exec_module(clip)


def frame(formats, payload):
    return json.dumps({"v": 1, "formats": [{"mime": m, "size": n} for m, n in formats]}).encode() + b"\n" + payload


class Wire(unittest.TestCase):
    def test_original_and_fallback(self):
        data = clip.read_image(io.BytesIO(frame([("image/gif", 3), ("image/png", 3)], b"a\0bxx\0")))
        self.assertEqual(list(data.items()), [("image/gif", b"a\0b"), ("image/png", b"xx\0")])

    def test_invalid_payloads(self):
        for msg in [frame([("image/png", 2)], b"1"), frame([("image/png", 1)], b"12"),
                    frame([("image/png", clip.MAX_BYTES + 1)], b""), frame([("image/png", True)], b"A"),
                    frame([("image/png", 1)] * 2, b"12"), frame([("text/html", 1)], b"x"),
                    b"X" * 1025, frame([], b"")]:
            with self.subTest(msg=msg[:70]), self.assertRaises(ValueError):
                clip.read_image(io.BytesIO(msg))

    def test_session_isolation(self):
        def session(d, uid=1001, xrdp="1", comm="xfce4-session", auth=None):
            env = {"DISPLAY": d, "XRDP_SESSION": xrdp}
            if auth is not None:
                env["XAUTHORITY"] = auth
            return uid, comm, env
        choice = [session(":12.0"), session(":12"), session(":13", uid=1002),
                  session(":0", xrdp="0"), session(":7", comm="gnome-session"), session("example.org:0")]
        self.assertEqual(clip.select_session(choice, 1001, "/home/w"), (":12", "/home/w/.Xauthority"))
        for bad in [[], [session(":12"), session(":14")], [session(":12", auth="bad")],
                    [session("localhost:10.0")], [session(":12", xrdp="0")]]:
            with self.assertRaises(ValueError):
                clip.select_session(bad, 1001, "/home/w")

    def test_gtk_selection_callbacks_preserve_binary_for_each_target(self):
        # Exercise the real ctypes callback declarations and readiness order, without a desktop.
        ptr, uint = C.c_void_p, C.c_uint
        get = C.CFUNCTYPE(None, ptr, ptr, uint, ptr)
        clear = C.CFUNCTYPE(None, ptr, ptr)
        callbacks, returned, ordering = [], {}, []
        atoms = {b"image/png": 17, b"image/jpeg": 23, b"CLIPBOARD": 55}
        payload = {"image/jpeg": b"\xff\0\xd9", "image/png": b"\x89\0g"}
        def claim(board, targets, n, provide, replaced, _):
            self.assertEqual(board, 55)
            self.assertEqual([targets[i].target.decode() for i in range(n)], list(payload))
            callbacks.extend([provide,replaced]); ordering.append("claimed")
            return True
        def consume(selection, atom, bits, data, n):
            self.assertEqual(bits,8)
            returned[atom] = C.string_at(data,n)
        ack, send = os.pipe()
        def simulate():
            self.assertEqual(os.read(ack,1),b"1")
            ordering.append("received-ack")
            for i in [1,0,2]: callbacks[0](None, 123, i, None)
            callbacks[1](None,None)
        try:
            api = (lambda *_:True, lambda name,_:atoms[name], lambda a:a,claim,consume,simulate,
                   lambda:ordering.append("released"),lambda:1,lambda _:ordering.append("synced"),get,clear)
            clip.serve(api,payload,send)
            self.assertEqual(returned,{17:payload["image/png"],23:payload["image/jpeg"]})
            self.assertEqual(ordering,["claimed","synced","received-ack","released"])
        finally:
            os.close(ack)


@unittest.skipUnless(os.name != "nt" and shutil.which("Xvfb") and shutil.which("xclip"), "requires isolated Linux Xvfb + GTK3 + xclip")
class NativeSelection(unittest.TestCase):
    def test_multiple_targets_and_large_transfer(self):
        # Real image larger than an X11 property (INCR). No connection to the user's own X server.
        def chunk(tag, content):
            return struct.pack(">I", len(content)) + tag + content + struct.pack(">I", zlib.crc32(tag + content))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR",struct.pack(">IIBBBBB",512,512,8,2,0,0,0))
               + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + os.urandom(1536) for _ in range(512))))
               + chunk(b"IEND",b""))
        gif = bytes.fromhex("47494638396101000100800000000000ffffff2c00000000010001000002024401003b")
        display_r, display_w = os.pipe()
        server = subprocess.Popen(["Xvfb","-displayfd",str(display_w),"-screen","0","800x600x24","-nolisten","tcp"],
                                  pass_fds=(display_w,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(display_w)
        owner = None
        try:
            with os.fdopen(display_r) as f:
                display = ":" + f.readline().strip()
            self.assertRegex(display, r"^:[0-9]+$")
            env = {**os.environ, "DISPLAY": display, "GDK_BACKEND": "x11", "NO_AT_BRIDGE":"1"}
            owner_code = ("import os,runpy,sys; m=runpy.run_path(sys.argv[1]); "
                          "m['serve'](m['gtk_api'](),m['read_image'](sys.stdin.buffer),1)")
            owner = subprocess.Popen([sys.executable,"-I","-c",owner_code,str(SCRIPT)], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            owner.stdin.write(frame([("image/gif",len(gif)),("image/png",len(png))], gif+png))
            owner.stdin.close()
            self.assertEqual(owner.stdout.read(1),b"1")
            for mime,data in [("image/png",png),("image/gif",gif)]:
                self.assertEqual(subprocess.check_output(["xclip","-selection","clipboard","-o","-t",mime],
                                                         env=env,timeout=10),data)
            subprocess.run(["xclip","-selection","clipboard","-i","-loops","1"],input=b"new",env=env,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8,check=True)
            self.assertEqual(owner.wait(timeout=5),0)
        finally:
            if owner:
                if owner.poll() is None: owner.kill()
                owner.wait()
                owner.stdout.close()
            server.terminate(); server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
