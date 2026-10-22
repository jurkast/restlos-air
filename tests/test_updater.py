from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import updater as u  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HAS_OPENSSL = shutil.which("openssl") is not None


def _program(root: Path, version: str, key: Path | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    for name in ("run_air.py", "air_studio.py", "updater.py", "sensors.py"):
        (root / name).write_text("# test\n", encoding="utf-8")
    (root / u.PUBLIC_KEY_NAME).write_bytes(key.read_bytes() if key else b"placeholder")
    return root


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self


class _Opener:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requested: list[str] = []

    def open(self, request: Request, timeout: float) -> Any:
        url = request.full_url
        self.requested.append(url)
        name = url.rsplit("/", 1)[-1]
        if name not in self.files:
            raise HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]
        return _Response(self.files[name])


@unittest.skipUnless(HAS_OPENSSL, "OpenSSL wird für die Signaturprüfung benötigt")
class SignedUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="restlos-update-test-")
        self.work = Path(self.temporary.name)
        self.base = _program(self.work / "base", "2.1.0")
        self.key = self.work / "private.pem"
        self.public = self.base / u.PUBLIC_KEY_NAME
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.key)], check=True,
                       capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(self.key), "-pubout", "-out",
                        str(self.public)], check=True, capture_output=True)
        self.home = self.work / "updates"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def release(self, version: str, *, unsafe: bool = False, wrong_version: bool = False,
                minimum_base: str = "2.0.0") -> dict[str, bytes]:
        source = _program(self.work / f"src-{version}" / f"RestlosAir-{version}", "9.9.9" if wrong_version else version,
                          self.public)
        archive = self.work / f"RestlosAir-{version}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(source, arcname=f"RestlosAir-{version}")
            if unsafe:
                data = b"bad"
                info = tarfile.TarInfo("../escape.txt")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        payload = archive.read_bytes()
        manifest = {
            "application": u.APPLICATION, "architecture": "all", "version": version,
            "minimum_base": minimum_base, "archive": archive.name, "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        manifest_path = self.work / f"manifest-{version}.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        signature_path = self.work / f"manifest-{version}.sig"
        subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(self.key), "-rawin", "-in",
                        str(manifest_path), "-out", str(signature_path)], check=True, capture_output=True)
        return {u.MANIFEST_NAME: manifest_path.read_bytes(), u.SIGNATURE_NAME: signature_path.read_bytes(),
                archive.name: payload}

    def updater(self, files: dict[str, bytes] | None = None, root: Path | None = None) -> u.Updater:
        return u.Updater(root=root or self.base, base=self.base, home=self.home, opener=_Opener(files or {}),
                         clock=lambda: 1_000.0)

    def test_check_stages_signed_update_and_launch_handshake(self) -> None:
        files = self.release("2.2.0")
        status = self.updater(files).check()
        self.assertEqual((status.kind, status.version, status.pending), ("ready", "2.2.0", "2.2.0"))
        staged = self.home / "versions/2.2.0"
        self.assertTrue((staged / "release.sig").is_file())
        self.assertEqual(staged.stat().st_mode & 0o777, 0o700)

        launcher = self.updater()
        self.assertEqual(launcher.choose_launch(), staged)
        self.assertTrue((self.home / "launch-pending.json").exists())
        self.updater(root=staged).startup_ready()
        self.assertFalse((self.home / "launch-pending.json").exists())
        # Erneute Prüfung lädt nichts doppelt.
        opener = _Opener(files)
        again = u.Updater(root=self.base, base=self.base, home=self.home, opener=opener).check()
        self.assertEqual(again.kind, "ready")
        self.assertEqual(len(opener.requested), 2)

    def test_failed_start_rolls_back_and_blocks_only_that_version(self) -> None:
        self.updater(self.release("2.2.0")).check()
        u._write_json(self.home / "launch-pending.json", {"version": "2.2.0", "pid": 2**30})
        launcher = self.updater()
        self.assertEqual(launcher.choose_launch(), self.base)
        self.assertFalse((self.home / "current.json").exists())
        self.assertTrue(launcher.config()["automatic"])
        self.assertEqual(launcher.config()["blocked"], "2.2.0")
        self.assertEqual(self.updater(self.release("2.2.0")).check().kind, "blocked")
        # Die nächste, korrigierte Version kommt wieder automatisch.
        self.assertEqual(self.updater(self.release("2.2.1")).check().kind, "ready")

    def test_running_instance_is_not_a_failed_start(self) -> None:
        self.updater(self.release("2.2.0")).check()
        u._write_json(self.home / "launch-pending.json", {"version": "2.2.0", "pid": os.getpid()})
        self.assertEqual(self.updater().choose_launch(), self.base)
        self.assertTrue((self.home / "current.json").exists())

    def test_tampered_signature_hash_and_paths_are_rejected(self) -> None:
        files = self.release("2.2.0")
        files[u.SIGNATURE_NAME] = b"0" * 64
        status = self.updater(files).check()
        self.assertEqual(status.kind, "error")
        self.assertIn("Signatur", status.detail)

        files = self.release("2.2.0")
        files["RestlosAir-2.2.0.tar.gz"] += b"x"
        self.assertEqual(self.updater(files).check().kind, "error")

        status = self.updater(self.release("2.3.0", unsafe=True)).check()
        self.assertEqual(status.kind, "error")
        self.assertFalse((self.work / "escape.txt").exists())
        self.assertFalse((self.home / "current.json").exists())

    def test_version_mismatch_and_minimum_base(self) -> None:
        self.assertEqual(self.updater(self.release("2.2.0", wrong_version=True)).check().kind, "error")
        status = self.updater(self.release("3.0.0", minimum_base="3.0.0")).check()
        self.assertEqual(status.kind, "error")
        self.assertIn("Installationspaket", status.detail)

    def test_tampered_staged_copy_is_not_started(self) -> None:
        self.updater(self.release("2.2.0")).check()
        (self.home / "versions/2.2.0/release.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.updater().choose_launch(), self.base)
        self.assertEqual(self.updater().status().kind, "error")

    def test_crash_before_handshake_rolls_back_immediately(self) -> None:
        updater = self.updater(self.release("2.1.1"))
        self.assertEqual(updater.check().pending, "2.1.1")
        self.assertEqual(updater.choose_launch(), self.home / "versions/2.1.1")
        self.assertTrue(updater.abandon_failed_launch("2.1.1", crashed=True))
        status = updater.status()
        self.assertEqual((status.pending, status.kind), ("", "rolled-back"))
        self.assertEqual(updater.config()["blocked"], "2.1.1")
        self.assertEqual(updater.choose_launch(), self.base)

    def test_signal_or_confirmed_start_is_not_a_failure(self) -> None:
        updater = self.updater(self.release("2.1.1"))
        updater.check()
        updater.choose_launch()
        self.assertFalse(updater.abandon_failed_launch("2.1.1", crashed=False))
        self.assertFalse((self.home / "launch-pending.json").exists())
        self.assertEqual(updater.status().pending, "2.1.1")
        updater.choose_launch()
        updater.startup_ready.__func__(u.Updater(root=self.home / "versions/2.1.1", base=self.base, home=self.home))
        self.assertFalse(updater.abandon_failed_launch("2.1.1", crashed=True))
        self.assertEqual(updater.status().pending, "2.1.1")

    def test_installation_without_launcher_is_not_updated_in_app(self) -> None:
        (self.base / "run_air.py").unlink()
        opener = _Opener(self.release("2.1.1"))
        status = u.Updater(root=self.base, base=self.base, home=self.home, opener=opener, clock=lambda: 1.0).check()
        self.assertEqual(status.kind, "error")
        self.assertIn("git pull", status.detail)
        self.assertEqual(opener.requested, [])
        self.assertFalse((self.home / "versions").exists())

    def test_current_and_offline(self) -> None:
        self.assertEqual(self.updater(self.release("2.1.0")).check().kind, "current")
        status = self.updater({}).check()
        self.assertEqual(status.kind, "error")
        self.assertTrue(self.updater().status().automatic)

    def test_newer_base_install_wins_and_old_versions_are_pruned(self) -> None:
        self.updater(self.release("2.2.0")).check()
        self.updater(self.release("2.3.0")).check()
        (self.home / "versions/2.1.5").mkdir()
        removed = self.updater().prune_versions()
        self.assertEqual(removed, ["2.1.5"])
        self.assertTrue((self.home / "versions/2.2.0").is_dir())
        newer_base = _program(self.work / "newbase", "2.4.0")
        shutil.copy2(self.public, newer_base / u.PUBLIC_KEY_NAME)
        launcher = u.Updater(root=newer_base, base=newer_base, home=self.home, opener=_Opener({}))
        self.assertEqual(launcher.choose_launch(), newer_base)


class UpdateHelperTests(unittest.TestCase):
    def test_trusted_urls(self) -> None:
        self.assertTrue(u.trusted_url("https://github.com/jurkast/restlos-air/releases/latest/download/x"))
        self.assertTrue(u.trusted_url("https://release-assets.githubusercontent.com/x"))
        self.assertFalse(u.trusted_url("http://github.com/x"))
        self.assertFalse(u.trusted_url("https://evil.example/x"))
        self.assertFalse(u.trusted_url("https://user:pw@github.com/x"))

    def test_repository_setting_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            updater = u.Updater(home=Path(temporary))
            self.assertEqual(updater.config()["repository"], u.DEFAULT_REPOSITORY)
            with self.assertRaises(u.UpdateError):
                updater.save_config(repository="../evil")
            updater.save_config(automatic=False)
            self.assertFalse(updater.config()["automatic"])
            self.assertEqual((Path(temporary) / "settings.json").stat().st_mode & 0o777, 0o600)

    def test_project_ships_same_public_key_and_matching_versions(self) -> None:
        app = PROJECT_ROOT / "app"
        key = (app / u.PUBLIC_KEY_NAME).read_text(encoding="ascii")
        self.assertIn("BEGIN PUBLIC KEY", key)
        self.assertNotIn("PRIVATE", key)
        # Same Ed25519 key as Restlos Bewerbung / Uninstaller (RESTLOS_SIGNING_KEY).
        self.assertIn("MCowBQYDK2VwAyEA0L+XOjKx8S8TnNC/o0UuEhJR7LAhFlGdYqeWfmxZrZM=", key)
        self.assertEqual(u.DEFAULT_REPOSITORY, "jurkast/restlos-air")
        control = (PROJECT_ROOT / "packaging/control").read_text(encoding="utf-8")
        self.assertIn(f"Version: {u.version_of(app)}\n", control)
        self.assertIn("run_air.py", (PROJECT_ROOT / "packaging/restlos-air").read_text(encoding="utf-8"))

    def test_release_archive_contains_program_and_stages(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_release", PROJECT_ROOT / "scripts/build_release.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            archive = module.build(Path(temporary))
            version = u.version_of(PROJECT_ROOT / "app")
            root = u.extract_release_archive(archive, Path(temporary) / "x", version)
            for name in u.REQUIRED_FILES:
                self.assertTrue((root / name).is_file(), name)
            self.assertEqual(u.version_of(root), version)

if __name__ == "__main__":
    unittest.main()
