"""NKPI upload entrypoint. Keep nkpi_runtime.zip beside this file."""
from __future__ import annotations

import errno
import hashlib
import hmac
import importlib
import io
import json
import logging
import mimetypes
from pathlib import Path
import runpy
import shutil
import stat
import sys
import tempfile
import threading
import zipfile


RUNTIME_SHA256 = "eb5e97c52a9b4bfff81cbc2040430f36479efe2015a7559661c5598dbe7bd4d4"
APP_FILENAME = "kcn_dual_calculator_app.py"
BUNDLE_PATH = Path(__file__).resolve().with_name("nkpi_runtime.zip")
_MARKER = ".nkpi-runtime.sha256"
_SOURCE_REQUIRED = ('kcn_dual_calculator_app.py', 'nkpi/__init__.py', 'nkpi/service.py', 'nkpi/component.py', 'nkpi/recovery_ui.py', 'components/index.template.html', 'components/app.js', 'tools/build_delivery.py', 'components/delivery/ocr_delivery.js', 'components/delivery/extractor_guard.js', 'components/delivery/mount_extractor.js', 'components/extractor/index.html', 'components/extractor/app.js', 'components/extractor/archive_integration.js', 'components/extractor/vendor/tesseract-v7/tesseract.min.js', 'components/extractor/vendor/tesseract-v7/worker.min.js', 'components/extractor/vendor/tesseract-v7/tesseract-core-lstm.wasm.js', 'components/extractor/vendor/tesseract-v7/tesseract-core-simd-lstm.wasm.js', 'components/extractor/vendor/tesseract-v7/tesseract-core-relaxedsimd-lstm.wasm.js', 'components/extractor/ocr_data/eng.traineddata', 'components/extractor/ocr_second/eng.traineddata', 'models/release.json', 'models/release_model_baseline.json', 'models/release_model_longitudinal.json', 'FILE_MANIFEST.json')
_SOURCE_REQUIRED += ('components/replay/fluid_renderer.js', 'components/replay/webm_encoder.js', 'components/replay/replay_view.js', 'components/replay/gif_encoder.js')
_REQUIRED = _SOURCE_REQUIRED + ("components/index.html", "components/extractor.compiled.html", ".nkpi-delivery.json")
# Shared across Streamlit reruns and sessions, including an in-process update.
_STATE = sys.__dict__.setdefault(
    "_nkpi_packaged_runtime_state", {"lock": threading.RLock(), "path": None}
)


class BundleError(RuntimeError):
    """An incomplete, mismatched, or unreadable release package."""


def _installed(runtime: Path, digest: str) -> bool:
    try:
        marker = runtime / _MARKER
        if runtime.is_symlink() or marker.is_symlink():
            return False
        if marker.read_text(encoding="ascii") != digest:
            return False
        for relative in _REQUIRED:
            path = runtime / relative
            if not path.is_file():
                return False
            while path != runtime:
                if path.is_symlink():
                    return False
                path = path.parent
        record = json.loads((runtime / ".nkpi-delivery.json").read_text(encoding="utf-8"))
        for rel, key in [("components/index.html", "component_sha256"), ("components/extractor.compiled.html", "extractor_sha256")]:
            if not hmac.compare_digest(hashlib.sha256((runtime / rel).read_bytes()).hexdigest(), record[key]):
                return False
        return True
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return False


