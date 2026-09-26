import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agentviz import app
from agentviz.render import VT_LEGACY, VT_NO_CONSOLE, VT_OK, windows_vt_status


class FakeKernel32:
    """Stand-in for ctypes.windll.kernel32 with configurable console behaviour."""

    def __init__(self, console=True, mode=0x3, accepts=True, keeps_flag=True):
        self.console, self.mode, self.accepts, self.keeps_flag = console, mode, accepts, keeps_flag

    def GetStdHandle(self, n):
        return 7

    def GetConsoleMode(self, h, ref):
        if not self.console:
            return 0
        ref._obj.value = self.mode
        return 1

    def SetConsoleMode(self, h, mode):
        if not self.accepts:
            return 0
        self.mode = mode if self.keeps_flag else mode & ~0x4
        return 1


class VtStatusTest(unittest.TestCase):
    def test_modern_console(self):
        k = FakeKernel32()
        self.assertEqual(windows_vt_status(k), VT_OK)
        self.assertTrue(k.mode & 0x4)

    def test_already_enabled(self):
        self.assertEqual(windows_vt_status(FakeKernel32(mode=0x7, accepts=False)), VT_OK)

    def test_legacy_rejects_flag(self):
        self.assertEqual(windows_vt_status(FakeKernel32(accepts=False)), VT_LEGACY)

    def test_legacy_silently_drops_flag(self):
        self.assertEqual(windows_vt_status(FakeKernel32(keeps_flag=False)), VT_LEGACY)

    def test_redirected_output(self):
        self.assertEqual(windows_vt_status(FakeKernel32(console=False)), VT_NO_CONSOLE)

    def test_broken_api_counts_as_legacy(self):
        class Boom:
            def GetStdHandle(self, n):
                raise OSError("no console api")
        self.assertEqual(windows_vt_status(Boom()), VT_LEGACY)


class LegacyRunTest(unittest.TestCase):
    def run_app(self, argv):
        out, err = io.StringIO(), io.StringIO()
        out.isatty = lambda: True
        with mock.patch.object(app, "windows_vt_status", return_value=VT_LEGACY), \
                redirect_stdout(out), redirect_stderr(err):
            code = app.run(argv)
        return code, out.getvalue(), err.getvalue()

    def test_dashboard_refuses_with_guidance(self):
        code, out, err = self.run_app(["--demo"])
        self.assertEqual(code, 2)
        self.assertIn("Windows Terminal", err)
        self.assertIn("--once", err)
        self.assertNotIn("\x1b[", out)  # nothing drawn

    def test_once_still_works_in_plain_ascii(self):
        code, out, err = self.run_app(["--demo", "--once"])
        self.assertEqual(code, 0)
        self.assertIn("AGENTVIZ", out)
        self.assertNotIn("\x1b[", out)
        ui = [c for c in out if not "\uac00" <= c <= "\ud7a3"]  # ignore Hangul in demo data
        self.assertTrue(all(ord(c) < 128 for c in ui), "UI chrome should fall back to ASCII")


if __name__ == "__main__":
    unittest.main()
