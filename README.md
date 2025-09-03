# ai-scripts
A collection of one-off, AI‑generated scripts for quick, practical automation tasks. / 一系列一次性、由AI生成的脚本，用于快速完成实用的自动化任务。

# 使用指南

推荐直接使用 uv 单次运行目标功能的 py 文件，库中的每一个 py 文件都为此做了适配

## Github Releases 自动拉取

文件：gh_release_fetch.py

运行方式

```bash
uv run --with httpx --with ruff gh_release_fetch.py --repo https://github.com/fatedier/frp --tags latest
```

# 详细功能说明
## gh_release_fetch.py
下载指定 GitHub 发布版本资源并进行哈希验证。

功能: 
- 支持指定多个 `--tags` 下载特定发布版本，支持 `latest` 标签表示最新版本
- 使用 `latest` 标签时自动检测最新版本是否有更新，有更新则重新下载
- 流式下载 + 双重 SHA256 校验，生成 `<文件名>.sha256`
- 维护 `release_manifest.json`：记录资产元数据与 hash（幂等追加）
- 支持 `GITHUB_TOKEN` (环境变量) 以提升速率限制

依赖安装 (使用 uv):
```
uv sync
```

基础用法:
```
python gh_release_fetch.py --repo owner/name --tags latest
```

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

输出结构 (示例):
```
downloads/
	release_manifest.json
	psf_requests/
		v2.32.3/
			requests.tar.gz
			requests.tar.gz.sha256
```

`release_manifest.json` 字段:
```
[
	{
		"repo": "psf/requests",
		"release_tag": "v2.32.3",
		"release_id": 123456,
		"asset_id": 987654,
		"asset_name": "requests.tar.gz",
		"size": 12345,
		"download_url": "https://github.com/...",
		"hash_algo": "sha256",
		"hash_value": "<sha256>",
		"path": "/abs/path/.../requests.tar.gz"
	}
]
```

注意:
- 已存在且尺寸一致的文件跳过（除非 `--force-latest` 用于最新 release）。
- 校验失败会报错并跳过该资产，不会写入 manifest。
- 建议设置 `export GITHUB_TOKEN=xxxx` 以避免速率限制。

