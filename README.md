# OpenClaw 桌面控制台（Windows）

这是一个面向本地部署 `openclaw` 的桌面 GUI，核心目标是：

- 用中英双语界面完成部署参数配置；
- 内置环境检测门禁（检测全部通过才允许安装）；
- 未安装用户可在软件中一键安装 OpenClaw 并查看进度；
- 内置一键修复环境（Node/npm/镜像源）；
- 内置一键初始化向导（`openclaw onboard --install-daemon`）；
- 内置一键打包 EXE（便于分发）；
- 在同一窗口直接与本地 OpenClaw 聊天；
- 支持一键检测连接、日志落盘、会话清空；
- 保持方案轻量，尽量减少你的部署复杂度。

## 为什么选这个桌面方案

本项目使用 **Python + PySide6**：

- 对 Windows 11 兼容好，启动简单，不需要额外 Rust/Node 桌面壳；
- UI 可做专业化样式，便于后续继续升级；
- 能直接调用本地 `openclaw` 命令行，复用官方稳定入口（`openclaw agent`）。

对于你的诉求（“配置 + 本地聊天 + 专业页面 + 低部署负担”），这是当前性价比最高的桌面方案。

## 对 OpenClaw 最新版的关键分析（与你的 GUI 相关）

基于 `openclaw/openclaw` 主仓库 `README`（main）：

- 运行时要求：`Node >= 22`；
- 推荐安装：`openclaw onboard --install-daemon`；
- 推荐入口：`openclaw gateway` + `openclaw agent --message ...`；
- Windows 官方建议：通过 **WSL2** 使用（稳定性更高）；
- 命令行是最稳定、最不容易受内部协议变更影响的集成点。

因此 GUI 采用“**命令行桥接模式**”：

1. GUI 保存配置；
2. GUI 调用 `openclaw --version` / `openclaw agent`；
3. 将返回结果渲染到聊天区并记录日志。

这种方案不会绑定内部私有协议，后续 OpenClaw 升级时适配成本更低。

## 目录结构

```text
openclaw GUI/
├─ app.py
├─ requirements.txt
├─ README.md
└─ data/
   ├─ config.json          # 首次运行自动创建
   └─ logs/                # 按天写入日志
```

## 功能清单（当前版本）

- 界面文案双语：
  - 按钮、标签、提示框、状态、流程提示均为 `中文 / English`。
  - 支持语言模式切换：`中文`、`English`、`中文 / English`（持久化保存）。
- 交互布局升级：
  - 采用 Tab 分页：`引导与安装`、`会话聊天`、`运行日志`；
  - 在安装页提供步骤引导，按流程逐步完成配置与部署。
  - 关键操作按钮集中在顶部顺序排列，减少分散点击。
  - 启动后自动执行环境检测并给出“下一步建议”，推荐按钮高亮显示。
- 环境检测（安装门禁）：
  - Node 版本检测（必须 `>=22`）；
  - npm 可用性检测；
  - 安装源连通性检测（默认 `https://registry.npmmirror.com`）；
  - OpenClaw 已安装状态检测；
  - 综合结论不通过时，“一键安装 OpenClaw”按钮保持禁用。
- 一键安装：
  - 自动设置 npm registry 为镜像源；
  - 执行 `npm install -g openclaw@latest --registry ...`；
  - 安装过程实时日志与进度条；
  - 安装后自动校验 `openclaw --version`，并刷新环境检测结果。
- 一键修复环境：
  - Node 不满足要求时，尝试用 `winget` 自动安装 Node LTS；
  - 自动修复 npm registry；
  - 执行 npm 缓存校验；
  - 修复后自动复检环境。
- 一键初始化向导：
  - 点击后在 GUI 内打开“图形化初始化向导”窗口；
  - 向导窗口内直接执行 `openclaw onboard --install-daemon`；
  - 终端输出与输入都在 GUI 内完成，无需切换外部命令行窗口。
- 一键打包 EXE：
  - 自动安装/更新 `PyInstaller`（使用阿里云镜像）；
  - 自动打包当前 GUI 为 `dist/OpenClawGUI.exe`。
- 会话能力：
  - 检测连接（版本 + 测试消息）；
  - 发送消息与会话列表展示；
  - 日志按天落盘。

## 安装与运行（Windows PowerShell）

> 按你的规则，Python 包安装使用阿里云镜像。

```powershell
cd "D:\openclaw GUI"

python -m venv .venv
.\.venv\Scripts\activate

python -m pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/
python -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

python .\app.py
```

## 首次使用建议

1. 在 GUI 中设置：
   - OpenClaw 命令：`openclaw`（或你的绝对路径）
   - Node 命令：`node`
   - npm 命令：`npm`
   - winget 命令：`winget`
   - npm 安装源：默认即可（`https://registry.npmmirror.com`）
   - 工作目录：可留空
   - 思考等级：`medium`（默认）
2. 点击“环境检测”；
3. 若检测不通过，优先点击“一键修复环境”；
4. 全部通过后点击“一键安装 OpenClaw”（已安装也可用于升级）；
5. 安装后点击“一键初始化向导”完成官方初始化流程；
6. 点击“检测连接”确认版本与回包；
7. 在右侧聊天区域直接对话。

## 可继续扩展的能力

- 会话管理（多会话标签、历史搜索）；
- 更细粒度模型参数面板（模型、工具开关、输出模式）；
- WebSocket 直连网关模式（在你确认要绑定网关协议后再做）；
- 打包为 `.exe`（如用 `pyinstaller`）。
