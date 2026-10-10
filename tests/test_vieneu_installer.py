"""Test cài VieNeu từ giao diện (không chạy pip thật). Chạy: python -m unittest discover -s tests -v"""
import io
import sys
import unittest
from unittest import mock

from douyin_manager import vieneu_installer as vi


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self._rc = rc

    def wait(self):
        return self._rc

    def kill(self):
        pass


class InstallerTests(unittest.TestCase):
    def test_already_installed_skips_pip(self):
        with mock.patch.object(vi, "is_installed", return_value=True), \
                mock.patch.object(vi, "installed_version", return_value="1.2.3"), \
                mock.patch.object(vi.subprocess, "Popen") as popen:
            ok, msg = vi.install_vieneu()
        self.assertTrue(ok)
        self.assertIn("1.2.3", msg)
        popen.assert_not_called()

    def test_python_too_old(self):
        with mock.patch.object(vi, "is_installed", return_value=False), \
                mock.patch.object(vi, "python_ok", return_value=False):
            ok, msg = vi.install_vieneu()
        self.assertFalse(ok)
        self.assertIn("Python 3.10", msg)

    def test_pip_success_reports_progress(self):
        seen = []
        states = iter([False, True])        # trước khi cài: chưa có; sau khi cài: đã có
        with mock.patch.object(vi, "is_installed", side_effect=lambda: next(states)), \
                mock.patch.object(vi, "python_ok", return_value=True), \
                mock.patch.object(vi, "installed_version", return_value="0.9"), \
                mock.patch.object(vi, "_purge_vieneu_modules") as purge, \
                mock.patch.object(vi.subprocess, "Popen",
                                  return_value=FakeProc(["Collecting vieneu", "noise", "Successfully installed vieneu-0.9"])) as popen:
            ok, msg = vi.install_vieneu(seen.append)
        self.assertTrue(ok, msg)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[:4], [sys.executable, "-m", "pip", "install"])
        self.assertIn("vieneu", cmd)
        self.assertTrue(any(m.startswith("Collecting") for m in seen))
        self.assertFalse(any(m == "noise" for m in seen))
        self.assertEqual(purge.call_count, 2)      # trước pip (tắt worker cũ) và sau pip

    def test_pip_failure_returns_tail(self):
        with mock.patch.object(vi, "is_installed", return_value=False), \
                mock.patch.object(vi, "python_ok", return_value=True), \
                mock.patch.object(vi.subprocess, "Popen",
                                  return_value=FakeProc(["ERROR: No matching distribution"], rc=1)):
            ok, msg = vi.install_vieneu()
        self.assertFalse(ok)
        self.assertIn("No matching distribution", msg)

    def test_build_error_gives_actionable_message(self):
        lines = ["Building wheel for kaldi-native-fbank (pyproject.toml): finished with status 'error'",
                 "error: failed-wheel-build-for-install",
                 "Failed to build installable wheels for some pyproject.toml based projects",
                 "kaldi-native-fbank"]
        with mock.patch.object(vi, "is_installed", return_value=False), \
                mock.patch.object(vi, "python_ok", return_value=True), \
                mock.patch.object(vi.subprocess, "Popen", return_value=FakeProc(lines, rc=1)):
            ok, msg = vi.install_vieneu()
        self.assertFalse(ok)
        self.assertIn("kaldi-native-fbank", msg)
        self.assertIn("3.12", msg)
        self.assertIn("Gemini", msg)

    def test_frozen_without_external_python(self):
        with mock.patch.object(vi, "is_installed", return_value=False), \
                mock.patch.object(vi.sys, "frozen", True, create=True), \
                mock.patch.object(vi, "venv_python", return_value=vi.Path("/nonexistent/python")), \
                mock.patch.object(vi, "_find_external_python", return_value=None):
            ok, msg = vi.install_vieneu()
        self.assertFalse(ok)
        self.assertIn("python.org", msg)

    def test_frozen_creates_venv_then_pip_in_venv(self):
        import tempfile
        tmp = vi.Path(tempfile.mkdtemp())
        fake_py = tmp / "venv_python"

        def fake_run(cmd, **kw):
            self.assertEqual(cmd[-3:-1], ["-m", "venv"])
            fake_py.write_text("")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(vi.sys, "frozen", True, create=True), \
                mock.patch.object(vi, "VENV_DIR", tmp / "venv"), \
                mock.patch.object(vi, "venv_python", return_value=fake_py), \
                mock.patch.object(vi, "_find_external_python", return_value=["py", "-3.12"]), \
                mock.patch.object(vi.subprocess, "run", side_effect=fake_run):
            cmd, err = vi._build_cmd(upgrade=False)
        self.assertEqual(err, "")
        self.assertEqual(cmd[0], str(fake_py))
        self.assertEqual(cmd[1:4], ["-m", "pip", "install"])
        self.assertNotIn("--target", cmd)

    def test_use_worker_only_when_frozen_and_not_bundled(self):
        with mock.patch.object(vi.sys, "frozen", True, create=True), \
                mock.patch.object(vi, "_bundled_in_app", return_value=False):
            self.assertTrue(vi.use_worker())
        with mock.patch.object(vi.sys, "frozen", True, create=True), \
                mock.patch.object(vi, "_bundled_in_app", return_value=True):
            self.assertFalse(vi.use_worker())
        self.assertFalse(vi.use_worker())      # chạy từ mã nguồn

    def _fake_mac_pythons(self, versions):
        """Tạo thư mục giả kiểu Homebrew chứa python3.x; trả đường dẫn thư mục + hàm probe giả."""
        import tempfile
        d = vi.Path(tempfile.mkdtemp())
        by_path = {}
        for v in versions:
            exe = d / f"python{v}"
            exe.write_text("")
            by_path[str(exe)] = tuple(int(x) for x in v.split("."))

        def probe(cmd):
            return by_path.get(cmd[0], (3, 9) if cmd[0].endswith("python3") else None)
        return d, probe

    def test_macos_finds_homebrew_python_missing_from_gui_path(self):
        # Mở .app từ Finder: PATH chỉ có /usr/bin (python3 = 3.9 của Apple), không thấy Homebrew.
        d, probe = self._fake_mac_pythons(["3.12"])
        with mock.patch.object(vi.sys, "platform", "darwin"), \
                mock.patch.object(vi, "MAC_PYTHON_BIN_DIRS", (str(d),)), \
                mock.patch.object(vi, "MAC_PYTHON_FRAMEWORK", str(d / "nonexistent-{v}")), \
                mock.patch.object(vi.shutil, "which",
                                  side_effect=lambda n: "/usr/bin/python3" if n == "python3" else None), \
                mock.patch.object(vi, "_probe_python", side_effect=probe):
            cmd = vi._find_external_python()
        self.assertEqual(cmd, [str(d / "python3.12")])

    def test_macos_prefers_312_over_newer_generic_python3(self):
        d, probe = self._fake_mac_pythons(["3.12"])

        def probe2(cmd):
            return (3, 14) if cmd[0] == "/usr/local/bin/python3" else probe(cmd)
        with mock.patch.object(vi.sys, "platform", "darwin"), \
                mock.patch.object(vi, "MAC_PYTHON_BIN_DIRS", (str(d),)), \
                mock.patch.object(vi, "MAC_PYTHON_FRAMEWORK", str(d / "nonexistent-{v}")), \
                mock.patch.object(vi.shutil, "which",
                                  side_effect=lambda n: "/usr/local/bin/python3" if n == "python3" else None), \
                mock.patch.object(vi, "_probe_python", side_effect=probe2):
            cmd = vi._find_external_python()
        self.assertEqual(cmd, [str(d / "python3.12")])

    def test_macos_rejects_only_apple_python39(self):
        d, probe = self._fake_mac_pythons([])
        with mock.patch.object(vi.sys, "platform", "darwin"), \
                mock.patch.object(vi, "MAC_PYTHON_BIN_DIRS", (str(d),)), \
                mock.patch.object(vi, "MAC_PYTHON_FRAMEWORK", str(d / "nonexistent-{v}")), \
                mock.patch.object(vi.shutil, "which",
                                  side_effect=lambda n: "/usr/bin/python3" if n == "python3" else None), \
                mock.patch.object(vi, "_probe_python", side_effect=probe):
            self.assertIsNone(vi._find_external_python())

    def test_macos_error_message_mentions_brew(self):
        with mock.patch.object(vi, "venv_python", return_value=vi.Path("/nonexistent/python")), \
                mock.patch.object(vi.sys, "platform", "darwin"), \
                mock.patch.object(vi, "_find_external_python", return_value=None):
            cmd, err = vi._ensure_venv(None)
        self.assertIsNone(cmd)
        self.assertIn("brew install python@3.12", err)

    def test_second_install_rejected_while_running(self):
        self.assertTrue(vi._install_lock.acquire(blocking=False))
        try:
            ok, msg = vi.install_vieneu()
        finally:
            vi._install_lock.release()
        self.assertFalse(ok)
        self.assertIn("đợi", msg)

    def test_background_calls_on_done(self):
        done = []
        with mock.patch.object(vi, "install_vieneu", return_value=(True, "ok")):
            t = vi.install_vieneu_in_background(on_done=lambda ok, m: done.append((ok, m)))
            t.join(5)
        self.assertEqual(done, [(True, "ok")])


if __name__ == "__main__":
    unittest.main()