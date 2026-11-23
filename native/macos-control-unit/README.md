# Mac 控制模块

[macOS 支持计划](../../docs/macos-support-plan.md)

Mac 版使用基于 MaaFramework 源码打补丁、自行编译的 `libMaaMacOSControlUnit.dylib`，其余库沿用官方运行时。只替换这一个文件：工厂函数签名和版本号与官方一致，官方主库按文件名加载它。Windows 构建不涉及本目录。

| 文件 | 作用 |
| --- | --- |
| `upstream.lock.json` | 锁定 MaaFramework 标签与提交、MaaUtils 子模块提交、MaaDeps 预编译依赖和官方运行时的 SHA-256。 |
| `patches/` | 按文件名顺序应用到上游源码的补丁。 |
| `build.py` | 下载并校验源码与依赖，应用补丁，编译控制模块。 |
| `probe.py` | 通过官方主库驱动控制模块：连接、截图、销毁、重连，不发送输入。 |

## 补丁内容

`0001` 增加两个只有本构建认识的取值，官方取值的行为不变。官方控制模块收到这两个值时会报「Unknown method」，之后既不截图也不发送输入，不会悄悄换成别的行为。

| 取值 | 行为 |
| --- | --- |
| 截图 `1 << 1`（DisplayWindow） | 方法 A：按窗口所在显示器过滤出这一个窗口，`sourceRect` 取窗口在该显示器内的可见部分，按真实像素输出。缓存窗口查找和过滤器；每次截图先用 CGWindowList 和 CGDisplay 比对窗口位置、显示器位置和显示模式，有变化才重建；截图期间几何变化时丢弃这一帧。 |
| 输入 `GlobalEvent \| PostToPid`（Hybrid） | 按键和文字发给游戏进程；绝对坐标的鼠标走全局事件，不激活窗口；相对移动发给游戏进程。必须与 DisplayWindow 截图一起使用。 |

只在前台工作：按键、文字、相对移动和点击都要求游戏是最前面的应用（`GetFrontProcess`，已弃用但仍可用；辅助功能的「当前焦点应用」在部分应用前台时会失败，`NSWorkspace` 在没有主循环的进程里不会更新）。

点击坐标按最近一帧的原点和缩放换算到桌面坐标，截图和点击共用同一套数据，每个偏移只加一次。按下前还要满足：

- 有可用的帧，且不是几何变化后的第一帧（点击可能是按变化前的画面算出的）；
- 窗口仍在，窗口位置、显示器位置和显示模式与该帧一致；
- 坐标在画面内；
- 用辅助功能按坐标命中测试，接收点击的是游戏进程，而不是 Dock、菜单栏或别的窗口。窗口列表无法用来判断：本机就有三个应用在所有窗口之上放着全屏的透明窗口，点击会穿过它们；
- 在游戏进程自己的窗口里，该坐标最前面的是已连接的窗口。

任一不满足就不发送。松开只跟随被接受的按下，位置和进程与按下时相同，并且总是发送。拖动只跟随被接受的按下。主库停止时会在另一个线程直接松开按键，所以全部状态由一把锁保护；停止线程送来的松开如果对应的按下还没到，随后 0.5 秒内到达的这次按下会被取消。`inactive()` 和销毁时松开仍按住的键和鼠标键。滚轮暂不支持。

`get_info()` 增加 `maanikki_patch`，agent 据此区分本构建和官方控制模块。

## 编译

需要 Xcode 命令行工具和 git。`cmake` 与 `ninja` 由脚本用 pip 装进工作目录下的虚拟环境，不安装到系统。

```sh
python3 -I native/macos-control-unit/build.py --runtime-out .build/macos-control-unit/runtime
```

- 产物在 `.build/macos-control-unit/out/patched/`，`build.json` 记录应用的补丁和产物哈希。
- `--stock` 不应用补丁，编译上游原版，用于先确认工具链本身没有问题。
- `--runtime-out` 下载同一标签的官方运行时，核对哈希后写出一份只替换了控制模块的副本，供 `probe.py` 和开发运行使用。目标目录必须是新的、空的，或者是本脚本以前写出的目录，脚本不会删除其他目录。
- 产物使用与官方相同的 `@loader_path`、`@executable_path` 两条运行路径，依赖只能来自 `@rpath` 或系统库，脚本编译后会检查。
- 重复使用工作目录时，脚本会还原上游源码并检查 MaaUtils 没有本地改动，但信任已解压的 MaaDeps。CI 和发布构建使用全新的 `--work` 目录。

升级 MaaFramework 时，同时更新 `upstream.lock.json` 中的全部条目，重新生成补丁，并用 `--stock` 和 `probe.py` 重新验证。

## 验证

```sh
python probe.py windows --runtime .build/macos-control-unit/runtime
python probe.py lifecycle --runtime .build/macos-control-unit/runtime --profile maanikki --game \
    --log-dir /tmp/probe-log --expect-unit <build.json 中的 sha256>
python probe.py refusal-check --runtime .build/macos-control-unit/runtime --profile maanikki \
    --window <一个不在前台的应用的窗口> --out /tmp/refusal
```

`--profile maanikki` 使用补丁新增的截图和输入取值；不加时是官方取值，在补丁版里走的也是官方代码。

`probe.py` 需要已安装 `maafw` 的 Python，并且宿主应用已获得「屏幕录制」权限。`lifecycle` 报告每轮的连接结果、画面尺寸和截图延迟。只有同时满足以下条件才算通过：至少两轮，每轮都连接成功并截到画面；新日志里控制模块的创建与销毁次数都等于轮数；进程中实际加载的主库和 MaaUtils 与 `upstream.lock.json` 一致，控制模块的哈希等于 `--expect-unit`（`stock` 表示官方版）。截图失败时不读取主库缓存的上一张图。

`refusal-check` 不发送任何输入：目标窗口所属应用在前台时直接退出；否则点击、按键和相对移动都必须被控制模块拒绝并记录日志。`input-check` 会向游戏发送按键和点击，只能在维护者在场、同意后运行，见 `probe.py` 的说明。
