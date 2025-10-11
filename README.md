# ai-scripts

本仓库是一系列日常常用的自动化 Python 脚本，由 ai 自动开发，并经过人工审核和测试，力求实用和可靠

每个脚本都可单独运行，单独使用，采用传参方式来控制，推荐使用 `uv` 直接运行，例如拉取仓库 Releases：

```bash
uv run --with httpx --with ruff gh_release_fetch.py --repo https://github.com/fatedier/frp --tags latest
```

## 参数优先级

所有脚本遵循统一的配置优先级：**命令行传参 > 环境变量 > 内置默认值**。缺省时会自动回退到下一级来源，确保在不同部署环境下都能顺利运行。

# 使用指南

## Github Releases 拉取

文件：**gh_release_fetch.py**

该脚本用于拉取指定 GitHub 仓库的发布版本资源，并进行哈希验证。

运行方式

```bash
uv run gh_release_fetch.py --repo https://github.com/fatedier/frp --tags latest
```

功能说明: 
- 支持指定多个 `--tags` 下载特定发布版本，支持 `latest` 标签表示最新版本
- 使用 `latest` 标签时自动检测最新版本是否有更新，有更新则重新下载
- 流式下载 + 双重 SHA256 校验，生成 `<文件名>.sha256`
- 维护 `release_manifest.json`：记录资产元数据与 hash（幂等追加）
- 支持 `GITHUB_TOKEN` (环境变量) 以提升速率限制

参数说明:

```
--repo          GitHub 仓库（owner/name 或完整 URL）。支持环境变量 GH_RELEASE_FETCH_REPO。
--tags          要下载的发布版本标签，支持多个；默认 latest。支持环境变量 GH_RELEASE_FETCH_TAGS（以空格或逗号分隔）。
--force-latest  强制重新下载最新版本（支持 --no-force-latest 关闭）。也可通过 GH_RELEASE_FETCH_FORCE_LATEST 控制，取值 1/0、true/false 等。
--download-dir  下载根目录，默认当前目录，可用 GH_RELEASE_FETCH_DOWNLOAD_DIR 覆盖。
--log-level     日志等级，默认 INFO，可用 GH_RELEASE_FETCH_LOG 控制。
```

所有参数均可通过命令行显式指定；缺省时依次尝试读取上述环境变量，再落入默认值（例如 `--tags` 默认为 `latest`）。

示例:
```
# 下载最新 release
python gh_release_fetch.py --repo psf/requests --tags latest

# 下载特定版本
python gh_release_fetch.py --repo psf/requests --tags v2.31.0 v2.30.0

# 混合下载最新版本和特定版本
python gh_release_fetch.py --repo psf/requests --tags latest v2.31.0 --download-dir ./downloads

# 强制重新下载最新版本
python gh_release_fetch.py --repo psf/requests --tags latest --force-latest

# 使用完整仓库 URL
python gh_release_fetch.py --repo https://github.com/psf/requests --tags latest
```

注意:
- 已存在且尺寸一致的文件跳过（除非 `--force-latest` 用于最新 release）。
- 校验失败会报错并跳过该资产，不会写入 manifest。
- 建议设置 `export GITHUB_TOKEN=xxxx` 以避免速率限制。

## Telegram 消息归档

文件：**scripts/telegram_message_archiver.py**

该脚本用于连接 Telegram 机器人或用户会话，发送上线问候，并归档所有接收到的消息与媒体附件到本地目录。

### 功能说明
- 支持 Bot 模式和用户模式切换。
- 下载媒体文件并生成安全文件名，支持超大文件（默认 2GB 上限，可配置）。
- 保存发送者信息、错误日志等元数据到每日 JSON 文件。
- 支持多 Chat ID 监控，可通过命令行或环境变量配置。
- 提供实时归档回执，支持自定义上线问候文本。

### 运行方式

```bash
uv run scripts/telegram_message_archiver.py --api-id <api-id> --api-hash <api-hash> --chat-id <chat-id>
```

或使用 Bot 模式：

```bash
uv run scripts/telegram_message_archiver.py --api-id <api-id> --api-hash <api-hash> --bot-token <bot-token>
```

### 参数说明

```
--api-id            Telegram API ID（必填），对应环境变量 TELEGRAM_API_ID。
--api-hash          Telegram API Hash（必填），对应环境变量 TELEGRAM_API_HASH。
--bot-token         Telegram 机器人 Token（可选），对应环境变量 TELEGRAM_BOT_TOKEN。
--chat-id           要监控的 Chat ID，可指定多个，支持环境变量 TELEGRAM_CHAT_ID（逗号分隔）。
--save-dir          消息与媒体保存目录，默认 telegram_messages，对应环境变量 TELEGRAM_SAVE_DIR。
--max-file-size-mb  最大文件大小（单位 MB），默认 2000（2GB），对应环境变量 TELEGRAM_MAX_FILE_SIZE_MB。
--log-level         日志等级，默认 INFO，对应环境变量 TELEGRAM_LOG_LEVEL。
--greeting          上线问候文本，默认中文提示，对应环境变量 TELEGRAM_GREETING。
```

### 存储与日志
- 消息将以 Chat ID 与时间戳分目录归档。
- 文本、描述信息保存为 UTF-8 文本，媒体按原格式下载。
- 每日生成 `.cache/YYYY-MM-DD.json` 汇总文件，记录消息元数据。
- 日志级别默认 INFO，可通过 `--log-level` 或 `TELEGRAM_LOG_LEVEL` 配置。

## 开发指南

本节介绍如何使用 VS Code 进行脚本开发和调试。

### 配置 VS Code 调试环境

VS Code 提供了强大的调试功能，以下是推荐的 `launch.json` 配置示例，适用于本项目中的脚本开发：

```jsonc
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python Debugger: gh_release_fetch",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/scripts/gh_release_fetch.py",
      "console": "internalConsole"
    },
    {
      "name": "Python Debugger: telegram_message_archiver",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/scripts/telegram_message_archiver.py",
      "console": "internalConsole",
      "env": {
        "TELEGRAM_BOT_TOKEN": "<your-bot-token>",
      },
      "args": [
        "--save-dir", "downloads",
        "--log-level", "INFO",
        "--bot-token", "<your-bot-token>",
        "--chat-id", "<your-chat-id>",
        "--max-file-size", "100",
        "--api-id", "<your-api-id>",
        "--api-hash", "<your-api-hash>"
      ]
    }
  ]
}
```

### 配置说明
- **TELEGRAM_BOT_TOKEN**: 替换为你的 Telegram Bot Token。
- **--chat-id**: 替换为目标 Chat ID。
- **--api-id 和 --api-hash**: 替换为你的 Telegram API ID 和 Hash。
- **--save-dir**: 指定保存目录，默认为 `downloads`。
- **--log-level**: 设置日志级别，默认为 `INFO`。
- **--max-file-size**: 设置最大文件大小（单位 MB），默认为 `100`。

### 使用方法
1. 打开 VS Code，确保已安装 Python 扩展。
2. 在 `.vscode/launch.json` 中添加上述配置。
3. 选择调试配置（如 `Python Debugger: telegram_message_archiver`）。
4. 点击调试按钮开始调试。

通过上述步骤，你可以方便地在 VS Code 中开发和调试本项目中的脚本。

