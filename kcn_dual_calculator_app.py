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


# Browser-only replay repair. Keep the existing v9.2.1 runtime ZIP unchanged.
# Replace the OLD final two-line "if __name__" block with this entire block.
# The compressed data contains source-text edits, not an executable installer.
import base64 as _patch_b64
import zlib as _patch_zlib

_REPLAY_BASE = "eb5e97c52a9b4bfff81cbc2040430f36479efe2015a7559661c5598dbe7bd4d4"
_REPLAY_DATA_SHA = "631e1a888556abaefa68133d2e645046471910e5a36cce116aa6b7d1a48dd5aa"
_REPLAY_DATA = """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"""
_REPLAY_FILES = (
    "components/replay/replay_math.js", "components/replay/fluid_renderer.js",
    "components/replay/replay_view.js", "components/replay/replay.css",
    "nkpi/service.py", "nkpi/component.py", "kcn_dual_calculator_app.py",
    "tools/build_delivery.py",
)
_original_prepare_runtime = prepare_runtime


def prepare_runtime(bundle_path=BUNDLE_PATH, expected_sha256=RUNTIME_SHA256, cache_base=None):
    """Verify the old ZIP, then apply the replay-only repair to a separate cache."""
    if expected_sha256 != _REPLAY_BASE:
        raise BundleError("This copy/paste repair requires the original v9.2.1 ZIP. Restore the previous launcher edit; do not mix releases.")
    original = _original_prepare_runtime(bundle_path, expected_sha256, cache_base)
    staging = None
    try:
        packed = _patch_b64.b64decode("".join(_REPLAY_DATA.split()), validate=True)
        if not hmac.compare_digest(hashlib.sha256(packed).hexdigest(), _REPLAY_DATA_SHA):
            raise BundleError("The pasted repair is incomplete. Undo this edit or copy the entire repair block again.")
        changes = json.loads(_patch_zlib.decompress(packed))
        if set(changes) != set(_REPLAY_FILES):
            raise BundleError("Unexpected files in the replay repair. Restore the original launcher.")
        patch_id = hashlib.sha256((_REPLAY_BASE + _REPLAY_DATA_SHA).encode()).hexdigest()
        target = original.parent / patch_id

        def valid(folder):
            if not _installed(folder, patch_id):
                return False
            return all(hashlib.sha256((folder / name).read_bytes()).hexdigest() == item["after"]
                       for name, item in changes.items())

        with _STATE["lock"]:
            if valid(target):
                return target
            if target.exists() or target.is_symlink():
                raise BundleError("The replay repair cache is incomplete. Reboot the Streamlit app to rebuild it.")
            staging = Path(tempfile.mkdtemp(prefix=".nkpi-replay-repair-", dir=original.parent))
            shutil.copytree(original, staging, dirs_exist_ok=True)
            for name, item in changes.items():
                path = staging / name
                before = path.read_bytes()
                if hashlib.sha256(before).hexdigest() != item["before"]:
                    raise BundleError("Unexpected original source: " + name + ". No repair was applied.")
                lines = before.decode("utf-8").splitlines(keepends=True)
                parts, cursor = [], 0
                for start, end, replacement in item["edits"]:
                    if not (0 <= cursor <= start <= end <= len(lines)):
                        raise BundleError("The replay source edits are invalid.")
                    parts.extend(lines[cursor:start])
                    parts.append(replacement)
                    cursor = end
                parts.extend(lines[cursor:])
                after = "".join(parts).encode("utf-8")
                if hashlib.sha256(after).hexdigest() != item["after"]:
                    raise BundleError("Repaired source failed its check: " + name)
                path.write_bytes(after)
            manifest_path = staging / "FILE_MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["release"] = "9.2.2-replay-repair-copy-paste"
            for item in manifest["files"]:
                name = item["path"]
                if name in changes:
                    item["sha256"] = changes[name]["after"]
                if hashlib.sha256((staging / name).read_bytes()).hexdigest() != item["sha256"]:
                    raise BundleError("File verification failed: " + name)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            builder = runpy.run_path(str(staging / "tools/build_delivery.py"), run_name="_nkpi_replay_delivery")
            built = builder["build"](staging)
            old_delivery = json.loads((original / ".nkpi-delivery.json").read_text(encoding="utf-8"))
            if built["extractor_sha256"] != old_delivery["extractor_sha256"]:
                raise BundleError("The extractor changed unexpectedly. Replay repair was stopped.")
            (staging / _MARKER).write_text(patch_id, encoding="ascii")
            if not valid(staging):
                raise BundleError("The replay repair did not pass its final installation check.")
            try:
                staging.rename(target)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not valid(target):
                    raise
            return target
    except BundleError:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, _patch_zlib.error) as exc:
        raise BundleError("Copy/paste repair could not be installed. Undo the launcher edit to restore the old app.") from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    main()


