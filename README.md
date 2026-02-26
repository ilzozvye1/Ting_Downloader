# 有声小说下载器（ting13）

用于下载 `ting13.cc` 与 `ting22.com / huanting.cc` 的有声书，支持命令行和图形界面。

## 功能

- 支持整本下载与单集下载
- 支持指定集数范围下载
- 支持代理（手动或自动检测）
- 支持 Clash 自动换 IP
- 支持断点补集（跳过已下载，优先补缺失章节）

## 目录结构

- `ting13/cli.py`：命令行入口
- `ting13/gui.py`：通用 GUI（v3）
- `ting13/ting13_gui.py`：多任务 GUI（v4）
- `ting13/ting13_downloader.py`：旧版兼容入口

## 环境依赖

推荐 Python 3.10+，并安装以下依赖：

```bash
pip install playwright requests lxml cssselect customtkinter
playwright install chromium
```

## 快速开始

### 命令行

```bash
python ting13/cli.py "https://www.ting13.cc/youshengxiaoshuo/10408/"
```

指定输出目录和范围：

```bash
python ting13/cli.py -o "./downloads" --start 5 --end 10 "URL"
```

使用代理和自动换 IP：

```bash
python ting13/cli.py --proxy auto --rotate 30 "URL"
```

### 图形界面

```bash
python ting13/ting13_gui.py
```

## CLI 参数

- `url`：书籍页或播放页 URL
- `-o, --output`：输出目录（默认当前目录）
- `--start`：起始集（默认 1）
- `--end`：结束集（默认全部）
- `--no-headless`：显示浏览器窗口
- `--proxy`：代理地址，`auto` 为自动检测
- `--rotate`：每 N 集通过 Clash API 自动换 IP

## 使用说明

- 建议优先使用书籍详情页 URL，单集链接也可下载
- 首次运行前请确认 Playwright 浏览器已安装
- 如遇访问限制，建议开启代理并适当提高换 IP 频率

## 免责声明

本项目仅用于学习与研究，请遵守目标网站的服务条款与当地法律法规。请勿用于未授权内容下载。
