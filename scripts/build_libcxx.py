#!/usr/bin/env python3

import argparse
import collections
import errno
import io
import os
import shutil
import stat
import subprocess
import sys
from os.path import dirname, exists, expandvars


def norm(p: str) -> str:
    return os.path.normpath(p)


def join(b: str, p: str) -> str:
    return norm(os.path.join(b, p))


def mkdir(p: str) -> None:
    if not exists(p):
        os.makedirs(p)


def rmdir(p: str) -> None:
    if not exists(p):
        return
    shutil.rmtree(p, onerror=_handle_read_only)


def _handle_read_only(f, p, e):
    if f in (os.rmdir, os.remove, os.unlink) and e[1].errno == errno.EACCES:
        os.chmod(p, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
        f(p)
    else:
        raise e


BUILD_DIR = norm(dirname(__file__))

LLVM_PROJECT_DIR = join(BUILD_DIR, "../llvm-project")
LLVM_INSTALL_DIR = join(BUILD_DIR, "../out/llvm-install")

LIBCXX_BUILD_DIR = join(BUILD_DIR, "../out/libcxx-build")
LIBCXX_INSTALL_DIR = join(BUILD_DIR, "../out/libcxx-install")


def main() -> int:
    if not exists(LLVM_INSTALL_DIR):
        print("Build '%s' first." % "llvm-install")
        return 1

    parser = argparse.ArgumentParser(description="Build libc++ library.")
    parser.add_argument(
        "--install-dir",
        help="override the install",
    )

    args = parser.parse_args()
    libcxx_install_dir = args.install_dir if args.install_dir else LIBCXX_INSTALL_DIR

    cmake_args = [
        "-GNinja",
        "-DCMAKE_BUILD_TYPE=Release",
        f'-DCMAKE_INSTALL_PREFIX="{libcxx_install_dir}"',
    ]

    if sys.platform == "win32":
        cmake_args += [
            f'-DCMAKE_C_COMPILER="{LLVM_INSTALL_DIR}/bin/clang-cl.exe"',
            f'-DCMAKE_CXX_COMPILER="{LLVM_INSTALL_DIR}/bin/clang-cl.exe"',
            f'-DCMAKE_LINKER="{LLVM_INSTALL_DIR}/bin/lld-link.exe"',
            '-DLLVM_ENABLE_RUNTIMES="libcxx"',
        ]
    else:
        cmake_args += [
            f'-DCMAKE_C_COMPILER="{LLVM_INSTALL_DIR}/bin/clang"',
            f'-DCMAKE_CXX_COMPILER="{LLVM_INSTALL_DIR}/bin/clang++"',
            f'-DCMAKE_LINKER="{LLVM_INSTALL_DIR}/bin/lld"',
            "-DLIBCXXABI_USE_LLVM_UNWINDER=Off",
            '-DLLVM_ENABLE_RUNTIMES="libcxx;libcxxabi"',
        ]

    deployment_target = "10.15"
    os.environ["MACOSX_DEPLOYMENT_TARGET"] = deployment_target

    rmdir(LIBCXX_BUILD_DIR)
    rmdir(LIBCXX_INSTALL_DIR)

    mkdir(LIBCXX_BUILD_DIR)
    os.chdir(LIBCXX_BUILD_DIR)

    run_command(
        ["cmake"] + cmake_args + ['"' + join(LLVM_PROJECT_DIR, "runtimes") + '"']
    )
    run_command(["ninja", "install"])

    return 0


def run_command(command: list[str]) -> None:
    if sys.platform == "win32":
        _, vs_dir = detect_visual_studio()
        script_path = join(vs_dir, "VC/Auxiliary/Build/vcvarsall.bat")
        command = [f'"{script_path}"', "amd64", "&&"] + command
    cmd = " ".join(command)
    print("Running:", cmd)
    subprocess.call(cmd, shell=True)


def detect_visual_studio() -> tuple[str, str]:
    """Return best available version of Visual Studio."""
    # VS versions are listed in descending order of priority (highest first).
    # The first version is assumed by this script to be the one that is packaged,
    # which makes a difference for the arm64 runtime.
    MSVS_VERSIONS = collections.OrderedDict(
        [
            ("2022", "17.0"),  # Default and packaged version of Visual Studio.
            ("2019", "16.0"),
            ("2017", "15.0"),
        ]
    )

    supported_versions = list(MSVS_VERSIONS.keys())

    for version in supported_versions:
        # Checking vs%s_install environment variables.
        # For example, vs2019_install could have the value
        # "C:\Program Files (x86)\Microsoft Visual Studio\2019\Community".
        # Only vs2017_install, vs2019_install and vs2022_install are supported.
        path = os.environ.get("vs%s_install" % version)
        if path and exists(path):
            return version, path

        # Detecting VS under possible paths.
        dirs = ["%ProgramFiles%", "%ProgramFiles(x86)%"]
        editions = [
            "Enterprise",
            "Professional",
            "Community",
            "Preview",
            "BuildTools",
        ]
        for dir in dirs:
            for edition in editions:
                path = expandvars(
                    dir + "/Microsoft Visual Studio/%s/%s" % (version, edition)
                )
                if path and exists(path):
                    os.environ["vs%s_install" % version] = path
                    return version, path

    raise Exception(
        "No supported Visual Studio can be found."
        " Supported versions are: %s."
        % ", ".join("{} ({})".format(v, k) for k, v in MSVS_VERSIONS.items())
    )


if __name__ == "__main__":
    # Don't buffer stdout, so that print statements are immediately flushed.
    # LLVM tests print output without newlines, so with buffering they won't be
    # immediately printed.
    major, _, _, _, _ = sys.version_info
    if major == 3:
        # Python3 only allows unbuffered output for binary streams. This
        # workaround comes from https://stackoverflow.com/a/181654/4052492.
        sys.stdout = io.TextIOWrapper(
            open(sys.stdout.fileno(), "wb", 0), write_through=True
        )
    else:
        sys.stdout = os.fdopen(sys.stdout.fileno(), "w", 0)

    sys.exit(main())
