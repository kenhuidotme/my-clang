#!/usr/bin/env python3

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
import tempfile
import time
import urllib
import urllib.error
import urllib.request
import zipfile
import zlib
from os.path import dirname, exists, expandvars


def norm(p: str) -> str:
    return os.path.normpath(p)


def join(b: str, p: str) -> str:
    return norm(os.path.join(b, p))


def ensure_dir_exists(path: str) -> None:
    if not exists(path):
        os.makedirs(path)


def rmtree(p: str) -> None:
    shutil.rmtree(p, onerror=_handle_rmtree_read_only)


def _handle_rmtree_read_only(f, p, e):
    if f in (os.rmdir, os.remove, os.unlink) and e[1].errno == errno.EACCES:
        os.chmod(p, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
        f(p)
    else:
        raise


CDS_URL = "https://commondatastorage.googleapis.com/chromium-browser-clang"

BUILD_DIR = norm(dirname(__file__))

LLVM_PROJECT_DIR = join(BUILD_DIR, "../llvm-project")
LLVM_BOOTSTRAP_DIR = join(BUILD_DIR, "out/llvm-bootstrap")
LLVM_BOOTSTRAP_INSTALL_DIR = join(BUILD_DIR, "out/llvm-bootstrap-install")
LLVM_BUILD_DIR = join(BUILD_DIR, "out/llvm-build")
LLVM_INSTALL_DIR = join(BUILD_DIR, "out/llvm-install")
LLVM_BUILD_TOOLS_DIR = join(BUILD_DIR, "out/llvm-build-tools")


def run_command(command: list[str], env=None, fail_hard=True) -> bool:
    """Run command and return success (True) or failure; or if fail_hard is
    True, exit on failure."""
    cmd = None
    if sys.platform == "win32":
        (_, vs_dir) = detect_visual_studio_version_and_dir()
        script_path = join(vs_dir, "VC/Auxiliary/Build/vcvarsall.bat")
        cmd = [script_path, "amd64", "&&"] + command
        # Windows can't handle quote
        cmd = [i.replace('"', "") for i in cmd]
    else:
        cmd = " ".join(command)
    print("Running", cmd)
    if subprocess.call(cmd, env=env, shell=True) == 0:
        return True
    print("Failed.")
    if fail_hard:
        sys.exit(1)
    return False


def detect_visual_studio_version_and_dir() -> tuple[str, str]:
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
                    return version, path

    raise Exception(
        "No supported Visual Studio can be found."
        " Supported versions are: %s."
        % ", ".join("{} ({})".format(v, k) for k, v in MSVS_VERSIONS.items())
    )


def download_url(url: str, output_file) -> None:
    """Download url into output_file."""
    CHUNK_SIZE = 4096
    TOTAL_DOTS = 10
    num_retries = 3
    retry_wait_s = 5  # Doubled at each retry.

    while True:
        try:
            sys.stdout.write(f"Downloading {url} ")
            sys.stdout.flush()
            request = urllib.request.Request(url)
            request.add_header("Accept-Encoding", "gzip")
            response = urllib.request.urlopen(request)
            total_size = None
            if "Content-Length" in response.headers:
                total_size = int(response.headers["Content-Length"].strip())

            is_gzipped = response.headers.get("Content-Encoding", "").strip() == "gzip"
            if is_gzipped:
                gzip_decode = zlib.decompressobj(zlib.MAX_WBITS + 16)

            bytes_done = 0
            dots_printed = 0
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                bytes_done += len(chunk)

                if is_gzipped:
                    chunk = gzip_decode.decompress(chunk)  # type: ignore
                output_file.write(chunk)

                if total_size is not None:
                    num_dots = TOTAL_DOTS * bytes_done // total_size
                    sys.stdout.write("." * (num_dots - dots_printed))
                    sys.stdout.flush()
                    dots_printed = num_dots
            if total_size is not None and bytes_done != total_size:
                raise urllib.error.URLError(
                    f"only got {bytes_done} of {total_size} bytes"
                )
            if is_gzipped:
                output_file.write(gzip_decode.flush())  # type: ignore
            print(" Done.")
            return
        except (ConnectionError, urllib.error.URLError) as e:
            sys.stdout.write("\n")
            print(e)
            if (
                num_retries == 0
                or isinstance(e, urllib.error.HTTPError)
                and e.code == 404
            ):
                raise e
            num_retries -= 1
            output_file.seek(0)
            output_file.truncate()
            print(f"Retrying in {retry_wait_s} s ...")
            sys.stdout.flush()
            time.sleep(retry_wait_s)
            retry_wait_s *= 2


def download_and_unpack(
    url: str, output_dir: str, path_prefixes=None, is_known_zip=False
) -> None:
    """Download an archive from url and extract into output_dir. If path_prefixes
    is not None, only extract files whose paths within the archive start with
    any prefix in path_prefixes."""
    with tempfile.TemporaryFile() as f:
        download_url(url, f)
        f.seek(0)
        ensure_dir_exists(output_dir)
        if url.endswith(".zip") or is_known_zip:
            assert path_prefixes is None
            zipfile.ZipFile(f).extractall(path=output_dir)
        else:
            t = tarfile.open(mode="r:*", fileobj=f)
            members = None
            if path_prefixes is not None:
                members = [
                    m
                    for m in t.getmembers()
                    if any(m.name.startswith(p) for p in path_prefixes)
                ]
            t.extractall(path=output_dir, members=members)


def build_zlib():
    """Download and build zlib, and add to PATH."""
    ZLIB_VERSION = "zlib-1.2.11"

    zlib_dir = join(LLVM_BUILD_TOOLS_DIR, ZLIB_VERSION)
    if exists(zlib_dir):
        rmtree(zlib_dir)

    zip_name = ZLIB_VERSION + ".tar.gz"
    download_and_unpack(CDS_URL + "/tools/" + zip_name, LLVM_BUILD_TOOLS_DIR)
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
    rmtree("test")
    return zlib_dir


def build_libxml2():
    """Download and build libxml2"""
    LIBXML2_VERSION = "libxml2-v2.9.12"

    src_dir = join(LLVM_BUILD_TOOLS_DIR, LIBXML2_VERSION)
    if exists(src_dir):
        rmtree(src_dir)

    zip_name = LIBXML2_VERSION + ".tar.gz"
    download_and_unpack(CDS_URL + "/tools/" + zip_name, LLVM_BUILD_TOOLS_DIR)

    build_dir = join(src_dir, "build")
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
    return extra_cmake_flags, extra_cflags


def build_zstd():
    """Download and build zstd lib"""
    ZSTD_VERSION = "zstd-1.5.5"

    # The zstd-1.5.5.tar.gz was downloaded from
    #   https://github.com/facebook/zstd/releases/
    # and uploaded as follows.
    # $ gsutil cp -n -a public-read zstd-$VER.tar.gz \
    #   gs://chromium-browser-clang/tools
    src_dir = join(LLVM_BUILD_TOOLS_DIR, ZSTD_VERSION)
    if exists(src_dir):
        rmtree(src_dir)

    zip_name = ZSTD_VERSION + ".tar.gz"
    download_and_unpack(CDS_URL + "/tools/" + zip_name, LLVM_BUILD_TOOLS_DIR)

    build_dir = join(src_dir, "cmake_build")
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
            "-DZSTD_BUILD_SHARED=OFF",
            "../build/cmake",
        ]
    )
    run_command(["ninja", "install"])

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
    return extra_cmake_flags, extra_cflags


