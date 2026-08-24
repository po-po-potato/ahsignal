[app]

# App 元信息
title = AH策略
package.name = ahsignal
package.domain = org.ahsignal
source.dir = .
source.include_exts = py,json,txt
version = 0.1.0

# 依赖：kivy + pytdx（纯 Python，引擎零 numpy/pandas）
# python3 锁 3.11：3.14 与 kivy 2.3.0 有已知兼容问题
requirements = python3==3.11,kivy==2.3.0,pytdx

# Android 配置
android.permissions = INTERNET
android.archs = arm64-v8a
android.api = 33
android.minapi = 24

[buildozer]
log_level = 2
warn_on_root = 1
