"""Writes THIRD-PARTY-NOTICES.txt for the Windows build of the dictation server.

    python third_party_notices.py <output file>

Run with the build environment's Python (build_exe.ps1 does): it walks what `alefa-stt[local]`
installs, package by package, and copies each one's licence text from its metadata. PyAV's wheel
also carries FFmpeg and the libraries FFmpeg was built with, as DLLs; those are listed by name
with where their source is, since they are not Python packages.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

from packaging.requirements import Requirement

ROOT = "alefa-stt"
EXTRAS = {"local"}
# Left out of the build by build_exe.ps1 (--exclude-module), so not shipped.
EXCLUDED = {"onnxruntime", "hf-xet"}
CONTACT = "alefa-support@googlegroups.com"

FFMPEG_NOTE = """\
PyAV's wheel for Windows (av {av}) ships FFmpeg's libraries (avcodec, avformat, avutil,
swresample, swscale, avfilter, avdevice) and the libraries that FFmpeg build links to, as DLLs in
av.libs. The dictation server uses them only to decode audio. FFmpeg reports its licence as:
"{ffmpeg_licence}". Among the bundled libraries, x264 and x265 are under the GNU General Public
License, version 2 or later; the others under the LGPL (libiconv), BSD-style licences (libvpx,
dav1d, SVT-AV1, libwebp, libopus), the zlib licence (zlib), or the GCC Runtime Library Exception
(the MinGW runtime: libgcc, libstdc++, libwinpthread).

DLLs in this build: {dlls}

Source code:
  - how that FFmpeg build is made, with the source of each library:
    https://github.com/PyAV-Org/pyav-ffmpeg
  - PyAV: https://github.com/PyAV-Org/PyAV (tag v{av})
  - FFmpeg: https://ffmpeg.org/download.html
  - x264: https://code.videolan.org/videolan/x264
  - x265: https://bitbucket.org/multicoreware/x265_git

Written offer: for three years after each release of this program, the complete corresponding
source code of the GPL- and LGPL-licensed components above is sent to anyone who asks at
{contact}, for no more than the cost of sending it.

The GNU GPL v2: https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt
The GNU LGPL v2.1: https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt
The GNU LGPL v3: https://www.gnu.org/licenses/lgpl-3.0.txt
"""

NATIVE_NOTE = """\
Native libraries inside Python packages (their licences are in the package's own texts below,
except where named here):
{native}
  - cudnn64_9.dll (inside CTranslate2's wheel): NVIDIA cuDNN, redistributed under the NVIDIA
    cuDNN Software License Agreement, https://docs.nvidia.com/deeplearning/cudnn/latest/reference/eula.html
  - libiomp5md.dll (inside CTranslate2's wheel): the Intel OpenMP runtime, redistributed under the
    Intel Simplified Software License, https://www.intel.com/content/www/us/en/developer/articles/license/end-user-license-agreement.html
  - onnxruntime.dll (inside sherpa-onnx's wheel): ONNX Runtime, MIT licence,
    https://github.com/microsoft/onnxruntime
"""

LICENCE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE)[^/]*$", re.IGNORECASE)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def walk() -> list[md.Distribution]:
    seen: dict[str, md.Distribution] = {}
    queue: list[tuple[str, set[str]]] = [(ROOT, EXTRAS)]
    while queue:
        name, extras = queue.pop()
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue  # an optional dependency not installed on Windows
        key = norm(dist.metadata["Name"])
        if key in seen and not extras:
            continue
        seen[key] = dist
        for line in dist.requires or []:
            req = Requirement(line)
            envs = [{"extra": e} for e in extras] or [{"extra": ""}]
            if req.marker is None or any(req.marker.evaluate(env) for env in envs):
                queue.append((req.name, set(req.extras)))
    return sorted(
        (d for k, d in seen.items() if k != norm(ROOT) and k not in EXCLUDED),
        key=lambda d: norm(d.metadata["Name"]),
    )


def licence_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    out = []
    for f in dist.files or []:
        path = str(f).replace("\\", "/")
        if ".dist-info/" in path and LICENCE_FILE.search(path):
            try:
                out.append((path.rsplit("/", 1)[-1], Path(f.locate()).read_text("utf-8", "replace")))
            except OSError:
                pass
    return out


def licence_name(dist: md.Distribution) -> str:
    m = dist.metadata
    if m.get("License-Expression"):
        return m["License-Expression"]
    classifiers = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or [] if c.startswith("License")]
    if classifiers:
        return ", ".join(classifiers)
    first = (m.get("License") or "").strip().splitlines()
    return first[0][:80] if first else "see the licence text below"


def ffmpeg_section() -> str:
    av = md.distribution("av")
    libs = Path(av.locate_file("av.libs"))
    dlls = sorted({re.split(r"-[0-9a-f]{32}", p.name)[0] for p in libs.glob("*.dll")})
    licence = "unknown"
    for p in libs.glob("avutil-*.dll"):
        found = re.search(rb"license: ([A-Za-z0-9 .+]+)", p.read_bytes())
        if found:
            licence = "license: " + found.group(1).decode("ascii")
    return FFMPEG_NOTE.format(av=av.version, ffmpeg_licence=licence, dlls=", ".join(dlls), contact=CONTACT)


def native_section() -> str:
    lines = []
    for package, folder in (("ctranslate2", "ctranslate2"), ("sherpa-onnx", "sherpa_onnx/lib")):
        try:
            where = Path(md.distribution(package).locate_file(folder))
        except md.PackageNotFoundError:
            continue
        names = sorted(p.name for p in where.glob("*.dll"))
        if names:
            lines.append(f"  - {package}: {', '.join(names)}")
    return NATIVE_NOTE.format(native="\n".join(lines))


def main() -> None:
    out = Path(sys.argv[1])
    parts = [
        "Third-party software in the Clew dictation server (Windows build)",
        "=" * 66,
        "",
        "The dictation server's own code is under the MIT licence (LICENSE in",
        "https://github.com/yaqalab/clew-tools). It is packed with Python and the packages",
        "below, each under its own licence. No speech model is included; models are downloaded",
        "later, each under its own licence (MIT for the Whisper models, Apache-2.0 for 3D-Speaker).",
        "",
        f"Python {sys.version.split()[0]}: Python Software Foundation License, https://docs.python.org/3/license.html",
        "PyInstaller (packs it into a program): its bootloader is under the GPL-2.0-or-later with an",
        "exception that allows it in programs under any licence, https://pyinstaller.org/en/stable/license.html",
        "",
        "-" * 66,
        "FFmpeg and the libraries it is built with (inside PyAV)",
        "-" * 66,
        ffmpeg_section(),
        native_section(),
    ]
    dists = walk()
    parts += ["-" * 66, "Python packages", "-" * 66]
    for d in dists:
        parts.append(f"  {d.metadata['Name']} {d.version}: {licence_name(d)}")
    parts.append("")
    for d in dists:
        for name, text in licence_texts(d):
            parts += ["=" * 66, f"{d.metadata['Name']} {d.version}: {name}", "=" * 66, text.strip(), ""]
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"Wrote {out} ({len(dists)} packages)")


if __name__ == "__main__":
    main()
