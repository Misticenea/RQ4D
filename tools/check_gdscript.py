"""Parse-checks the Godot project with a headless Godot binary.

The GDScript in `quest-app/` cannot be run without a headset, but it can be
*parsed*, and parse errors are what actually blocked the first device session.
Catching them here costs seconds; catching them on the headset costs a build,
a deploy, and a round trip through someone wearing it.

Godot is fetched on demand and cached. Without a headset attached the OpenXR
runtime fails to load and prints a wall of errors on every invocation — all of
which are noise here, and all of which have to be filtered out without also
swallowing real script errors. `--self-test` proves the filter still catches a
genuine error, because a checker that reports success unconditionally is worse
than no checker at all.

    python tools/check_gdscript.py              # parse-check every script
    python tools/check_gdscript.py --run        # also boot the scene headless
    python tools/check_gdscript.py --self-test  # prove the checker still works
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "quest-app"
GODOT_VERSION = "4.5-stable"
GODOT_URL = (
    f"https://github.com/godotengine/godot/releases/download/{GODOT_VERSION}"
    f"/Godot_v{GODOT_VERSION}_linux.x86_64.zip"
)
CACHE = Path.home() / ".cache" / "rq4d"
BINARY = CACHE / f"Godot_v{GODOT_VERSION}_linux.x86_64"

# Everything OpenXR prints when there is no runtime — expected off-device, and
# nothing to do with whether the scripts are valid.
NOISE = re.compile(
    r"OpenXR|xrEnumerateInstanceExtensionProperties|RuntimeManifestFile|"
    r"RuntimeInterface|Failed querying extension|build-tools|"
    r"Godot Engine v|godotengine\.org|^\s*$",
    re.IGNORECASE,
)
# What a real problem looks like. Deliberately narrow: broadening this to any
# line containing "error" is how the OpenXR noise gets back in.
REAL = re.compile(r"(Parse Error|Compile Error|SCRIPT ERROR|Invalid|Failed to load script)", re.I)


def ensure_godot() -> Path:
    if BINARY.exists():
        return BINARY
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"fetching Godot {GODOT_VERSION}…", file=sys.stderr)
    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        with urllib.request.urlopen(GODOT_URL) as response:
            tmp.write(response.read())
        tmp.flush()
        with zipfile.ZipFile(tmp.name) as archive:
            archive.extractall(CACHE)
    BINARY.chmod(0o755)
    return BINARY


def run(godot: Path, args: list[str], timeout: int = 120) -> str:
    result = subprocess.run(
        [str(godot), "--headless", "--path", str(PROJECT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout + result.stderr


def check_script(godot: Path, script: Path) -> list[str]:
    rel = script.relative_to(PROJECT).as_posix()
    output = run(godot, ["--check-only", "--script", f"res://{rel}"])
    return [
        line.strip()
        for line in output.splitlines()
        if REAL.search(line) and not NOISE.search(line)
    ]


def check_all(godot: Path) -> int:
    run(godot, ["--import"], timeout=300)  # register class_names first

    failures = 0
    for script in sorted(PROJECT.glob("scripts/*.gd")):
        problems = check_script(godot, script)
        if problems:
            failures += 1
            print(f"FAIL  {script.relative_to(ROOT)}")
            for line in problems:
                print(f"      {line}")
        else:
            print(f"ok    {script.relative_to(ROOT)}")
    return failures


def boot_scene(godot: Path, frames: int = 180) -> list[str]:
    """Run the main scene headless for a few frames.

    Parsing proves the syntax; booting proves the scene tree. Node paths,
    @onready types and signal connections are all runtime failures that a
    parse check cannot see, and they are just as capable of wasting a device
    session. XR itself will fail to start here, which exercises the
    no-headset path rather than avoiding it.
    """
    output = run(godot, ["--quit-after", str(frames)], timeout=180)
    return [
        line.strip()
        for line in output.splitlines()
        if REAL.search(line) and not NOISE.search(line)
    ]


def self_test(godot: Path) -> int:
    """Inject a known-bad script and confirm the filter still reports it."""
    broken = PROJECT / "scripts" / "_selftest_broken.gd"
    broken.write_text(
        "extends Node\n\n"
        "func _ready() -> void:\n"
        "\tvar p := Projection()\n"
        "\tvar col := p[0]  # inferring from Variant — must be rejected\n"
        "\tprint(col)\n"
    )
    try:
        problems = check_script(godot, broken)
    finally:
        broken.unlink(missing_ok=True)

    if problems:
        print("self-test PASSED — the checker catches a real parse error:")
        for line in problems:
            print(f"      {line}")
        return 0
    print(
        "self-test FAILED — a script with a known parse error was reported clean.\n"
        "The noise filter is probably swallowing real errors; do not trust a pass.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true", help="verify the checker itself")
    p.add_argument("--run", action="store_true", help="also boot the main scene headless")
    args = p.parse_args()

    godot = ensure_godot()
    if args.self_test:
        return self_test(godot)

    failures = check_all(godot)

    if args.run and not failures:
        problems = boot_scene(godot)
        if problems:
            failures += 1
            print("FAIL  booting scenes/main.tscn")
            for line in problems:
                print(f"      {line}")
        else:
            print("ok    scenes/main.tscn boots")

    print()
    if failures:
        print(f"{failures} problem(s) found")
        return 1
    print("all scripts parse" + (" and the scene boots" if args.run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