def _extract_checked(payload: bytes, staging: Path) -> None:
    """Validate all paths before writing into a private staging directory."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = []
        seen = set()
        for member in archive.infolist():
            name = member.orig_filename
            parts = name.rstrip("/").split("/")
            kind = stat.S_IFMT(member.external_attr >> 16)
            if (
                not name
                or "\x00" in name
                or "\\" in name
                or any(part in ("", ".", "..") or ":" in part for part in parts)
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                or (kind == stat.S_IFDIR and not member.is_dir())
                or member.flag_bits & 1
                or parts[0].casefold() == _MARKER.casefold()
            ):
                raise BundleError("The runtime ZIP contains an invalid file path or type. Upload the original release ZIP unchanged.")
            relative = "/".join(parts)
            # Case-insensitive collisions are also unsafe on Windows/macOS.
            identity = relative.casefold()
            if identity in seen:
                raise BundleError("The runtime ZIP contains duplicate files. Upload the original release ZIP unchanged.")
            seen.add(identity)
            entries.append((member, staging.joinpath(*parts)))
        for member, target in entries:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)


def prepare_runtime(
    bundle_path: Path = BUNDLE_PATH,
    expected_sha256: str = RUNTIME_SHA256,
    cache_base: Path | None = None,
) -> Path:
    """Verify the shipped bytes, then reuse or atomically install that release."""
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise BundleError("This entrypoint is incomplete. Upload kcn_dual_calculator_app.py from the finished release package.")
    try:
        payload = Path(bundle_path).read_bytes()
    except FileNotFoundError as exc:
        raise BundleError("Missing nkpi_runtime.zip. Upload it into the same GitHub folder as kcn_dual_calculator_app.py, then reboot the app.") from exc
    except OSError as exc:
        raise BundleError("Cannot read nkpi_runtime.zip. Re-upload that file from this release package.") from exc
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256):
        raise BundleError("The app file and nkpi_runtime.zip do not match, or the ZIP is incomplete. Upload both files together from the same release package.")

    cache_base = Path(cache_base) if cache_base is not None else Path(tempfile.gettempdir()) / "nkpi-runtime-cache"
    runtime = cache_base / expected_sha256
    staging = None
    try:
        cache_base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if cache_base.is_symlink():
            raise BundleError("The temporary NKPI installation location is invalid. Restart this app in a fresh runtime.")
        if _installed(runtime, expected_sha256):
            return runtime
        if runtime.exists() or runtime.is_symlink():
            raise BundleError("The temporary NKPI installation is incomplete. Restart this app in a fresh runtime to rebuild it.")

        staging = Path(tempfile.mkdtemp(prefix=f".{expected_sha256[:16]}-", dir=cache_base))
        _extract_checked(payload, staging)
        for relative in _SOURCE_REQUIRED:
            if not (staging / relative).is_file():
                raise BundleError(f"The runtime ZIP is missing {relative}. Re-upload the complete release package.")
        # Compile only trusted release assets, before the complete runtime is
        # published. No reports or user data enter the generated HTML.
        with _STATE["lock"]:
            builder = runpy.run_path(str(staging / "tools/build_delivery.py"), run_name="_nkpi_delivery_builder")
            builder["build"](staging)
        for relative in _REQUIRED:
            if not (staging / relative).is_file():
                raise BundleError("The embedded OCR page could not be assembled. Re-upload this matched release package.")
        (staging / _MARKER).write_text(expected_sha256, encoding="ascii")
        try:
            # The destination appears only after every file has been written.
            # A concurrent installer may already have published the same hash.
            staging.rename(runtime)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not _installed(runtime, expected_sha256):
                raise
        return runtime
    except BundleError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, NotImplementedError) as exc:
        raise BundleError("The runtime ZIP could not be installed. Re-upload the complete release package and reboot the app.") from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def activate_runtime(runtime: Path) -> None:
    """Prefer this complete release over any old nkpi folder or cached imports."""
    runtime = runtime.resolve()
    selected = str(runtime)
    with _STATE["lock"]:
        previous = _STATE["path"]
        package_modules = [name for name in list(sys.modules) if name == "nkpi" or name.startswith("nkpi.")]
        stale = previous != selected
        for name in package_modules:
            origin = getattr(sys.modules.get(name), "__file__", None)
            if not origin or not Path(origin).resolve().is_relative_to(runtime):
                stale = True
                break
        if stale:
            for name in package_modules:
                sys.modules.pop(name, None)
        sys.path[:] = [selected] + [path for path in sys.path if path != selected and path != previous]
        _STATE["path"] = selected
        importlib.invalidate_caches()


def run_bundled_app(runtime: Path) -> None:
    # run_path temporarily changes interpreter state; serialize app invocations.
    with _STATE["lock"]:
        activate_runtime(runtime)
        mimetypes.add_type("application/javascript", ".js")
        mimetypes.add_type("text/css", ".css")
        mimetypes.add_type("application/wasm", ".wasm")
        runpy.run_path(str(runtime / APP_FILENAME), run_name="__main__")


def main() -> None:
    try:
        runtime = prepare_runtime()
    except BundleError as exc:
        logging.getLogger("nkpi.bootstrap").exception("NKPI release package could not be loaded")
        import streamlit as st

        st.error(str(exc))
        st.stop()
        return
    # Application exceptions and Streamlit rerun/stop signals remain unchanged.
    run_bundled_app(runtime)


if __name__ == "__main__":
    main()
