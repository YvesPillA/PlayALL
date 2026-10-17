# 接入下一款游戏

PlayALL 的游戏列表来源是 `playall/games/registry.py`。Factorio 是第一个真实适配器，框架不要求下一款游戏使用 RCON、Lua、坐标或 Factorio 的动作集合。

## 适配器约定

适配器实例接收 `(data_directory, settings, emit)`，提供：

| 方法 | 责任 |
| --- | --- |
| `prepare(control, new_world=False)` | 核对用户选择的已运行进程和游戏版本；不自动启动游戏；循环中检查暂停与退出 |
| `connect_window(control)` | 对 `has_game_window=True` 的游戏等待实际窗口/游戏连接就绪；成功前 Runner 不调用 AI |
| `window_ready()` | 检查实际连接仍在；不能仅以启动进程成功代替进入游戏 |
| `observe()` | 读取真实游戏状态，返回可 JSON 序列化的字典 |
| `execute(action, control)` | 校验后执行真实动作；提交前进入 `control.action()`，长动作反复 `control.check()` |
| `pause()` / `resume()` | 停止/恢复 AI 控制；可以保留动作或取消后重规划。返回 `{replan: true}` 时 Runner 废弃旧响应并重新观察；`manual_control: true` 表示玩家可接手 |
| `checkpoint()` | 确认存档确实生成；失败抛异常，保留可重试的活状态 |
| `cancel()` | 软件退出时中止未完成动作 |
| `close()` | 释放自己的资源。自有服务器保存失败时保留活状态；用户拥有的图形游戏保持打开并释放桥接连接/心跳 |
| `watch()` | 可选，只检查连接并返回 `{ok, message}`，不打开或聚焦游戏 |

动作结果至少包含 `ok: bool`，及 `message` 或 `error`。进度、道具、地图、资源必须来自实际游戏，不能用构造数据代替成功证据。

`checkpoint()` 可以返回严格布尔的 `saved` 与可选 `pending`。尚在菜单或加载中不能伪报已保存；已进入可保存状态却写入/读回失败应抛异常。Runner 在游戏保存失败时仍保存 AI 会话，再报告错误。没有自有活进程的适配器也必须执行最终连接清理。

恢复时 `resume_event` 尚未开启，`connect_window` 接收的 control 会检查退出但不等待继续事件；不能产生恢复死锁。适配器的暂停只代表 AI 停止，不应让用户丧失游戏操作权。

## 注册

```python
register_game(GameSpec(
    id="your-game",
    label="游戏名称",
    factory="playall.games.your_game:YourGame",
    rules="描述该游戏及其真实操作约束，不混入其他游戏规则。",
    tool_factory="playall.games.your_game:tools_for_game",
    validator="playall.games.your_game:validate_action",
    has_game_window=True,
    path_setting="your_game_path",
    executable_name="YourGame.exe",
    process_attach=True,
))
```

`tool_factory()` 返回游戏自己的工具 schema，包含一个 `game_action` 和通用的 `report_progress(summary)`。`validator(action)` 可以支持该游戏自己的字段，不需要使用 Factorio 的 name/x/y/count。AI 输出只进入这个校验器和 execute，不能当作代码执行。注册 ID 也是自己的会话文件名，因此限定字母、数字、连字符和下划线。

工厂/工具/校验器只从已经安装的模块中解析。注册新模块后重新构建软件；打包会收集 `playall.games` 子模块。

`path_setting` 和 `executable_name` 用于通用的设置路径选择器；对应设置字段还需加入 Settings 的默认值和保存白名单。幼神的控件动作 schema 与 Factorio 的生产动作完全独立。

`process_attach=True` 表示已实现只接入用户所选进程的契约。settings 中的 game_process 仅在内存传入，包括 PID、名称、完整路径、创建时间；动作前核对身份，幼神桥接端再核对 PID，Factorio 则通过只读 IP Helper API 核对本机监听端口所属 PID。不能把本机身份加入模型观察。当前 Factorio 与幼神均设置此能力。connect_only 在准备阶段不等待继续，接入完成后保持 AI 未开始，直到明确 resume。

`connection_preparer` 指向关闭游戏时显式准备组件的函数 `(directory, path, emit)`，返回 `ok/message`；`connection_instructions` 提供该适配器自己的简短接入说明。设置界面不硬编码某一个游戏的安装逻辑。准备接入所选进程的 `prepare` 不得顺便安装组件或修改正在运行的游戏。

主界面由进程自动确定适配器。要进入选择列表，还需 process_verifier 指向只读版本核对函数：接收已查询路径/创建时间的 GameProcess，只有核对通过返回 True。窗口只检查名称匹配的已启用游戏，不读取其他程序详情。

需要重要操作确认的适配器实现 review_action(action)、approve_action(ticket)、reject_action(ticket)、execute_approved(action, approval, control)。review 返回 requires_confirmation/question/ticket；批准凭据只由真人回答产生，不允许模型提供批准字段。凭据绑定精确动作、控件和当前界面/会话，过期及重复使用拒绝。原生层也要阻止待确认时的继续和后续动作。checkpoint_due() 用于可选的间隔/进度调度；没有这个方法不会逐轮请求游戏存档，明确暂停和关闭仍请求 checkpoint。

## 用 REA 分析与验证

1. 确认具体游戏版本、安装包和运行进程；用 `tools/rea.cmd inspect-artifact <绝对路径>` 固定哈希和格式。
2. 优先检查公开的模组、脚本、命令行和遥测接口。有现成可验证接口时不必进行整包反编译。
3. 无公开接口时，围绕观察、输入、存档和暂停分别提出可检验的问题；根据格式选择 REA 的原生、托管、JavaScript 或其他分析提供者。分析引擎缺失时按 REA 的恢复说明安装对应能力，不能把“未配置”当作分析结果。
4. 做单个最小真实动作，记录前后状态、坐标/物品变化和引擎结果，验证碰撞、时间、材料等约束。
5. 反复验证暂停中无新动作、继续同一状态、重启恢复、断网暂停、保存失败可重试及 owned 进程清理。
6. 将静态推导与运行证据分开写入验收记录；只有实际通过的动作才能对用户宣称支持。

目前的 REA 安装为 6.4.0。该版本 Windows 主机的原生 UI 捕获提供者不可用；不能把 macOS 专属调用宣称成 Windows 运行证据。后台 Qt 检查使用 offscreen。实际游戏窗口由用户打开，助手不发送桌面键鼠输入；独立命令行引擎检查只使用自己创建的临时地图和测试进程。REA 继续负责支持的包、PE/CLI 身份、资源与元数据任务。
