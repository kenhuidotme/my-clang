# debian_sysroot
This uses basically Chromium's sysroots, but with minor changes:
- glibc version bumped to 2.18 to make __cxa_thread_atexit_impl work (clang can require 2.18; chromium currently doesn't)
- libcrypt.so.1 reversioned so that crypt() is picked up from glibc
The sysroot was built at: https://chromium-review.googlesource.com/c/chromium/src/+/3684954/1
and the hashes here are from sysroots.json in that CL.

## debian_bullseye_i386_sysroot.tar.xz
https://commondatastorage.googleapis.com/chrome-linux-sysroot/toolchain/a033618b5e092c86e96d62d3c43f7363df6cebe7/debian_bullseye_i386_sysroot.tar.xz

## debian_bullseye_amd64_sysroot.tar.xz
https://commondatastorage.googleapis.com/chrome-linux-sysroot/toolchain/2028cdaf24259d23adcff95393b8cc4f0eef714b/debian_bullseye_amd64_sysroot.tar.xz

## debian_bullseye_arm_sysroot.tar.xz
https://commondatastorage.googleapis.com/chrome-linux-sysroot/toolchain/0b9a3c54d2d5f6b1a428369aaa8d7ba7b227f701/debian_bullseye_arm_sysroot.tar.xz

## debian_bullseye_arm64_sysroot.tar.xz
https://commondatastorage.googleapis.com/chrome-linux-sysroot/toolchain/0e28d9832614729bb5b731161ff96cb4d516f345/debian_bullseye_arm64_sysroot.tar.xz
