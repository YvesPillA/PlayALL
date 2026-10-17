# PlayALL

让 AI 通过游戏原生接口游玩的 Windows 桌面软件。你保持游戏窗口打开，PlayALL 负责模型、一个长期总提示词、交流和暂停。

## 从源码运行

需要 Windows、Python 3.12。所有命令在仓库根目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe desktop_run.py
```

安装依赖后，也可以双击 `启动PlayALL.cmd`。源码仓库不附带可执行程序、游戏文件、加载器二进制或个人运行数据。

## 使用

1. 在「设置」填写兼容工具调用的模型服务地址、模型名和 API Key，测试连接并保存。支持 DeepSeek、其他兼容接口，以及不请求模型的「免费演练」固定脚本。
2. 首次接入游戏或更新组件时，先在游戏里保存并退出，再在 PlayALL 设置中选择游戏路径、准备连接组件。
3. 亲自打开游戏，在 PlayALL「选择进程…」中选已适配、版本已核对的进程并连接。Factorio 需要先进入单人地图。连接不启动游戏，也不调用 AI。
4. 填写总提示词，点「开始游玩」。独立的「AI 思路与交流」窗口自动打开，显示公开观察/计划摘要、行动结果和对话；可最大化、调字号和调整分栏。
5. 随时发消息，Enter 发送、Shift+Enter 换行。重要操作逐次询问，在回复前停止后续动作。聊天会参考近期实际工具结果，区分完成、失败和待确认。
6. 点「暂停」交还游戏操作，继续时重新观察当前进度。关闭阅读窗口只是隐藏；要停止 AI 请点暂停。关闭主程序会保存并断开，保留游戏窗口。

没有每局目标、会话轮数、token 或费用上限。上下文采用滚动记录；单次请求和单次动作仍有必要的协议边界。

## 当前适配

| 游戏 | 接入方式与范围 |
| --- | --- |
| Factorio 2.0.77 Steam Windows | 游戏内置本机 RCON、自写模组、用户现有单人角色。14 类动作，包含采矿、制作、建造、物品转移、科研、旋转和设置配方。详见 [Factorio 说明](docs/factorio.md)。 |
| 幼神 Demo / Aistalt | 自写 Unity / FairyGUI 事件桥接。需先在本机构建组件，游戏程序集不随仓库发布。详见 [幼神说明](docs/aistalt.md)。 |
| 采矿练习场 | 独立小型练习环境；免费演练是固定脚本，不能当作真实 AI 或 Factorio。 |

Factorio 当前交互限定普通品质、单人、原版/官方扩展；载具、瞄准、列车时刻表、太空平台等尚无专属动作接口。幼神的长按等未完成能力见 [已知问题](docs/issues.md)。[验证范围](docs/factorio-validation-matrix.md)区分真实玩家流程、隔离测试和未覆盖系统。

## 构建桌面包

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe tools\build_desktop.py --stage-only
```

产物位于 `.cache/package/PlayALL`；分发时保留 exe 旁的 `_internal` 文件夹。图标由源码生成。需要幼神适配时，先按其说明下载已校验的加载器并构建自写桥接，再构建桌面包。

## 验证与贡献

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe tools\desktop_qa.py
.\.venv\Scripts\python.exe tools\focus_window_qa.py
git add --all
.\.venv\Scripts\python.exe tools\check_publication.py
```

单元测试使用临时数据和模拟服务，不连接真实模型或接管游戏。实际游戏验证脚本必须由操作者明确选择测试进程和测试存档，见 [适配器约定](docs/adapters.md)。历史 Web / 服务器工具保留在 `run.py`、`web` 和相关模块，默认桌面入口是 `desktop_run.py`。

本机密钥、会话、日志、存档、截图和交接笔记不应提交。提交前检查覆盖 Git 索引，CI 再检查提交树；[隐私说明](docs/privacy.md)列出运行数据和模型数据流。第三方组件来源见 [依赖说明](docs/dependencies.md)。
