# ai-scripts

本仓库收集一系列日常常用的自动化脚本，全部由多个 ai 开发，并经过人工审核和测试，力求实用和可靠

每个脚本都可单独运行，单独使用，采用传参方式来控制，推荐使用 `uv` 直接运行，例如拉取仓库 Releases：

```bash
uv run --with httpx --with ruff gh_release_fetch.py --repo https://github.com/fatedier/frp --tags latest
```

# 使用指南

## Github Releases 拉取

文件：**gh_release_fetch.py**

该脚本用于拉取指定 GitHub 仓库的发布版本资源，并进行哈希验证。

运行方式

```bash
uv run --with httpx --with ruff gh_release_fetch.py --repo https://github.com/fatedier/frp --tags latest
```

功能说明: 
- 支持指定多个 `--tags` 下载特定发布版本，支持 `latest` 标签表示最新版本
- 使用 `latest` 标签时自动检测最新版本是否有更新，有更新则重新下载
- 流式下载 + 双重 SHA256 校验，生成 `<文件名>.sha256`
- 维护 `release_manifest.json`：记录资产元数据与 hash（幂等追加）
- 支持 `GITHUB_TOKEN` (环境变量) 以提升速率限制

参数说明:

```
--repo          必填，GitHub 仓库（owner/name 或完整 URL）
--tags          必填，要下载的发布版本标签，支持多个。使用 'latest' 表示最新版本。
--force-latest  强制重新下载 latest 版本，即使文件已存在且 latest 未更新。
--download-dir  下载根目录，默认当前目录。
```

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

