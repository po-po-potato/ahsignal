[app]

# App 元信息
title = AH策略
package.name = ahsignal
package.domain = org.ahsignal
source.dir = .
source.include_exts = py,json,txt
source.include_patterns = assets/*,main.py
version = 0.1.0

# 依赖：kivy + pytdx（纯 Python，引擎零 numpy/pandas）
requirements = python3,kivy==2.3.0,pytdx

# Android 配置
android.permissions = INTERNET
android.archs = arm64-v8a
android.api = 33
android.minapi = 24
android.ndk_api = 24

# 图标（可选，无则用默认）
# icon.filename = %(source.dir)s/icon.png

# 打包方式
presplash.filename = %(source.dir)s/presplash.png
android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