def default_target_triple() -> list[str]:
    # The default LLVM_DEFAULT_TARGET_TRIPLE depends on the host machine.
    # Set it explicitly to make the build of clang more hermetic, and also to
    # set it to arm64 when cross-building clang for mac/arm.
    ret = []
    if sys.platform == "darwin":
        if platform.machine() == "arm64":
            ret.append("-DLLVM_DEFAULT_TARGET_TRIPLE=arm64-apple-darwin")
        else:
            ret.append("-DLLVM_DEFAULT_TARGET_TRIPLE=x86_64-apple-darwin")
    elif sys.platform.startswith("linux"):
        if platform.machine() == "aarch64":
            ret.append('-DLLVM_DEFAULT_TARGET_TRIPLE="aarch64-unknown-linux-gnu"')
        elif platform.machine() == "riscv64":
            ret.append('-DLLVM_DEFAULT_TARGET_TRIPLE="riscv64-unknown-linux-gnu"')
        elif platform.machine() == "loongarch64":
            ret.append('-DLLVM_DEFAULT_TARGET_TRIPLE="loongarch64-unknown-linux-gnu"')
        else:
            ret.append('-DLLVM_DEFAULT_TARGET_TRIPLE="x86_64-unknown-linux-gnu"')
        ret.append("-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=ON")
    elif sys.platform == "win32":
        ret.append('-DLLVM_DEFAULT_TARGET_TRIPLE="x86_64-pc-windows-msvc"')
    return ret


