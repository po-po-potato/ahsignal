# AH策略 v7 · Android APK

手机本地运行的 AH 策略信号 App(Kivy + Buildozer)。

## 工作原理

点【开始计算】→ 增量补日线 → 拉实时价 → 引擎回放 → 显示今日信号
（继续持有 / 今日买入 / 今日卖出 / 候选 Top5）

- 数据/代码首次运行自动复制到应用私有目录
- 依赖仅 kivy + pytdx（纯 Python，引擎零 numpy/pandas）
- 不依赖任何电脑，手机本地计算

## 构建 APK

推到 GitHub 后，Actions 自动构建（约 15-30 分钟）：

1. 创建仓库并推送本目录内容
2. Actions → Build APK → 等待完成
3. 下载 `ahsignal-apk` artifact → 安装到手机

或本地构建：`buildozer android debug`（需 Linux/WSL + Android SDK/NDK）

## 目录结构

```
main.py                  # Kivy 入口
buildozer.spec           # 打包配置
assets/                  # 策略代码 + 数据文件（打包进 APK，运行时可写目录复制）
.github/workflows/       # GitHub Actions 自动构建
```

## 注意

- 手机 CPU 约为 PC 1/3~1/5，点刷新约 1-3 分钟
- 手机网络直连通达信服务器可能不稳，失败自动降级为按收盘价回放
- 仅供研究参考，不构成投资建议
