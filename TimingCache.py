"""PlaceDB-owned binary DB of OpenTimer's parsed static timing inputs.

No Python pickle and no serialized C++ pointers. Dynamic RC/slack/weights are
not restored across placements. This mixin also works with MakeDB-backed DBs.
"""
import contextlib
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import uuid

LOG = logging.getLogger(__name__)
SCHEMA = 2


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def library_files(value):
    """A file, directory, or ordered list; directories expand deterministically."""
    if not value:
        return []
    values = [value] if isinstance(value, (str, os.PathLike)) else value
    result = []
    for item in values:
        path = Path(item).expanduser().resolve()
        files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".lib") if path.is_dir() else [path]
        if not files:
            raise ValueError("No .lib files in %s" % path)
        for entry in files:
            if not entry.is_file():
                raise FileNotFoundError(entry)
            if str(entry) not in result:
                result.append(str(entry))
    return result


def timing_sources(params):
    common = getattr(params, "lib_input", "")
    early = getattr(params, "early_lib_input", "")
    late = getattr(params, "late_lib_input", "")
    if common and (early or late):
        raise ValueError("Use lib_input OR early_lib_input + late_lib_input, not both")
    if common:
        libs = [(p, -1) for p in library_files(common)]
    else:
        if not early or not late:
            raise ValueError("Timing requires lib_input or both early_lib_input and late_lib_input")
        libs = [(p, 0) for p in library_files(early)] + [(p, 1) for p in library_files(late)]
    verilog = getattr(params, "verilog_input", "")
    if not verilog:
        raise ValueError("Timing requires verilog_input")
    sdc = getattr(params, "sdc_input", "")
    return libs, str(Path(verilog).resolve()), str(Path(sdc).resolve()) if sdc else ""


def cache_location(params):
    """Use MakeDB's explicit export/import contract; never infer from a miss.

    Legacy timing_cache_mode/timing_cache_dir settings have no authority here.
    Binary modes take precedence even if mode still contains binary_write.
    """
    option = getattr(params, "db_option", "")
    mode = getattr(params, "mode", "")
    modes = mode.split("/") if isinstance(mode, str) else (mode or [])
    if option in ("binary", "binary_wo_pos"):
        action = "read"
    elif option == "def" and "binary_write" in modes:
        action = "write"
    else:
        return None, "text"
    base = getattr(params, "save_path", "")
    if not base:
        raise ValueError("save_path is required for timing DB " + action)
    return Path(base).expanduser().resolve() / "timing_cache", action


def fingerprint(params, backend):
    libs, verilog, sdc = timing_sources(params)
    deps = getattr(params, "timing_cache_dependencies", []) or []
    if isinstance(deps, str):
        deps = [deps]
    paths = [p for p, _ in libs] + [verilog] + ([sdc] if sdc else [])
    paths += [str(Path(p).resolve()) for p in deps]
    files = {p: file_hash(p) for p in dict.fromkeys(paths)}
    binary = getattr(backend, "__file__", None)
    return dict(schema=SCHEMA, native_schema=int(backend.timing_cache_schema),
                backend_sha256=file_hash(binary) if binary else "test-backend",
                libraries=libs, verilog=verilog, sdc=sdc, files=files)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def checked_model(directory, backend):
    with open(directory / "manifest.json") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict):
        raise ValueError("invalid timing cache manifest")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or inputs.get("schema") != SCHEMA:
        raise ValueError("incompatible timing DB schema; export with db_option=def, mode=binary_write")
    if inputs.get("native_schema") != int(backend.timing_cache_schema):
        raise ValueError("incompatible native timing DB format; export again with binary_write")
    binary = getattr(backend, "__file__", None)
    backend_hash = file_hash(binary) if binary else "test-backend"
    if inputs.get("backend_sha256") != backend_hash:
        raise ValueError("timing backend changed; export again with db_option=def, mode=binary_write")
    name = manifest["model"]
    if not isinstance(name, str) or Path(name).name != name or not name.startswith("model-") or not name.endswith(".bin"):
        raise ValueError("invalid timing model filename")
    path = directory / name
    if file_hash(path) != manifest["model_sha256"]:
        raise ValueError("timing model checksum mismatch")
    return path, manifest


