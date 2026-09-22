# computer-use

在 Claude Code CLI 中用自然语言操控本机 Windows 桌面的 MCP server。术语一律以 [`CONTEXT.md`](CONTEXT.md) 为准，设计决策见 [`docs/adr/`](docs/adr)。

当前只有一个只读工具：

| 工具 | 作用 |
| --- | --- |
| `list_windows` | 列出桌面上可操作的窗口，含标题、进程名与屏幕矩形 |

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
├── windows.py         # 核心：纯逻辑，只依赖 DesktopPort
├── tools.py           # 工具层：整理成回传给模型的形状
├── win32_desktop.py   # DesktopPort 的 Windows 实现（唯一 import Win32 的地方）
└── server.py          # MCP 工具壳
tests/fake_desktop.py  # DesktopPort 的测试替身
```

所有与 Windows 打交道的调用都收在 `DesktopPort` 之后：核心不 import 任何 Win32/UIA/截图库，新增平台能力时只在 `desktop.py` 加方法、在 `win32_desktop.py` 与 `fake_desktop.py` 各实现一次。