def runtimes_triples_args() -> dict[str, dict[str, list[str] | str]]:
    # Map from triple to {
    #   "args": list of CMake vars without '-D' common to builtins and runtimes
    #   "sanitizers": bool # build sanitizer runtimes
    # }
    ret = {}
    if sys.platform.startswith("linux"):
        ret["i386-unknown-linux-gnu"] = {
            "args": [
                # TODO
                # "CMAKE_SYSROOT=%s" % sysroot_i386,
                # TODO(crbug.com/40242553): pass proper flags to i386 tests so they compile correctly
                "LLVM_INCLUDE_TESTS=OFF",
            ],
            "sanitizers": True,
        }
        ret["x86_64-unknown-linux-gnu"] = {
            "args": [
                # TODO
                # "CMAKE_SYSROOT=%s" % sysroot_amd64,
            ],
            "sanitizers": True,
        }
        # Using "armv7a-unknown-linux-gnueabhihf" confuses the compiler-rt
        # builtins build, since compiler-rt/cmake/builtin-config-ix.cmake
        # doesn't include "armv7a" in its `ARM32` list.
        # TODO(thakis): It seems to work for everything else though, see try
        # results on
        # https://chromium-review.googlesource.com/c/chromium/src/+/3702739/4
        # Maybe it should work for builtins too?
        ret["armv7-unknown-linux-gnueabihf"] = {
            "args": [
                # TODO
                # "CMAKE_SYSROOT=%s" % sysroot_arm,
                # Can't run tests on x86 host.
                "LLVM_INCLUDE_TESTS=OFF",
            ],
            "sanitizers": True,
        }
        ret["aarch64-unknown-linux-gnu"] = {
            "args": [
                # TODO
                # "CMAKE_SYSROOT=%s" % sysroot_arm64,
                # Can't run tests on x86 host.
                "LLVM_INCLUDE_TESTS=OFF",
            ],
            "sanitizers": True,
        }
    elif sys.platform == "win32":
        ret["i386-pc-windows-msvc"] = {
            "args": [],
            "sanitizers": False,
        }
        ret["x86_64-pc-windows-msvc"] = {
            "args": [],
            "sanitizers": True,
        }
        ret["aarch64-pc-windows-msvc"] = {
            "args": [
                # Can't run tests on x86 host.
                "LLVM_INCLUDE_TESTS=OFF",
            ],
            "sanitizers": False,
        }
    elif sys.platform == "darwin":
        # compiler-rt is built for all platforms/arches with a single
        # configuration, we should only specify one target triple. 'default' is
        # specially handled.
        ret["default"] = {
            "args": [
                "COMPILER_RT_ENABLE_MACCATALYST=OFF",
                "COMPILER_RT_ENABLE_IOS=OFF",
                "COMPILER_RT_ENABLE_WATCHOS=OFF",
                "COMPILER_RT_ENABLE_TVOS=OFF",
                "COMPILER_RT_ENABLE_XROS=OFF",
                # "DARWIN_ios_ARCHS=arm64",
                # 'DARWIN_iossim_ARCHS="arm64;x86_64"',
                'DARWIN_osx_ARCHS="arm64;x86_64"',
            ],
            "sanitizers": True,
        }

    return ret


