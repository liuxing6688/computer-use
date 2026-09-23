# computer-use

在 Claude Code CLI 中用自然语言操控本机 Windows 桌面的 MCP server。术语一律以 [`CONTEXT.md`](CONTEXT.md) 为准，设计决策见 [`docs/adr/`](docs/adr)。

当前只有只读工具：

| 工具 | 作用 |
| --- | --- |
| `list_windows` | 列出桌面上可操作的窗口，含标题、进程名与屏幕矩形 |
| `observe_window` | 截取一个窗口（不截全屏），附带截图 ID、采集时刻、缩放比、DPI 缩放与屏幕偏移 |
| `zoom` | 把某张截图上的矩形按屏幕原尺寸裁出，附带裁剪相对窗口的偏移 |

截图长边超过 1568 像素时由服务端先缩小，并在 `scale` 中如实报告；模型只需给出截图像素坐标，换算到屏幕物理像素由服务端凭截图 ID 完成。

## 动作日志

每次工具调用在 `%LOCALAPPDATA%\computer-use\actions.jsonl` 追加一行 JSON：

| 字段 | 含义 |
| --- | --- |
| `time` | 调用时刻（UTC，ISO 8601） |
| `tool` / `target` | 工具名与目标（窗口句柄、截图 ID、坐标或路径） |
| `intent` | 模型自报的意图；只读工具为 `null` |
| `verdict` | `allowed` 或 `intercepted` |
| `outcome` | `succeeded`、`failed` 或 `not_executed`（被拦截） |
| `detail` | 拦截理由或错误信息 |
| `evidence` | 留证截图的路径 |

被拦截或失败的调用额外把目标窗口当时的样子存进 `evidence\`；成功的调用不留截图。目标窗口已经关掉、或本就不可操作（不可见、最小化、无标题）时不留证，`evidence` 为 `null`。

## 环境

Windows 10/11，Python 3.12+，用 [uv](https://docs.astral.sh/uv/) 管依赖：

```powershell
uv sync
```

## 在 Claude Code 中注册

```powershell
claude mcp add computer-use -- uv run --directory E:\ai-projects\computer-use computer-use
```

等价的 `.mcp.json` 写法：

```json
{
  "mcpServers": {
    "computer-use": {
      "command": "uv",
      "args": ["run", "--directory", "E:\\ai-projects\\computer-use", "computer-use"]
    }
  }
}
```

注册后在会话里问「当前打开了哪些窗口」即可。服务以普通用户权限运行，不要用管理员身份启动。

## 测试

```powershell
uv run pytest                    # 默认套件，不需要真实桌面
uv run pytest -m realdesktop     # 真实桌面验收，会启动记事本
uv run mypy                      # 类型检查
```

默认套件在 `FakeDesktop` 上跑，因此无头环境也能通过。`realdesktop` 标记的用例需要一个真实的桌面会话，会拉起记事本并在结束后关掉它。

## 结构

```
src/computer_use/
├── desktop.py         # DesktopPort：核心与 Windows 之间唯一的缝
├── windows.py         # 核心：哪些窗口可操作
├── observation.py     # 核心：观察、放大与截图 ID 的解析
├── action_log.py      # 核心：动作日志与留证
├── tools.py           # 工具层：整理成回传给模型的形状
├── win32_desktop.py   # DesktopPort 的 Windows 实现（唯一 import Win32 的地方）
└── server.py          # MCP 工具壳
tests/fake_desktop.py  # DesktopPort 的测试替身
```

所有与 Windows 打交道的调用（包括写日志与留证的磁盘读写）都收在 `DesktopPort` 之后：核心不 import 任何 Win32/UIA/截图库（Pillow 只用于裁剪、缩放与编码已采集的图像，不用于采集），新增平台能力时只在 `desktop.py` 加方法、在 `win32_desktop.py` 与 `fake_desktop.py` 各实现一次。