class TimingCacheMixin:
    """Include in PlaceDB; explicit binary DB ownership stays with PlaceDB."""

    def initialize_timing_db(self, params, backend):
        directory, action = cache_location(params)
        prepared = self.__dict__.pop("_prepared_timing_db", None)
        if prepared is not None and prepared[:2] == (directory, action):
            return prepared[2]
        if action == "write":
            return self.save_timing_cache(params, backend)
        if action == "read":
            return self.load_timing_cache(params, backend)
        self.timing_cache_info = {"status": "text"}
        return None

    def prepare_timing_db(self, params, backend=None):
        """Export at PlaceDB initialization, including DB-preparation-only jobs.

        Pure physical exports without any timing inputs remain valid. If any
        timing input is specified, require a complete valid timing model rather
        than quietly producing a partial timing DB.
        """
        directory, action = cache_location(params)
        if action != "write":
            return
        has_sources = any(getattr(params, key, "") for key in
                          ("lib_input", "early_lib_input", "late_lib_input", "sdc_input"))
        if not has_sources and not (getattr(params, "timing_opt_flag", 0) or
                                    getattr(params, "timing_clustering_flag", 0)):
            return
        if backend is None:
            from dreamplace.ops.timing import timing_cpp as backend
        timer = self.save_timing_cache(params, backend)
        if getattr(params, "timing_opt_flag", 0) or getattr(params, "timing_clustering_flag", 0):
            # Hand the just-built model to Timer without saving/parsing twice.
            self._prepared_timing_db = (directory, action, timer)

    def _record_timing_db(self, status, path, manifest, started):
        self.timing_cache_info = {"status": status, "model": str(path),
                                  "seconds": time.monotonic() - started,
                                  "inputs": manifest["inputs"]}
        LOG.info("timing DB %s: %s (%.2fs; dynamic RC/STA not restored)", status, path,
                 self.timing_cache_info["seconds"])

    def load_timing_cache(self, params, backend):
        """Read only. Never read/hash source LIB/Verilog/SDC or rebuild on error."""
        directory, action = cache_location(params)
        if action != "read":
            raise ValueError("timing DB restore requires db_option=binary or binary_wo_pos")
        if not hasattr(backend, "load_timing_model"):
            raise RuntimeError("timing_cpp lacks binary model support; rebuild/install timing_cpp")
        started = time.monotonic()
        try:
            path, manifest = checked_model(directory, backend)
            timer = backend.load_timing_model(str(path))
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            raise RuntimeError("Cannot restore timing DB from %s: %s. "
                               "Export first with db_option=def, mode=binary_write, save_path. "
                               "Binary mode will not parse sources or create files." % (directory, error)) from error
        self._record_timing_db("restored", path, manifest, started)
        return timer

    def save_timing_cache(self, params, backend):
        """Explicit export, permitted ONLY for def + binary_write.

        Always parse current sources, even if an older exported model exists.
        """
        directory, action = cache_location(params)
        if action != "write":
            raise ValueError("timing DB export requires db_option=def and mode=binary_write")
        if not hasattr(backend, "compile_timing_model") or not hasattr(backend, "load_timing_model"):
            raise RuntimeError("timing_cpp lacks binary model support; rebuild/install timing_cpp")
        started = time.monotonic()
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / ".lock", "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            expected = fingerprint(params, backend)
            timer, path, manifest = self._write_timing_model(params, backend, directory, expected)
        self._record_timing_db("saved", path, manifest, started)
        return timer

    def _write_timing_model(self, params, backend, directory, expected):
        path = directory / ("model-" + uuid.uuid4().hex + ".bin")
        libs, verilog, sdc = timing_sources(params)
        try:
            backend.compile_timing_model(libs, verilog, sdc, str(path))
            # Do not publish an artifact if source files changed while parsing.
            if canonical(fingerprint(params, backend)) != canonical(expected):
                raise RuntimeError("Timing inputs changed during cache creation; retry")
            timer = backend.load_timing_model(str(path))
            manifest = dict(inputs=expected, model=path.name, model_sha256=file_hash(path),
                            dynamic_state_saved=False)
            with open(path, "rb") as stream:
                os.fsync(stream.fileno())
            atomic_json(directory / "manifest.json", manifest)
        except Exception:
            # Only our unpublished generation is removed; existing DB untouched.
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            raise
        return timer, path, manifest


def main(argv=None):
    """Export/restore timing DB according to the JSON's db_option and mode."""
    import argparse
    import sys
    from types import SimpleNamespace
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="DREAMPlace JSON; paths resolve against the working directory")
    args = parser.parse_args(argv)
    with open(args.config) as stream:
        params = SimpleNamespace(**json.load(stream))
    if cache_location(params)[0] is None:
        parser.error("use def + binary_write to export, or binary/binary_wo_pos to restore; set save_path")
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    import torch  # Load native extension dependencies before import.
    from dreamplace.ops.timing import timing_cpp
    db = TimingCacheMixin()
    timer = db.initialize_timing_db(params, timing_cpp)
    # Graph/library/constraint restoration is sufficient here: avoid performing
    # a placement-independent STA just to prepare a static input cache.
    print(json.dumps(dict(status=db.timing_cache_info["status"],
                          model=db.timing_cache_info["model"],
                          seconds=db.timing_cache_info["seconds"],
                          gates=timer.num_gates(), pins=timer.num_pins()), indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    raise SystemExit(main())
