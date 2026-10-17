# 依赖与构建来源

运行时：Python 3.12、PySide6 Essentials（requirements.txt）。Windows密钥保护使用系统DPAPI；不包含模型密钥或游戏控制凭据。

桌面构建额外需要PyInstaller和Pillow（requirements-dev.txt）。图标由PlayALL自己的绘图代码生成。

幼神加载器使用[BepInEx官方5.4.23.5发布资产](https://github.com/BepInEx/BepInEx/releases/tag/v5.4.23.5)。bootstrap脚本验证固定SHA-256，只写本机.cache。BepInEx及其依赖遵循各自上游许可，仓库不提交其运行库；构建包的分发者应保留相应许可信息。游戏程序集仅在合法本机安装中引用，不复制进源码或桌面包。

可选REA工具由tools/bootstrap_rea.py在本机.cache中准备，用于有范围的静态检查。下载物、分析缓存和原始证据不入Git。
