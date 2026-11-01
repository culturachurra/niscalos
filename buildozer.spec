[app]
title = Niscalos
package.name = niscalos
package.domain = org.niscalos
source.dir = .
source.include_exts = py
source.exclude_dirs = .github,.buildozer,bin,tests
version = 2.0
requirements = python3,kivy==2.3.0,numpy,pillow,openssl,certifi,pyjnius
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
