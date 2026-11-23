# Mac 控制模块

[macOS 支持计划](../../docs/macos-support-plan.md)

Mac 版使用基于 MaaFramework 源码打补丁、自行编译的 `libMaaMacOSControlUnit.dylib`，其余库沿用官方运行时。只替换这一个文件：工厂函数签名和版本号与官方一致，官方主库按文件名加载它。Windows 构建不涉及本目录。

| 文件 | 作用 |
| --- | --- |
| `upstream.lock.json` | 锁定 MaaFramework 标签与提交、MaaUtils 子模块提交、MaaDeps 预编译依赖和官方运行时的 SHA-256。 |
| `patches/` | 按文件名顺序应用到上游源码的补丁。 |
| `build.py` | 下载并校验源码与依赖，应用补丁，编译控制模块。 |
| `probe.py` | 通过官方主库驱动控制模块：连接、截图、销毁、重连，不发送输入。 |

## 编译

需要 Xcode 命令行工具和 git。`cmake` 与 `ninja` 由脚本用 pip 装进工作目录下的虚拟环境，不安装到系统。

```sh
python3 -I native/macos-control-unit/build.py --runtime-out .build/macos-control-unit/runtime
```

- 产物在 `.build/macos-control-unit/out/patched/`，`build.json` 记录应用的补丁和产物哈希。
- `--stock` 不应用补丁，编译上游原版，用于先确认工具链本身没有问题。
- `--runtime-out` 下载同一标签的官方运行时，核对哈希后写出一份只替换了控制模块的副本，供 `probe.py` 和开发运行使用。

升级 MaaFramework 时，同时更新 `upstream.lock.json` 中的全部条目，重新生成补丁，并用 `--stock` 和 `probe.py` 重新验证。

## 验证

```sh
python probe.py windows --runtime .build/macos-control-unit/runtime
python probe.py lifecycle --runtime .build/macos-control-unit/runtime --game --log-dir /tmp/probe-log
```

`probe.py` 需要已安装 `maafw` 的 Python，并且宿主应用已获得「屏幕录制」权限。`lifecycle` 报告每轮的连接结果、画面尺寸和截图延迟，并从日志核对控制模块的创建与销毁次数，以及进程中实际加载的控制模块路径与哈希。