def compiler_rt_cmake_flags(sanitizers=False) -> list[str]:
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
        "COMPILER_RT_BUILD_XRAY=OFF",
        # See crbug.com/1205046: don't build scudo (and others we don't need).
        'COMPILER_RT_SANITIZERS_TO_BUILD="asan;dfsan;msan;hwasan;tsan;cfi"',
        # We explicitly list all targets we want to build, do not autodetect
        # targets.
        "COMPILER_RT_DEFAULT_TARGET_ONLY=ON",
    ]
    return args


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
        # "-DCLANG_PLUGIN_SUPPORT=OFF",
        "-DCLANG_ENABLE_STATIC_ANALYZER=OFF",
        "-DCLANG_ENABLE_ARCMT=OFF",
        "-DLLVM_ENABLE_UNWIND_TABLES=OFF",
        # See crbug.com/1126219: Use native symbolizer instead of DIA
        "-DLLVM_ENABLE_DIA_SDK=OFF",
        # Link all binaries with lld. Effectively passes -fuse-ld=lld to the
        # compiler driver. On Windows, cmake calls the linker directly, so there
        # the same is achieved by passing -DCMAKE_LINKER=$lld below.
        "-DLLVM_ENABLE_LLD=ON",
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

    cflags = []
    cxxflags = []
    ldflags = []

    if sys.platform == "win32":
        base_cmake_args.append("-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded")

        # Require zlib compression.
        zlib_dir = build_zlib()
        os.environ["PATH"] = zlib_dir + os.pathsep + os.environ.get("PATH", "")

        cflags += ["-I" + zlib_dir]
        cxxflags += ["-I" + zlib_dir]
        ldflags += ["-LIBPATH:" + zlib_dir]
        base_cmake_args.append("-DLLVM_ENABLE_ZLIB=FORCE_ON")

    # Statically link libxml2 to make lld-link not require mt.exe on Windows,
    # and to make sure lld-link output on other platforms is identical to
    # lld-link on Windows (for cross-builds).
    libxml_cmake_args, libxml_cflags = build_libxml2()
    base_cmake_args += libxml_cmake_args
    cflags += libxml_cflags
    cxxflags += libxml_cflags

    if args.with_zstd:
        # Statically link zstd to make lld support zstd compression for debug info.
        zstd_cmake_args, zstd_cflags = build_zstd()
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
        print("Building bootstrap compiler")

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

        # if len(bootstrap_runtimes) > 0:
        bootstrap_args += ["-D" + f for f in compiler_rt_cmake_flags()]

        if sys.platform == "darwin":
            bootstrap_args += [
                "-DCOMPILER_RT_ENABLE_IOS=OFF",
                "-DCOMPILER_RT_ENABLE_WATCHOS=OFF",
                "-DCOMPILER_RT_ENABLE_TVOS=OFF",
            ]
            if platform.machine() == "arm64":
                bootstrap_args.append("-DDARWIN_osx_ARCHS=arm64")
            else:
                bootstrap_args.append("-DDARWIN_osx_ARCHS=x86_64")

        if exists(LLVM_BOOTSTRAP_DIR):
            rmtree(LLVM_BOOTSTRAP_DIR)
        ensure_dir_exists(LLVM_BOOTSTRAP_DIR)
        os.chdir(LLVM_BOOTSTRAP_DIR)

        run_command(["cmake"] + bootstrap_args + [join(LLVM_PROJECT_DIR, "llvm")])
        run_command(["ninja"])
        if args.run_tests:
            run_command(["ninja", "check-all"], env=test_env)
        run_command(["ninja", "install"])

        print("Bootstrap compiler installed.")

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

    # Build PDBs for archival on Windows.  Don't use RelWithDebInfo since it
    # has different optimization defaults than Release.
    # Also disable stack cookies (/GS-) for performance.
    if sys.platform == "win32":
        cflags += ["/Zi", "/GS-"]
        cxxflags += ["/Zi", "/GS-"]
        ldflags += ["/DEBUG", "/OPT:REF", "/OPT:ICF"]

    base_cmake_args.append('-DCMAKE_C_COMPILER="%s"' % cc)
    base_cmake_args.append('-DCMAKE_CXX_COMPILER="%s"' % cxx)
    if lld is not None:
        base_cmake_args.append('-DCMAKE_LINKER="%s"' % lld)

    final_install_dir = args.install_dir if args.install_dir else LLVM_INSTALL_DIR

    cmake_args = base_cmake_args + [
        '-DCMAKE_C_FLAGS="%s"' % " ".join(cflags),
        '-DCMAKE_CXX_FLAGS="%s"' % " ".join(cxxflags),
        '-DCMAKE_EXE_LINKER_FLAGS="%s"' % " ".join(ldflags),
        '-DCMAKE_SHARED_LINKER_FLAGS="%s"' % " ".join(ldflags),
        '-DCMAKE_MODULE_LINKER_FLAGS="%s"' % " ".join(ldflags),
        '-DCMAKE_INSTALL_PREFIX="%s"' % final_install_dir,
    ]

    # runtimes = None
    # if sys.platform == "win32":
    #     runtimes = ["compiler-rt", "libcxx"]
    # else:
    #     runtimes = ["compiler-rt", "libcxx", "libcxxabi", "libunwind"]
    # cmake_args.append('-DLLVM_ENABLE_RUNTIMES="%s"' % ";".join(runtimes))

    toolchain_tools = None
    if sys.platform == "win32":
        toolchain_tools = [
            "llvm-ml",
            "llvm-pdbutil",
            "llvm-readobj",
            "llvm-symbolizer",
            "llvm-undname",
        ]
    else:
        toolchain_tools = [
            "llvm-ar",
            "llvm-ml",
            "llvm-objcopy",
            "llvm-pdbutil",
            "llvm-readobj",
            "llvm-symbolizer",
            "llvm-undname",
        ]

    distribution_components = [
        "clang",
        "clang-resource-headers",
        "lld",
        "builtins",
        # "runtimes",
    ] + toolchain_tools

    cmake_args.append('-DLLVM_TOOLCHAIN_TOOLS="%s"' % ";".join(toolchain_tools))
    cmake_args.append(
        '-DLLVM_DISTRIBUTION_COMPONENTS="%s"' % ";".join(distribution_components)
    )
    cmake_args.append("-DLLVM_INSTALL_TOOLCHAIN_ONLY=ON")

    if args.thinlto:
        cmake_args.append("-DLLVM_ENABLE_LTO=Thin")

    cmake_args += default_target_triple()

    # Convert FOO=BAR CMake flags per triple into
    # -DBUILTINS_$triple_FOO=BAR/-DRUNTIMES_$triple_FOO=BAR and build up
    # -DLLVM_BUILTIN_TARGETS/-DLLVM_RUNTIME_TARGETS.
    all_triples = ""
    triples_args = runtimes_triples_args()
    for triple in sorted(triples_args.keys()):
        all_triples += (";" if all_triples else "") + triple
        for arg in triples_args[triple]["args"]:
            assert not arg.startswith("-")
            # 'default' is specially handled to pass through relevant CMake flags.
            if triple == "default":
                cmake_args.append("-D" + arg)
            else:
                cmake_args.append("-DRUNTIMES_" + triple + "_" + arg)
                cmake_args.append("-DBUILTINS_" + triple + "_" + arg)
        for arg in compiler_rt_cmake_flags(
            sanitizers=triples_args[triple]["sanitizers"]  # type: ignore
        ):
            # 'default' is specially handled to pass through relevant CMake flags.
            if triple == "default":
                cmake_args.append("-D" + arg)
            else:
                cmake_args.append("-DRUNTIMES_" + triple + "_" + arg)

    cmake_args.append("-DLLVM_BUILTIN_TARGETS=" + all_triples)
    cmake_args.append("-DLLVM_RUNTIME_TARGETS=" + all_triples)

    if exists(LLVM_BUILD_DIR):
        rmtree(LLVM_BUILD_DIR)
    ensure_dir_exists(LLVM_BUILD_DIR)
    os.chdir(LLVM_BUILD_DIR)

    run_command(
        ["cmake"] + cmake_args + [join(LLVM_PROJECT_DIR, "llvm")],
    )
    run_command(["ninja", "-C", LLVM_BUILD_DIR])
    if args.run_tests:
        run_command(["ninja", "check-all"], env=test_env)
    run_command(["ninja", "install-distribution"])

    print("Clang build was successful.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
