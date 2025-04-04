#!/usr/bin/env python3
# Copyright 2024 Ken Hui
# Copyright 2019 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""This script is used to build clang binaries.

DEAR MAC USER: YOU NEED XCODE INSTALLED TO BUILD LLVM/CLANG WITH THIS SCRIPT.
The Xcode command line tools that are installed as part of the Chromium
development setup process are not sufficient. CMake will fail to configure, as
the non-system Clang we use will not find any standard library headers. To use
this build script on Mac:
1. Download Xcode. (Visit http://go/xcode for googlers.)
2. Install to /Applications
3. sudo xcode-select --switch /Applications/Xcode.app
"""

import argparse
import collections
import errno
import io
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
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

LLVM_PROJECT_DIR = join(BUILD_DIR, "llvm-project")

LLVM_BOOTSTRAP_BUILD_DIR = join(BUILD_DIR, "out/llvm-bootstrap-build")
LLVM_BOOTSTRAP_INSTALL_DIR = join(BUILD_DIR, "out/llvm-bootstrap-install")

LLVM_BUILD_DIR = join(BUILD_DIR, "out/llvm-build")
LLVM_INSTALL_DIR = join(BUILD_DIR, "out/llvm-install")

TOOLS_DIR = join(BUILD_DIR, "tools")
TOOLS_BUILD_DIR = join(BUILD_DIR, "out/tools-build")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build My Clang.")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="first build clang with CC, then with itself.",
    )
    parser.add_argument("--build-dir", help="Override build directory")
    parser.add_argument(
        "--install-dir",
        help="override the install directory for the final "
        "compiler. If not specified, no install happens for "
        "the compiler.",
    )
    parser.add_argument("--thinlto", action="store_true", help="build with ThinLTO")
    parser.add_argument(
        "--disable-asserts", action="store_true", help="build with asserts disabled"
    )
    parser.add_argument(
        "--pic", action="store_true", help="Uses PIC when building LLVM"
    )
    parser.add_argument(
        "--without-zstd",
        dest="with_zstd",
        action="store_false",
        help="Disable zstd in the build",
    )
    parser.add_argument(
        "--run-tests", action="store_true", help="run tests after building"
    )

    args = parser.parse_args()

    cflags = []
    cxxflags = []
    ldflags = []

    targets = "AArch64;ARM;LoongArch;Mips;PowerPC;RISCV;SystemZ;WebAssembly;X86"
    projects = "clang;lld;clang-tools-extra"

    pic_default = sys.platform == "win32"
    pic_mode = "ON" if args.pic or pic_default else "OFF"

    base_cmake_args = [
        "-GNinja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_ENABLE_ASSERTIONS=%s" % ("OFF" if args.disable_asserts else "ON"),
        '-DLLVM_ENABLE_PROJECTS="%s"' % projects,
        "-DLLVM_ENABLE_RUNTIMES=compiler-rt",
        '-DLLVM_TARGETS_TO_BUILD="%s"' % targets,
        f"-DLLVM_ENABLE_PIC={pic_mode}",
        "-DLLVM_ENABLE_Z3_SOLVER=OFF",
        "-DCLANG_PLUGIN_SUPPORT=OFF",
        "-DCLANG_ENABLE_STATIC_ANALYZER=OFF",
        "-DCLANG_ENABLE_ARCMT=OFF",
        "-DLLVM_ENABLE_UNWIND_TABLES=OFF",
        # See crbug.com/1126219: Use native symbolizer instead of DIA
        "-DLLVM_ENABLE_DIA_SDK=OFF",
        # The default value differs per platform, force it off everywhere.
        "-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF",
        # Don't use curl.
        "-DLLVM_ENABLE_CURL=OFF",
        # Build libclang.a as well as libclang.so
        "-DLIBCLANG_BUILD_STATIC=ON",
        # The Rust build (on Mac ARM at least if not others) depends on the
        # FileCheck tool which is built but not installed by default, this
        # puts it in the path for the Rust build to find and matches the
        # `bootstrap` tool:
        # https://github.com/rust-lang/rust/blob/021861aea8de20c76c7411eb8ada7e8235e3d9b5/src/bootstrap/src/core/build_steps/llvm.rs#L348
        "-DLLVM_INSTALL_UTILS=ON",
    ]

    if sys.platform == "win32":
        base_cmake_args.append("-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded")

        # Require zlib compression.
        zlib_dir = build_zlib(args.bootstrap)
        os.environ["PATH"] = zlib_dir + os.pathsep + os.environ.get("PATH", "")

        cflags += ["-I" + zlib_dir]
        cxxflags += ["-I" + zlib_dir]
        ldflags += ["-LIBPATH:" + zlib_dir]
        base_cmake_args.append("-DLLVM_ENABLE_ZLIB=FORCE_ON")

    # Statically link libxml2 to make lld-link not require mt.exe on Windows,
    # and to make sure lld-link output on other platforms is identical to
    # lld-link on Windows (for cross-builds).
    libxml_cmake_args, libxml_cflags = build_libxml2(args.bootstrap)
    base_cmake_args += libxml_cmake_args
    cflags += libxml_cflags
    cxxflags += libxml_cflags

    if args.with_zstd:
        # Statically link zstd to make lld support zstd compression for debug info.
        zstd_cmake_args, zstd_cflags = build_zstd(args.bootstrap)
        base_cmake_args += zstd_cmake_args
        cflags += zstd_cflags
        cxxflags += zstd_cflags

    # Preserve test environment
    lit_excludes = []
    if sys.platform.startswith("linux"):
        lit_excludes += [
            # fstat and sunrpc tests fail due to sysroot/host mismatches
            # (crbug.com/1459187).
            "^MemorySanitizer-.* f?stat(at)?(64)?.cpp$",
            "^.*Sanitizer-.*sunrpc.*cpp$",
            # sysroot/host glibc version mismatch, crbug.com/1506551
            "^.*Sanitizer.*mallinfo2.cpp$",
            # Allocator tests fail after kernel upgrade on the builders. Suppress
            # until the test fix has landed (crbug.com/342324064).
            "^SanitizerCommon-Unit :: ./Sanitizer-x86_64-Test/.*$",
            # This also seems to fail due to crbug.com/342324064.
            "^DataFlowSanitizer-x86_64.*release_shadow_space.c$",
        ]
    elif sys.platform == "darwin":
        lit_excludes += [
            # Fails on macOS 14, crbug.com/332589870
            "^.*Sanitizer.*Darwin/malloc_zone.cpp$",
            # Fails with a recent ld, crbug.com/332589870
            "^.*ContinuousSyncMode/darwin-proof-of-concept.c$",
            "^.*instrprof-darwin-exports.c$",
            # Fails on our mac builds, crbug.com/346289767
            "^.*Interpreter/pretty-print.c$",
        ]

    test_env = None
    if lit_excludes:
        test_env = os.environ.copy()
        test_env["LIT_FILTER_OUT"] = "|".join(lit_excludes)

    if args.bootstrap:
        print("Building bootstrap compiler.")

        bootstrap_targets = "X86"
        if sys.platform == "darwin":
            # Need ARM and AArch64 for building the ios clang_rt.
            bootstrap_targets += ";ARM;AArch64"

        bootstrap_args = base_cmake_args + [
            '-DLLVM_TARGETS_TO_BUILD="%s"' % bootstrap_targets,
            '-DLLVM_ENABLE_PROJECTS="clang;lld"',
            '-DCMAKE_INSTALL_PREFIX="%s"' % LLVM_BOOTSTRAP_INSTALL_DIR,
            '-DCMAKE_C_FLAGS="%s"' % " ".join(cflags),
            '-DCMAKE_CXX_FLAGS="%s"' % " ".join(cxxflags),
            '-DCMAKE_EXE_LINKER_FLAGS="%s"' % " ".join(ldflags),
            '-DCMAKE_SHARED_LINKER_FLAGS="%s"' % " ".join(ldflags),
            '-DCMAKE_MODULE_LINKER_FLAGS="%s"' % " ".join(ldflags),
            # Ignore args.disable_asserts for the bootstrap compiler.
            "-DLLVM_ENABLE_ASSERTIONS=ON",
        ]

        bootstrap_args += ["-D" + f for f in compiler_rt_cmake_flags(profile=True)]

        if sys.platform == "darwin":
            bootstrap_args += [
                "-DCOMPILER_RT_ENABLE_IOS=OFF",
                "-DCOMPILER_RT_ENABLE_WATCHOS=OFF",
                "-DCOMPILER_RT_ENABLE_TVOS=OFF",
                "-DCOMPILER_RT_ENABLE_XROS=OFF",
            ]
            if platform.machine() == "arm64":
                bootstrap_args.append("-DDARWIN_osx_ARCHS=arm64")
            else:
                bootstrap_args.append("-DDARWIN_osx_ARCHS=x86_64")

        rmdir(LLVM_BOOTSTRAP_BUILD_DIR)
        mkdir(LLVM_BOOTSTRAP_BUILD_DIR)
        os.chdir(LLVM_BOOTSTRAP_BUILD_DIR)

        run_command(
            ["cmake"] + bootstrap_args + ['"' + join(LLVM_PROJECT_DIR, "llvm") + '"']
        )
        run_command(["ninja"])
        if args.run_tests:
            run_command(["ninja", "check-all"], env=test_env)

        rmdir(LLVM_BOOTSTRAP_INSTALL_DIR)
        run_command(["ninja", "install"])

        print("Bootstrap compiler installed.")
        return 0

    deployment_target = "10.15"
    deployment_env = os.environ.copy()
    deployment_env["MACOSX_DEPLOYMENT_TARGET"] = deployment_target

    # Build PDBs for archival on Windows.  Don't use RelWithDebInfo since it
    # has different optimization defaults than Release.
    # Also disable stack cookies for performance.
    #
    # /Zi generates complete debugging information.
    # /GS- suppressing buffer overrun detection
    # /DEBUG creates a debugging information (PDB) file for the executable.
    # /OPT:REF eliminates functions and data that are never referenced
    # /OPT:ICF perform identical COMDAT folding
    if sys.platform == "win32":
        cflags += ["/Zi", "/GS-"]
        cxxflags += ["/Zi", "/GS-"]
        ldflags += ["/DEBUG", "/OPT:REF", "/OPT:ICF"]

    print("Building final compiler.")

    if not exists(LLVM_BOOTSTRAP_INSTALL_DIR):
        print("Bootstrap compiler not exists.")
        return 1

    cc, cxx, lld = None, None, None

    if sys.platform == "win32":
        cc = join(LLVM_BOOTSTRAP_INSTALL_DIR, "bin/clang-cl.exe")
        cxx = join(LLVM_BOOTSTRAP_INSTALL_DIR, "bin/clang-cl.exe")
        lld = join(LLVM_BOOTSTRAP_INSTALL_DIR, "bin/lld-link.exe")
    else:
        cc = join(LLVM_BOOTSTRAP_INSTALL_DIR, "bin/clang")
        cxx = join(LLVM_BOOTSTRAP_INSTALL_DIR, "bin/clang++")

    if lld is not None:
        base_cmake_args.append('-DCMAKE_LINKER="%s"' % lld)

    final_install_dir = args.install_dir if args.install_dir else LLVM_INSTALL_DIR

    cmake_args = base_cmake_args + [
        '-DCMAKE_C_COMPILER="%s"' % cc,
        '-DCMAKE_CXX_COMPILER="%s"' % cxx,
        '-DCMAKE_C_FLAGS="%s"' % " ".join(cflags),
        '-DCMAKE_CXX_FLAGS="%s"' % " ".join(cxxflags),
        '-DCMAKE_EXE_LINKER_FLAGS="%s"' % " ".join(ldflags),
        '-DCMAKE_SHARED_LINKER_FLAGS="%s"' % " ".join(ldflags),
        '-DCMAKE_MODULE_LINKER_FLAGS="%s"' % " ".join(ldflags),
        '-DCMAKE_INSTALL_PREFIX="%s"' % final_install_dir,
        # Link all binaries with lld. Effectively passes -fuse-ld=lld to the
        # compiler driver. On Windows, cmake calls the linker directly, so there
        # the same is achieved by passing -DCMAKE_LINKER=$lld above.
        "-DLLVM_ENABLE_LLD=ON",
    ]

    if args.thinlto:
        cmake_args.append("-DLLVM_ENABLE_LTO=Thin")

    # The default LLVM_DEFAULT_TARGET_TRIPLE depends on the host machine.
    # Set it explicitly to make the build of clang more hermetic.
    if sys.platform == "darwin":
        if platform.machine() == "arm64":
            cmake_args.append("-DLLVM_DEFAULT_TARGET_TRIPLE=arm64-apple-darwin")
        else:
            cmake_args.append("-DLLVM_DEFAULT_TARGET_TRIPLE=x86_64-apple-darwin")
    elif sys.platform.startswith("linux"):
        if platform.machine() == "aarch64":
            cmake_args.append(
                '-DLLVM_DEFAULT_TARGET_TRIPLE="aarch64-unknown-linux-gnu"'
            )
        elif platform.machine() == "riscv64":
            cmake_args.append(
                '-DLLVM_DEFAULT_TARGET_TRIPLE="riscv64-unknown-linux-gnu"'
            )
        elif platform.machine() == "loongarch64":
            cmake_args.append(
                '-DLLVM_DEFAULT_TARGET_TRIPLE="loongarch64-unknown-linux-gnu"'
            )
        else:
            cmake_args.append('-DLLVM_DEFAULT_TARGET_TRIPLE="x86_64-unknown-linux-gnu"')
    elif sys.platform == "win32":
        cmake_args.append('-DLLVM_DEFAULT_TARGET_TRIPLE="x86_64-pc-windows-msvc"')

    if sys.platform.startswith("linux"):
        debian_sysroot_i386 = unpack_debian_sysroot("i386")
        debian_sysroot_amd64 = unpack_debian_sysroot("amd64")
        debian_sysroot_arm = unpack_debian_sysroot("arm")
        debian_sysroot_arm64 = unpack_debian_sysroot("arm64")
        cmake_args += [
            "-DLLVM_STATIC_LINK_CXX_STDLIB=ON",
            "-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=ON",
        ]

    # Map from triple to {
    #   "args": list of CMake vars without '-D' common to builtins and runtimes
    #   "profile": bool # build profile runtime
    #   "sanitizers": bool # build sanitizer runtimes
    # }
    runtimes_triples_args = {}
    if sys.platform.startswith("linux"):
        runtimes_triples_args["i386-unknown-linux-gnu"] = {
            "args": [
                "CMAKE_SYSROOT=%s" % debian_sysroot_i386,  # type: ignore
                # TODO(crbug.com/40242553): pass proper flags to i386 tests so they compile correctly
                "LLVM_INCLUDE_TESTS=OFF",
            ],
            "profile": True,
            "sanitizers": True,
        }
        runtimes_triples_args["x86_64-unknown-linux-gnu"] = {
            "args": [
                "CMAKE_SYSROOT=%s" % debian_sysroot_amd64,  # type: ignore
            ],
            "profile": True,
            "sanitizers": True,
        }
        # Using "armv7a-unknown-linux-gnueabhihf" confuses the compiler-rt
        # builtins build, since compiler-rt/cmake/builtin-config-ix.cmake
        # doesn't include "armv7a" in its `ARM32` list.
        # TODO(thakis): It seems to work for everything else though, see try
        # results on
        # https://chromium-review.googlesource.com/c/chromium/src/+/3702739/4
        # Maybe it should work for builtins too?
        runtimes_triples_args["armv7-unknown-linux-gnueabihf"] = {
            "args": [
                "CMAKE_SYSROOT=%s" % debian_sysroot_arm,  # type: ignore
                # Can't run tests on x86 host.
                "LLVM_INCLUDE_TESTS=OFF",
            ],
            "profile": True,
            "sanitizers": True,
        }
        runtimes_triples_args["aarch64-unknown-linux-gnu"] = {
            "args": [
                "CMAKE_SYSROOT=%s" % debian_sysroot_arm64,  # type: ignore
                # Can't run tests on x86 host.
                "LLVM_INCLUDE_TESTS=OFF",
            ],
            "profile": True,
            "sanitizers": True,
        }
    elif sys.platform == "win32":
        runtimes_triples_args["i386-pc-windows-msvc"] = {
            "args": [
                "LLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF",
            ],
            "profile": True,
            "sanitizers": False,
        }
        runtimes_triples_args["x86_64-pc-windows-msvc"] = {
            "args": [
                "LLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF",
            ],
            "profile": True,
            "sanitizers": True,
        }
        runtimes_triples_args["aarch64-pc-windows-msvc"] = {
            "args": [  # Can't run tests on x86 host.
                "LLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF",
                "LLVM_INCLUDE_TESTS=OFF",
            ],
            "profile": True,
            "sanitizers": False,
        }
    elif sys.platform == "darwin":
        # compiler-rt is built for all platforms/arches with a single
        # configuration, we should only specify one target triple. 'default' is
        # specially handled.
        runtimes_triples_args["default"] = {
            "args": [
                "SANITIZER_MIN_OSX_VERSION=" + deployment_target,
                "COMPILER_RT_ENABLE_MACCATALYST=ON",
                "COMPILER_RT_ENABLE_IOS=ON",
                "COMPILER_RT_ENABLE_WATCHOS=OFF",
                "COMPILER_RT_ENABLE_TVOS=OFF",
                "COMPILER_RT_ENABLE_XROS=OFF",
                "DARWIN_ios_ARCHS=arm64",
                'DARWIN_iossim_ARCHS="arm64;x86_64"',
                'DARWIN_osx_ARCHS="arm64;x86_64"',
            ],
            "profile": True,
            "sanitizers": True,
        }

    # Convert FOO=BAR CMake flags per triple into
    # -DBUILTINS_$triple_FOO=BAR/-DRUNTIMES_$triple_FOO=BAR and build up
    # -DLLVM_BUILTIN_TARGETS/-DLLVM_RUNTIME_TARGETS.
    all_triples = ""
    for triple in sorted(runtimes_triples_args.keys()):
        all_triples += (";" if all_triples else "") + triple
        for arg in runtimes_triples_args[triple]["args"]:
            assert not arg.startswith("-")
            # 'default' is specially handled to pass through relevant CMake flags.
            if triple == "default":
                cmake_args.append("-D" + arg)
            else:
                cmake_args.append("-DRUNTIMES_" + triple + "_" + arg)
                cmake_args.append("-DBUILTINS_" + triple + "_" + arg)
        for arg in compiler_rt_cmake_flags(
            profile=runtimes_triples_args[triple]["profile"],  # type: ignore
            sanitizers=runtimes_triples_args[triple]["sanitizers"],  # type: ignore
        ):
            # 'default' is specially handled to pass through relevant CMake flags.
            if triple == "default":
                cmake_args.append("-D" + arg)
            else:
                cmake_args.append("-DRUNTIMES_" + triple + "_" + arg)

    cmake_args.append('-DLLVM_BUILTIN_TARGETS="%s"' % all_triples)
    cmake_args.append('-DLLVM_RUNTIME_TARGETS="%s"' % all_triples)

    base_install_targets = [
        "clang",
        "clang-resource-headers",
        "lld",
        "builtins",
        "runtimes",
    ]

    if sys.platform == "win32":
        install_targets = base_install_targets + [
            "llvm-ml",
            "llvm-pdbutil",
            "llvm-readobj",
            "llvm-symbolizer",
            "llvm-undname",
        ]
    else:
        install_targets = base_install_targets + [
            "llvm-ar",
            "llvm-ml",
            "llvm-objcopy",
            "llvm-pdbutil",
            "llvm-readobj",
            "llvm-symbolizer",
            "llvm-undname",
        ]

    rmdir(LLVM_BUILD_DIR)
    mkdir(LLVM_BUILD_DIR)
    os.chdir(LLVM_BUILD_DIR)

    run_command(
        ["cmake"] + cmake_args + ['"' + join(LLVM_PROJECT_DIR, "llvm") + '"'],
        env=deployment_env,
    )
    run_command(["ninja"])
    if args.run_tests:
        run_command(["ninja", "check-all"], env=test_env)

    rmdir(LLVM_INSTALL_DIR)
    run_command(["ninja"] + ["install-" + t for t in install_targets])

    print("Clang build was successful.")
    return 0


def compiler_rt_cmake_flags(profile=False, sanitizers=False) -> list[str]:
    # Don't set -DCOMPILER_RT_BUILD_BUILTINS=ON/OFF as it interferes with the
    # runtimes logic of building builtins.
    args = [
        # Build crtbegin/crtend. It's just two tiny TUs, so just enable this
        # everywhere, even though we only need it on Linux.
        "COMPILER_RT_BUILD_CRT=ON",
        "COMPILER_RT_BUILD_LIBFUZZER=OFF",
        # Turn off ctx_profile because it depends on the sanitizer libraries,
        # which we don't always build.
        "COMPILER_RT_BUILD_CTX_PROFILE=OFF",
        "COMPILER_RT_BUILD_MEMPROF=OFF",
        "COMPILER_RT_BUILD_ORC=OFF",
        "COMPILER_RT_BUILD_SANITIZERS=" + ("ON" if sanitizers else "OFF"),
        "COMPILER_RT_BUILD_PROFILE=" + ("ON" if profile else "OFF"),
        "COMPILER_RT_BUILD_XRAY=OFF",
        # See crbug.com/1205046: don't build scudo (and others we don't need).
        'COMPILER_RT_SANITIZERS_TO_BUILD="asan;dfsan;msan;hwasan;tsan;cfi"',
        # We explicitly list all targets we want to build, do not autodetect
        # targets.
        "COMPILER_RT_DEFAULT_TARGET_ONLY=ON",
    ]
    return args


def run_command(command: list[str], env=None) -> None:
    if sys.platform == "win32":
        _, vs_dir = detect_visual_studio()
        script_path = join(vs_dir, "VC/Auxiliary/Build/vcvarsall.bat")
        command = [f'"{script_path}"', "amd64", "&&"] + command
    cmd = " ".join(command)
    print("Running:", cmd)
    subprocess.call(cmd, env=env, shell=True)


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


def build_zlib(bootstrap = False) -> str:
    """Download and build zlib, and add to PATH."""
    ZLIB_VERSION = "zlib-1.2.11"

    zlib_dir = join(TOOLS_BUILD_DIR, ZLIB_VERSION)

    if not bootstrap and exists(join(zlib_dir, "zlib.lib")):
        return zlib_dir

    print("Building zlib.")
    rmdir(zlib_dir)

    pack_file = join(TOOLS_DIR, ZLIB_VERSION + ".tar.gz")
    unpack(pack_file, TOOLS_BUILD_DIR)
    os.chdir(zlib_dir)
    zlib_files = [
        "adler32",
        "compress",
        "crc32",
        "deflate",
        "gzclose",
        "gzlib",
        "gzread",
        "gzwrite",
        "inflate",
        "infback",
        "inftrees",
        "inffast",
        "trees",
        "uncompr",
        "zutil",
    ]
    cl_flags = [
        "/nologo",
        "/O2",
        "/DZLIB_DLL",
        "/c",
        "/D_CRT_SECURE_NO_DEPRECATE",
        "/D_CRT_NONSTDC_NO_DEPRECATE",
    ]
    run_command(["cl.exe"] + [f + ".c" for f in zlib_files] + cl_flags)
    run_command(
        ["lib.exe"] + [f + ".obj" for f in zlib_files] + ["/nologo", "/out:zlib.lib"],
    )
    # Remove the test directory so it isn't found when trying to find
    # test.exe.
    rmdir("test")
    return zlib_dir


def build_libxml2(bootstrap = False) -> tuple[list[str], list[str]]:
    """Download and build libxml2"""
    LIBXML2_VERSION = "libxml2-v2.9.12"

    src_dir = join(TOOLS_BUILD_DIR, LIBXML2_VERSION)

    build_dir = join(src_dir, "build")
    install_dir = join(build_dir, "install")
    include_dir = join(install_dir, "include/libxml2")
    lib_dir = join(install_dir, "lib")

    if sys.platform == "win32":
        libxml2_lib = join(lib_dir, "libxml2s.lib")
    else:
        libxml2_lib = join(lib_dir, "libxml2.a")

    extra_cmake_flags = [
        "-DLLVM_ENABLE_LIBXML2=FORCE_ON",
        '-DLIBXML2_INCLUDE_DIR="%s"' % include_dir,
        '-DLIBXML2_LIBRARIES="%s"' % libxml2_lib,
        '-DLIBXML2_LIBRARY="%s"' % libxml2_lib,
        # This hermetic libxml2 has enough features enabled for lld-link, but not
        # for the libxml2 usage in libclang. We don't need libxml2 support in
        # libclang, so just turn that off.
        "-DCLANG_ENABLE_LIBXML2=NO",
    ]
    extra_cflags = ["-DLIBXML_STATIC"]

    if not bootstrap and exists(libxml2_lib):
        return extra_cmake_flags, extra_cflags

    print("Building libxml2.")
    rmdir(src_dir)

    pack_file = join(TOOLS_DIR, LIBXML2_VERSION + ".tar.gz")
    unpack(pack_file, TOOLS_BUILD_DIR)

    os.mkdir(build_dir)
    os.chdir(build_dir)

    # Disable everything except WITH_TREE and WITH_OUTPUT, both needed by LLVM's
    # WindowsManifestMerger.
    # Also enable WITH_THREADS, else libxml doesn't compile on Linux.
    run_command(
        [
            "cmake",
            "-GNinja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_INSTALL_PREFIX=install",
            # The mac_arm bot builds a clang arm binary, but currently on an intel
            # host. If we ever move it to run on an arm mac, this can go. We
            # could pass this only if args.build_mac_arm, but libxml is small, so
            # might as well build it universal always for a few years.
            '-DCMAKE_OSX_ARCHITECTURES="arm64;x86_64"',
            "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded",  # /MT to match LLVM.
            "-DBUILD_SHARED_LIBS=OFF",
            "-DLIBXML2_WITH_C14N=OFF",
            "-DLIBXML2_WITH_CATALOG=OFF",
            "-DLIBXML2_WITH_DEBUG=OFF",
            "-DLIBXML2_WITH_DOCB=OFF",
            "-DLIBXML2_WITH_FTP=OFF",
            "-DLIBXML2_WITH_HTML=OFF",
            "-DLIBXML2_WITH_HTTP=OFF",
            "-DLIBXML2_WITH_ICONV=OFF",
            "-DLIBXML2_WITH_ICU=OFF",
            "-DLIBXML2_WITH_ISO8859X=OFF",
            "-DLIBXML2_WITH_LEGACY=OFF",
            "-DLIBXML2_WITH_LZMA=OFF",
            "-DLIBXML2_WITH_MEM_DEBUG=OFF",
            "-DLIBXML2_WITH_MODULES=OFF",
            "-DLIBXML2_WITH_OUTPUT=ON",
            "-DLIBXML2_WITH_PATTERN=OFF",
            "-DLIBXML2_WITH_PROGRAMS=OFF",
            "-DLIBXML2_WITH_PUSH=OFF",
            "-DLIBXML2_WITH_PYTHON=OFF",
            "-DLIBXML2_WITH_READER=OFF",
            "-DLIBXML2_WITH_REGEXPS=OFF",
            "-DLIBXML2_WITH_RUN_DEBUG=OFF",
            "-DLIBXML2_WITH_SAX1=OFF",
            "-DLIBXML2_WITH_SCHEMAS=OFF",
            "-DLIBXML2_WITH_SCHEMATRON=OFF",
            "-DLIBXML2_WITH_TESTS=OFF",
            "-DLIBXML2_WITH_THREADS=ON",
            "-DLIBXML2_WITH_THREAD_ALLOC=OFF",
            "-DLIBXML2_WITH_TREE=ON",
            "-DLIBXML2_WITH_VALID=OFF",
            "-DLIBXML2_WITH_WRITER=OFF",
            "-DLIBXML2_WITH_XINCLUDE=OFF",
            "-DLIBXML2_WITH_XPATH=OFF",
            "-DLIBXML2_WITH_XPTR=OFF",
            "-DLIBXML2_WITH_ZLIB=OFF",
            "..",
        ]
    )
    run_command(["ninja", "install"])
    return extra_cmake_flags, extra_cflags


def build_zstd(bootstrap = False) -> tuple[list[str], list[str]]:
    """Download and build zstd lib"""
    ZSTD_VERSION = "zstd-1.5.5"

    # The zstd-1.5.5.tar.gz was downloaded from
    #   https://github.com/facebook/zstd/releases/
    # and uploaded as follows.
    # $ gsutil cp -n -a public-read zstd-$VER.tar.gz \
    #   gs://chromium-browser-clang/tools
    src_dir = join(TOOLS_BUILD_DIR, ZSTD_VERSION)

    build_dir = join(src_dir, "cmake_build")
    install_dir = join(build_dir, "install")
    include_dir = join(install_dir, "include")
    lib_dir = join(install_dir, "lib")

    if sys.platform == "win32":
        zstd_lib = join(lib_dir, "zstd_static.lib")
    else:
        zstd_lib = join(lib_dir, "libzstd.a")

    extra_cmake_flags = [
        "-DLLVM_ENABLE_ZSTD=ON",
        "-DLLVM_USE_STATIC_ZSTD=ON",
        '-Dzstd_INCLUDE_DIR="%s"' % include_dir,
        '-Dzstd_LIBRARY="%s"' % zstd_lib,
    ]
    extra_cflags = []

    if not bootstrap and exists(zstd_lib):
        return extra_cmake_flags, extra_cflags

    print("Building zstd.")
    rmdir(src_dir)

    pack_file = join(TOOLS_DIR, ZSTD_VERSION + ".tar.gz")
    unpack(pack_file, TOOLS_BUILD_DIR)

    os.mkdir(build_dir)
    os.chdir(build_dir)

    run_command(
        [
            "cmake",
            "-GNinja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_INSTALL_PREFIX=install",
            # The mac_arm bot builds a clang arm binary, but currently on an intel
            # host. If we ever move it to run on an arm mac, this can go. We
            # could pass this only if args.build_mac_arm, but zstd is small, so
            # might as well build it universal always for a few years.
            '-DCMAKE_OSX_ARCHITECTURES="arm64;x86_64"',
            "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded",  # /MT to match LLVM.
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
            "-DZSTD_BUILD_SHARED=OFF",
            "../build/cmake",
        ]
    )
    run_command(["ninja", "install"])
    return extra_cmake_flags, extra_cflags


def unpack_debian_sysroot(platform_name: str) -> str:
    toolchain_name = f"debian_bullseye_{platform_name}_sysroot"
    sysroot_dir = join(TOOLS_BUILD_DIR, "debian_sysroot/" + toolchain_name)

    rmdir(sysroot_dir)
    pack_file = join(TOOLS_DIR, f"debian_sysroot/{toolchain_name}.tar.xz")
    unpack(pack_file, join(TOOLS_BUILD_DIR, "debian_sysroot"))

    return sysroot_dir


def unpack(pack_file: str, output_dir: str) -> None:
    with open(pack_file, "rb") as f:
        mkdir(output_dir)
        if pack_file.endswith(".zip"):
            zipfile.ZipFile(f).extractall(path=output_dir)
        else:
            t = tarfile.open(mode="r:*", fileobj=f)
            t.extractall(path=output_dir)


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
