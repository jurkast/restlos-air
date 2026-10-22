"""Signierte Programmupdates – dasselbe Verfahren wie Restlos Bewerbung und Uninstaller.

Ablauf:
1. Stündlich lädt Restlos das Manifest ``restlos-update.json`` samt Ed25519-Signatur aus dem
   neuesten GitHub-Release und prüft die Signatur mit dem öffentlichen Schlüssel der
   *installierten* Basisversion (Vertrauensanker ist nie das Update selbst).
2. Ist die Version neuer, wird das Archiv geladen, Größe und SHA-256 geprüft und sicher nach
   ``~/.local/share/restlos-air-updates/versions/<Version>`` entpackt. Keine Root-Rechte,
   kein Eingriff in das Systempaket (Autostart, Treiberkonfiguration bleiben unverändert).
3. ``run_air.py`` der Basisversion startet beim nächsten Öffnen die geprüfte Version.
   Meldet diese ihren erfolgreichen Start nicht (Handshake über ``launch-pending.json``),
   schaltet Restlos Air automatisch zurück und sperrt nur diese eine fehlerhafte Version.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener


APPLICATION = "restlos-air"
DEFAULT_REPOSITORY = "jurkast/restlos-air"
MANIFEST_NAME = "restlos-update.json"
SIGNATURE_NAME = "restlos-update.sig"
PUBLIC_KEY_NAME = "update-public.pem"
CHECK_INTERVAL_SECONDS = 60 * 60
FIRST_CHECK_DELAY_SECONDS = 20
NETWORK_TIMEOUT_SECONDS = 20.0
DOWNLOAD_DEADLINE_SECONDS = 600.0
MAX_MANIFEST_BYTES = 64_000
MAX_SIGNATURE_BYTES = 256
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 500
MINIMUM_FREE_BYTES = 50 * 1024 * 1024
TRUSTED_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
REQUIRED_FILES = ("run_air.py", "air_studio.py", "version.py", "updater.py", "sensors.py", "update-public.pem")
ARCHIVE_PREFIX = "RestlosAir"

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)

Version = tuple[int, int, int]


class UpdateError(RuntimeError):
    """Erwartbarer, verständlich formulierter Update-Fehler."""


class Opener(Protocol):
    def open(self, request: Request, timeout: float) -> Any: ...


def parse_version(value: object) -> Version:
    if not isinstance(value, str) or (match := _VERSION.fullmatch(value.strip())) is None:
        raise UpdateError("Ungültige Versionsnummer.")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def version_of(root: Path) -> str:
    """Version eines Programmordners lesen, ohne dessen Code zu importieren."""
    try:
        text = (root / "version.py").read_text(encoding="utf-8")
    except OSError as error:
        raise UpdateError("Die Programmversion konnte nicht gelesen werden.") from error
    match = _VERSION_LINE.search(text)
    if match is None:
        raise UpdateError("Die Programmversion konnte nicht gelesen werden.")
    parse_version(match.group(1))
    return match.group(1)


def trusted_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and (parsed.hostname or "").casefold() in TRUSTED_HOSTS
    )


class _TrustedRedirects(HTTPRedirectHandler):
    # Jeder Umleitungsschritt wird geprüft, nicht nur das Ziel.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not trusted_url(newurl):
            raise UpdateError("Der Download wurde an eine unerwartete Adresse umgeleitet.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener() -> OpenerDirector:
    return build_opener(_TrustedRedirects())


@dataclass(frozen=True, slots=True)
class Manifest:
    version: str
    minimum_base: str
    archive: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    running: str
    pending: str
    automatic: bool
    repository: str
    kind: str
    version: str
    detail: str
    checked: float | None

    @property
    def restart_ready(self) -> bool:
        return bool(self.pending)


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > 100_000:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _default_home() -> Path:
    if configured := os.environ.get("RESTLOS_AIR_UPDATE_DIR"):
        return Path(configured)
    data_home = os.environ.get("XDG_DATA_HOME")
    return (Path(data_home) if data_home else Path.home() / ".local/share") / "restlos-air-updates"


class Updater:
    def __init__(
        self,
        *,
        root: Path | None = None,
        base: Path | None = None,
        home: Path | None = None,
        opener: Opener | None = None,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = (root or Path(__file__).resolve().parent).resolve()
        self.base = (base or Path(os.environ.get("RESTLOS_AIR_BASE_DIR", str(self.root)))).resolve()
        self.home = home or _default_home()
        self._opener = opener or _default_opener()
        self._runner = runner
        self._clock = clock

    # ----- Zustand -------------------------------------------------------------------------
    @property
    def versions(self) -> Path:
        return self.home / "versions"

    def running_version(self) -> str:
        return version_of(self.root)

    def base_version(self) -> str:
        return version_of(self.base)

    def config(self) -> dict[str, Any]:
        stored = _read_json(self.home / "settings.json")
        repository = stored.get("repository")
        return {
            "repository": repository if isinstance(repository, str) and _valid_repository(repository) else DEFAULT_REPOSITORY,
            "automatic": stored.get("automatic", True) is not False,
            "blocked": stored.get("blocked", "") if isinstance(stored.get("blocked"), str) else "",
        }

    def save_config(self, *, repository: str | None = None, automatic: bool | None = None, blocked: str | None = None) -> None:
        current = self.config()
        if repository is not None:
            repository = repository.strip()
            if not _valid_repository(repository):
                raise UpdateError("Bitte GitHub-Benutzer/Repository eingeben, zum Beispiel jurkast/restlos-air.")
            current["repository"] = repository
        if automatic is not None:
            current["automatic"] = bool(automatic)
        if blocked is not None:
            current["blocked"] = blocked
        _write_json(self.home / "settings.json", current)

    def status(self) -> UpdateStatus:
        config = self.config()
        active = _read_json(self.home / "current.json").get("version", "")
        running = self.running_version()
        pending = ""
        if isinstance(active, str) and active and active != running:
            try:
                if parse_version(active) > parse_version(running):
                    pending = active
            except UpdateError:
                pending = ""
        stored = _read_json(self.home / "status.json")
        checked = stored.get("checked")
        return UpdateStatus(
            running=running,
            pending=pending,
            automatic=config["automatic"],
            repository=config["repository"],
            kind=str(stored.get("kind", "")),
            version=str(stored.get("version", "")),
            detail=str(stored.get("detail", "")),
            checked=float(checked) if isinstance(checked, (int, float)) and not isinstance(checked, bool) else None,
        )

    def _record(self, kind: str, *, version: str = "", detail: str = "") -> None:
        _write_json(
            self.home / "status.json",
            {"kind": kind, "version": version, "detail": detail, "checked": self._clock()},
        )

    # ----- Prüfung -------------------------------------------------------------------------
    def verify(self, manifest_path: Path, signature_path: Path) -> Manifest:
        public_key = self.base / PUBLIC_KEY_NAME
        if not public_key.is_file():
            raise UpdateError("Der öffentliche Update-Schlüssel fehlt in dieser Installation.")
        try:
            result = self._runner(
                ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key), "-rawin",
                 "-in", str(manifest_path), "-sigfile", str(signature_path)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError as error:
            raise UpdateError("OpenSSL fehlt. Bitte das Paket „openssl“ installieren.") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError("Die Update-Signatur konnte nicht geprüft werden.") from error
        if result.returncode != 0:
            raise UpdateError("Die Update-Signatur ist ungültig. Es wird nichts installiert.")
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise UpdateError("Ungültige Update-Beschreibung.") from error
        if not isinstance(raw, dict) or raw.get("application") != APPLICATION or raw.get("architecture") != "all":
            raise UpdateError("Das Update passt nicht zu Restlos Air.")
        version = raw.get("version")
        minimum_base = raw.get("minimum_base")
        archive = raw.get("archive")
        size = raw.get("size")
        digest = raw.get("sha256")
        parse_version(version)
        parse_version(minimum_base)
        if not isinstance(archive, str) or archive != f"{ARCHIVE_PREFIX}-{version}.tar.gz":
            raise UpdateError("Ungültige Update-Beschreibung.")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise UpdateError("Ungültige Update-Beschreibung.")
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise UpdateError("Ungültige Update-Größe.")
        if parse_version(minimum_base) > parse_version(self.base_version()):
            raise UpdateError("Dieses Update benötigt einmalig ein neueres Installationspaket.")
        return Manifest(str(version), str(minimum_base), archive, size, digest)

    def fetch(self, url: str, destination: Path, limit: int) -> None:
        if not trusted_url(url):
            raise UpdateError("Ungültige Update-Adresse.")
        request = Request(url, headers={"User-Agent": f"Restlos-Air-Updater/{self.running_version()}"})
        try:
            response = self._opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS)
        except HTTPError as error:
            if error.code == 404:
                raise UpdateError("Noch kein passendes Update-Release gefunden.") from error
            raise UpdateError(f"GitHub ist momentan nicht erreichbar (HTTP {error.code}).") from error
        except UpdateError:
            raise
        except (URLError, OSError, TimeoutError) as error:
            raise UpdateError("Die Updatequelle ist momentan nicht erreichbar. Die installierte Version bleibt nutzbar.") from error
        started = time.monotonic()
        total = 0
        try:
            with response, destination.open("xb") as output:
                while block := response.read(256 * 1024):
                    total += len(block)
                    if total > limit:
                        raise UpdateError("Die Update-Datei überschreitet die erlaubte Größe.")
                    if time.monotonic() - started > DOWNLOAD_DEADLINE_SECONDS:
                        raise UpdateError("Der Download hat zu lange gedauert. Bitte später erneut versuchen.")
                    output.write(block)
        except UpdateError:
            raise
        except (OSError, TimeoutError) as error:
            raise UpdateError("Die Updatequelle ist momentan nicht erreichbar. Die installierte Version bleibt nutzbar.") from error

    # ----- Vorbereitung --------------------------------------------------------------------
    def stage(self, manifest_path: Path, signature_path: Path, archive_path: Path) -> str:
        """Geprüftes Archiv für den nächsten Start bereitlegen. Gibt die Statusart zurück."""
        manifest = self.verify(manifest_path, signature_path)
        version = parse_version(manifest.version)
        if version <= parse_version(self.running_version()):
            return "current"
        if manifest.version == self.config()["blocked"]:
            return "blocked"
        active = _read_json(self.home / "current.json")
        active_version = active.get("version", "")
        if active_version == manifest.version and (self.versions / manifest.version / "run_air.py").is_file():
            return "ready"
        if isinstance(active_version, str) and active_version:
            try:
                if parse_version(active_version) > version:
                    return "ready"
            except UpdateError:
                pass
        if archive_path.stat().st_size != manifest.size or _sha256(archive_path) != manifest.sha256:
            raise UpdateError("Die Update-Datei ist beschädigt oder verändert. Es wird nichts installiert.")
        _private_dir(self.home)
        if shutil.disk_usage(self.home).free < MINIMUM_FREE_BYTES:
            raise UpdateError("Für ein sicheres Update werden mindestens 50 MB freier Speicher benötigt.")
        with tempfile.TemporaryDirectory(prefix="staging-", dir=self.home) as temporary:
            release_root = extract_release_archive(archive_path, Path(temporary) / "extract", manifest.version)
            for required in REQUIRED_FILES:
                if not (release_root / required).is_file():
                    raise UpdateError(f"Das Update ist unvollständig: {required}")
            if version_of(release_root) != manifest.version:
                raise UpdateError("Die Versionsangabe im Update stimmt nicht überein.")
            shutil.copy2(manifest_path, release_root / "release.json")
            shutil.copy2(signature_path, release_root / "release.sig")
            destination = _private_dir(self.versions) / manifest.version
            if destination.exists():
                shutil.rmtree(destination)
            release_root.rename(destination)
        previous = active_version if isinstance(active_version, str) else ""
        _write_json(self.home / "current.json", {"version": manifest.version, "previous": previous})
        return "ready"

    def supported(self) -> bool:
        """Nur Installationen mit ``run_air.py`` und Schlüssel können eine vorbereitete Version starten.

        Ein direkter Start aus dem Quellordner (``Starten.sh`` ohne Paket) aktualisiert ``git pull``.
        """
        return (self.base / "run_air.py").is_file() and (self.base / PUBLIC_KEY_NAME).is_file()

    def check(self) -> UpdateStatus:
        config = self.config()
        base_url = f"https://github.com/{config['repository']}/releases/latest/download/"
        _private_dir(self.home)
        if not self.supported():
            self._record("error", detail="Diese Installation wird über git pull aktualisiert, nicht über das In-App-Update.")
            return self.status()
        try:
            with tempfile.TemporaryDirectory(prefix="download-", dir=self.home) as temporary:
                folder = Path(temporary)
                manifest_path = folder / MANIFEST_NAME
                signature_path = folder / SIGNATURE_NAME
                self.fetch(base_url + MANIFEST_NAME, manifest_path, MAX_MANIFEST_BYTES)
                self.fetch(base_url + SIGNATURE_NAME, signature_path, MAX_SIGNATURE_BYTES)
                manifest = self.verify(manifest_path, signature_path)
                active = _read_json(self.home / "current.json").get("version")
                if parse_version(manifest.version) <= parse_version(self.running_version()):
                    kind = "current"
                elif manifest.version == config["blocked"]:
                    kind = "blocked"
                elif active == manifest.version and (self.versions / manifest.version / "run_air.py").is_file():
                    kind = "ready"
                else:
                    archive_path = folder / manifest.archive
                    self.fetch(base_url + manifest.archive, archive_path, manifest.size)
                    kind = self.stage(manifest_path, signature_path, archive_path)
                self._record(kind, version=manifest.version)
        except UpdateError as error:
            self._record("error", detail=str(error))
        except (OSError, ValueError) as error:
            self._record("error", detail=f"Update konnte nicht vorbereitet werden: {error}")
        return self.status()

    # ----- Start und Rückfall --------------------------------------------------------------
    def rollback(self, *, failed_version: str = "") -> None:
        active = _read_json(self.home / "current.json")
        previous = active.get("previous", "")
        if isinstance(previous, str) and previous and (self.versions / previous / "run_air.py").is_file():
            _write_json(self.home / "current.json", {"version": previous, "previous": ""})
        else:
            (self.home / "current.json").unlink(missing_ok=True)
        (self.home / "launch-pending.json").unlink(missing_ok=True)
        blocked = failed_version or str(active.get("version", ""))
        self.save_config(blocked=blocked)
        self._record("rolled-back", version=blocked)

    def abandon_failed_launch(self, version: str, *, crashed: bool) -> bool:
        """Nach dem Ende einer gestarteten Version: hat sie ihren Start nie bestätigt?

        Gibt True zurück, wenn sie abgestürzt ist und deshalb sofort zurückgeschaltet wurde.
        Ohne Absturz (z. B. beim Abmelden beendet) wird nur der offene Start-Handshake verworfen,
        damit der nächste Start nicht fälschlich zurückschaltet.
        """
        pending = _read_json(self.home / "launch-pending.json")
        if pending.get("version") != version or pending.get("pid") != os.getpid():
            return False
        if not crashed:
            (self.home / "launch-pending.json").unlink(missing_ok=True)
            return False
        self.rollback(failed_version=version)
        return True

    def startup_ready(self) -> None:
        pending = _read_json(self.home / "launch-pending.json")
        if pending.get("version") == self.running_version():
            (self.home / "launch-pending.json").unlink(missing_ok=True)
        try:
            self.prune_versions()
        except OSError:
            pass

    def prune_versions(self) -> list[str]:
        """Nur laufende, aktive und vorherige Version behalten (für den Rückfall)."""
        if not self.versions.is_dir():
            return []
        active = _read_json(self.home / "current.json")
        keep = {self.running_version(), str(active.get("version", "")), str(active.get("previous", ""))}
        pending = _read_json(self.home / "launch-pending.json").get("version")
        if isinstance(pending, str):
            keep.add(pending)
        removed: list[str] = []
        for entry in self.versions.iterdir():
            if entry.name in keep or entry.is_symlink() or not entry.is_dir():
                continue
            try:
                parse_version(entry.name)
            except UpdateError:
                continue
            shutil.rmtree(entry)
            removed.append(entry.name)
        return sorted(removed)

    def choose_launch(self) -> Path:
        """Basis oder geprüfte, vorbereitete Version für diesen Start wählen."""
        active = _read_json(self.home / "current.json")
        version = active.get("version")
        if not version:
            return self.base
        try:
            if parse_version(version) <= parse_version(self.base_version()):
                return self.base
            pending = _read_json(self.home / "launch-pending.json")
            if pending.get("version") == version:
                pid = pending.get("pid")
                if type(pid) is int and pid > 0 and _process_alive(pid):
                    # Eine laufende Instanz ist kein gescheiterter Start.
                    return self.base
                self.rollback(failed_version=str(version))
                return self.choose_launch()
            target = self.versions / str(version)
            manifest = self.verify(target / "release.json", target / "release.sig")
            if manifest.version != version or version_of(target) != version:
                raise UpdateError("Versionsprüfung fehlgeschlagen.")
            for required in REQUIRED_FILES:
                if not (target / required).is_file():
                    raise UpdateError(f"Das Update ist unvollständig: {required}")
            _write_json(self.home / "launch-pending.json", {"version": version, "pid": os.getpid()})
            return target
        except (UpdateError, OSError) as error:
            self.rollback(failed_version=str(version))
            self._record("error", detail=f"Update konnte nicht gestartet werden: {error}")
            return self.base


def _valid_repository(value: str) -> bool:
    return bool(_REPOSITORY.fullmatch(value)) and not any(part in {".", ".."} for part in value.split("/"))


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def extract_release_archive(archive_path: Path, destination: Path, version: str) -> Path:
    expected_root = f"{ARCHIVE_PREFIX}-{version}"
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    seen: set[str] = set()
    total_size = 0
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_ARCHIVE_MEMBERS:
                raise UpdateError("Das Update-Archiv enthält unerwartet viele Einträge.")
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or not path.parts
                    or path.parts[0] != expected_root
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or "\\" in member.name
                    or member.name in seen
                ):
                    raise UpdateError("Das Update-Archiv enthält einen unsicheren Pfad.")
                seen.add(member.name)
                if not (member.isdir() or member.isfile()):
                    raise UpdateError("Links oder Spezialdateien im Update-Archiv wurden abgelehnt.")
                total_size += max(member.size, 0)
                if total_size > MAX_EXTRACTED_BYTES:
                    raise UpdateError("Der entpackte Inhalt überschreitet das Größenlimit.")
                output_path = destination.joinpath(*path.parts)
                if member.isdir():
                    output_path.mkdir(parents=True, mode=0o700, exist_ok=True)
                    continue
                output_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise UpdateError("Eine Datei im Update-Archiv konnte nicht gelesen werden.")
                with source, output_path.open("xb") as output:
                    shutil.copyfileobj(source, output, length=128 * 1024)
                output_path.chmod(0o700 if member.mode & 0o111 else 0o600)
    except (OSError, tarfile.TarError) as error:
        raise UpdateError(f"Das Update-Archiv konnte nicht entpackt werden: {error}") from error
    release_root = destination / expected_root
    if not release_root.is_dir():
        raise UpdateError("Das Update-Archiv besitzt keine gültige Wurzel.")
    return release_root
