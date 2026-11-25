#!/usr/bin/env python3
"""Build the arm64 macOS RDP client with pinned static dependencies."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request


OPENSSL = 'https://ghcr.io/v2/homebrew/core/openssl/3/blobs/sha256:82b33ce156b289eaf95657b9ec16e8f49cb49c882cdeaeaf57e3e7d67fa85e25'
SOURCES = {
    'FreeRDP': (
        'freerdp-3.33.1', 'freerdp-3.33.1.tar.gz',
        'https://github.com/FreeRDP/FreeRDP/releases/download/3.33.1/freerdp-3.33.1.tar.gz',
        '4431bbf04d370545cfe2924b9d9f116b95c90c64ff156b2550bbaf82d34e7124'),
    'OpenSSL': (
        'openssl@3/3.5.9', 'openssl-sequoia.bottle.tar.gz', OPENSSL,
        '82b33ce156b289eaf95657b9ec16e8f49cb49c882cdeaeaf57e3e7d67fa85e25'),
    'SDL3': (
        'SDL3-3.4.20', 'SDL3-3.4.20.tar.gz',
        'https://github.com/libsdl-org/SDL/releases/download/release-3.4.20/SDL3-3.4.20.tar.gz',
        'f3bb06f55bed9557227a700af2ea62e49497ebdf58037732a731a5fb3ef8581f'),
    'SDL3_ttf': (
        'SDL3_ttf-3.2.2', 'SDL3_ttf-3.2.2.tar.gz',
        'https://github.com/libsdl-org/SDL_ttf/releases/download/release-3.2.2/SDL3_ttf-3.2.2.tar.gz',
        '63547d58d0185c833213885b635a2c0548201cc8f301e6587c0be1a67e1e045d'),
    'FreeType': (
        'freetype-2.15.0', 'freetype-2.15.0.tar.xz',
        'https://download.savannah.gnu.org/releases/freetype/freetype-2.15.0.tar.xz',
        '93cd47eb719c325107f2d3cf087d06299e944c3b48a40b55537302304dea1dd5'),
}


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def verify_library_paths(binary):
    if subprocess.check_output(['lipo', '-archs', binary], text=True).strip() != 'arm64':
        raise RuntimeError('远程桌面组件不是 arm64。')
    for row in subprocess.check_output(['otool', '-L', binary], text=True).splitlines()[1:]:
        library = row.strip().split(' (', 1)[0]
        if not library.startswith(('/usr/lib/', '/System/Library/')):
            raise RuntimeError('远程桌面组件依赖构建机上的库：'+library)


def download(work, name):
    directory, filename, url, digest = SOURCES[name]
    archive = work / filename
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        temporary = archive.with_suffix('.partial')
        try:
            request = urllib.request.Request(url)
            if name == 'OpenSSL':
                # Verified Homebrew arm64 Sequoia bottle. Only the static TLS libraries are linked.
                scope = 'https://ghcr.io/token?service=ghcr.io&scope=repository:homebrew/core/openssl/3:pull'
                with urllib.request.urlopen(scope, timeout=30) as response:
                    token = json.load(response)['token']
                request.add_unredirected_header('Authorization', 'Bearer '+token)
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                raise RuntimeError(name+' 归档 SHA-256 不匹配。')
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    source = work / directory
    if not source.exists():
        run('tar', '-xf', archive, '-C', work)
    return source


def build(work, cmake):
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    sources = {name: download(work, name) for name in SOURCES}
    deps = work / 'deps'
    openssl = sources['OpenSSL']
    mapping = work / 'map-source-paths'
    # The launcher applies the mapping without exposing the build path in FreeRDP build flags.
    mapping.write_text('''#!/bin/sh
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
compiler=$1
shift
exec "$compiler" "-ffile-prefix-map=$root=/_/rdp" "$@"
''')
    mapping.chmod(0o755)
    jobs = str(min(os.cpu_count() or 2, 8))

    def compile_project(name, options, target=None):
        directory = work / (name+'-build')
        run(cmake, '-S', sources[name], '-B', directory, '-DCMAKE_BUILD_TYPE=Release',
            '-DCMAKE_OSX_ARCHITECTURES=arm64', '-DCMAKE_OSX_DEPLOYMENT_TARGET=15.0',
            '-DCMAKE_C_FLAGS=', '-DCMAKE_CXX_FLAGS=',
            f'-DCMAKE_C_COMPILER_LAUNCHER={mapping}', f'-DCMAKE_CXX_COMPILER_LAUNCHER={mapping}',
            f'-DCMAKE_INSTALL_PREFIX={deps}', f'-DCMAKE_PREFIX_PATH={deps}',
            '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF', '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF',
            '-DCMAKE_IGNORE_PREFIX_PATH=/opt/local;/opt/homebrew;/usr/local', *options)
        run(cmake, '--build', directory, '--parallel', jobs, *(['--target', target] if target else []))
        if not target:
            run(cmake, '--install', directory)
        return directory

    compile_project('SDL3', ['-DSDL_SHARED=OFF', '-DSDL_STATIC=ON',
                             '-DSDL_TEST_LIBRARY=OFF', '-DSDL_TESTS=OFF', '-DSDL_EXAMPLES=OFF'])
    compile_project('FreeType', ['-DBUILD_SHARED_LIBS=OFF', '-DFT_DISABLE_HARFBUZZ=ON',
                                 '-DFT_DISABLE_HVF=ON', '-DFT_DISABLE_BZIP2=ON',
                                 '-DFT_DISABLE_PNG=ON', '-DFT_DISABLE_BROTLI=ON'])
    compile_project('SDL3_ttf', ['-DBUILD_SHARED_LIBS=OFF', '-DSDLTTF_VENDORED=OFF',
                                 '-DSDLTTF_HARFBUZZ=OFF', '-DSDLTTF_PLUTOSVG=OFF',
                                 '-DSDLTTF_SAMPLES=OFF'])
    directory = compile_project('FreeRDP', [
        '-DBUILD_SHARED_LIBS=OFF', '-DBUILD_TESTING=OFF',
        '-DCMAKE_INSTALL_PREFIX=/usr/local', '-DCMAKE_SKIP_RPATH=ON',
        '-DWITH_SERVER=OFF', '-DWITH_PROXY=OFF', '-DWITH_SAMPLE=OFF',
        '-DWITH_CLIENT_SDL=ON', '-DWITH_CLIENT_SDL2=OFF', '-DWITH_CLIENT_SDL3=ON',
        '-DWITH_SDL_LINK_SHARED=OFF', '-DWITH_X11=OFF', '-DWITH_WAYLAND=OFF',
        '-DWITH_WINPR_TOOLS=ON', '-DWITH_MANPAGES=OFF', '-DWITH_KRB5=OFF',
        '-DWITH_FFMPEG=OFF', '-DWITH_SWSCALE=OFF', '-DWITH_AAD=OFF',
        '-DWITH_JSON_DISABLED=ON', '-DWITH_URIPARSER=OFF', '-DWITH_PCSC_WINPR=OFF',
        '-DCHANNEL_URBDRC=OFF', '-DOPENSSL_USE_STATIC_LIBS=ON', f'-DOPENSSL_ROOT_DIR={openssl}',
        f"-DOPENSSL_SSL_LIBRARY={openssl / 'lib/libssl.a'}",
        f"-DOPENSSL_CRYPTO_LIBRARY={openssl / 'lib/libcrypto.a'}",
    ], target='sdl3-freerdp')
    executable = directory / 'client' / 'SDL' / 'SDL3' / 'sdl-freerdp'
    verify_library_paths(executable)
    if str(work).encode() in executable.read_bytes():
        raise RuntimeError('远程桌面组件包含构建目录路径。')
    payload = work / 'payload'
    licenses = payload / 'licenses'
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(executable, payload / executable.name)
    (payload / executable.name).chmod(0o755)
    for name, relative in [
        ('FreeRDP', 'LICENSE'), ('OpenSSL', 'LICENSE.txt'), ('SDL3', 'LICENSE.txt'),
        ('SDL3_ttf', 'LICENSE.txt'), ('FreeType', 'docs/FTL.TXT'),
    ]:
        shutil.copyfile(sources[name] / relative, licenses / (name+'-LICENSE.txt'))
    shutil.copyfile(sources['FreeRDP'] / 'resources/font/OFL.txt', licenses / 'OpenSans-OFL.txt')
    return payload
