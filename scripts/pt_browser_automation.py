#!/usr/bin/env python3
"""
PT 站点自动化浏览器脚本

使用 Playwright 同步 API 自动访问多个 PT 站点，检查登录状态并执行相关操作，最后通过 ntfy 发送报告。

功能说明:
- 使用指定的 user_data 目录启动浏览器实例（同步模式）。
- 访问 hdtime.org 并检查登录状态，登录时访问签到页面。
- 访问 haidan.video 并检查登录状态，登录时点击签到按钮。
- 访问 kp.m-team.cc/index 页面。
- 记录所有操作详情，并通过 ntfy 发送结果报告。

用法:
    python pt_browser_automation.py --user-data-dir /path/to/user_data
    python pt_browser_automation.py --user-data-dir /path/to/user_data --ntfy-url https://ntfy.chancel.me/signal

环境变量:
    PT_USER_DATA_DIR: 浏览器用户数据目录
    PT_NTFY_URL: ntfy 通知服务 URL，默认 https://ntfy.chancel.me/signal
    PT_LOG_LEVEL: 日志级别，默认 INFO
    PT_HEADLESS: 是否无头模式，默认 true
    PT_TIMEOUT_MS: 页面加载超时时间（毫秒），默认 30000

登录检测逻辑:
    - 检测页面中是否包含用户名 "chancel"，包含则视为已登录
    - 未登录时：无头模式直接失败；有头模式等待 180 秒供手动登录（每 10 秒检测一次）

依赖: Python 3.12+, playwright
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PlaywrightTimeout

# ============================= Constants & Defaults =============================

DEFAULT_USER_DATA_DIR = "browser_data"
DEFAULT_NTFY_URL = "https://ntfy.chancel.me/signal"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_HEADLESS = True
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_BROWSER_TYPE = "chromium"
LOGIN_CHECK_KEYWORD = "chancel"
LOGIN_WAIT_TIMEOUT_SEC = 180
LOGIN_CHECK_INTERVAL_SEC = 10

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

HDTIME_URL = "https://hdtime.org"
HDTIME_ATTENDANCE_URL = "https://hdtime.org/attendance.php"
HAIDAN_URL = "https://www.haidan.video"
MTEAM_URL = "https://kp.m-team.cc/index"

HAIDAN_BUTTON_ID = "modalBtn"


# ================================ Data Models ==================================

@dataclass(slots=True)
class Config:
    """Application configuration container."""
    user_data_dir: Path
    ntfy_url: str
    log_level: str
    headless: bool
    timeout_ms: int
    browser_type: str


@dataclass(slots=True)
class SiteResult:
    """Single site visit result."""
    site_name: str
    url: str
    success: bool
    logged_in: bool | None
    message: str
    error: str | None = None


# ============================= Configuration Setup ==============================

def build_parser() -> argparse.ArgumentParser:
    """Build command line argument parser."""
    parser = argparse.ArgumentParser(
        description="PT 站点自动化浏览器脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --user-data-dir /path/to/user_data
  %(prog)s --user-data-dir /path/to/user_data --ntfy-url https://ntfy.chancel.me/signal
  %(prog)s --user-data-dir /path/to/user_data --headed
        """
    )
    parser.add_argument("--user-data-dir", help="浏览器用户数据目录路径")
    parser.add_argument("--ntfy-url", help=f"ntfy 通知服务 URL (默认: {DEFAULT_NTFY_URL})")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help=f"日志级别 (默认: {DEFAULT_LOG_LEVEL})")
    parser.add_argument("--headed", action="store_true",
                       help="使用有头模式 (默认: 无头模式)")
    parser.add_argument("--timeout", type=int,
                       help=f"页面加载超时时间（毫秒）(默认: {DEFAULT_TIMEOUT_MS})")
    parser.add_argument("--browser-type", choices=["chromium", "firefox", "webkit"],
                       help=f"浏览器类型 (默认: {DEFAULT_BROWSER_TYPE})")
    return parser


def resolve_config(args: argparse.Namespace) -> Config:
    """Resolve configuration from arguments and environment variables."""
    user_data_dir_str = args.user_data_dir or os.getenv("PT_USER_DATA_DIR")
    if not user_data_dir_str:
        raise ValueError(
            "必须指定浏览器用户数据目录。使用 --user-data-dir 参数或设置 PT_USER_DATA_DIR 环境变量。"
        )
    
    user_data_dir = Path(user_data_dir_str).expanduser().resolve()
    
    ntfy_url = args.ntfy_url or os.getenv("PT_NTFY_URL", DEFAULT_NTFY_URL)
    log_level = (args.log_level or os.getenv("PT_LOG_LEVEL", DEFAULT_LOG_LEVEL)).upper()
    
    # Parse headless flag - default is True (headless), --headed makes it False
    headless = not args.headed
    if args.headed:
        # Explicit --headed overrides environment
        headless = False
    elif env_headless := os.getenv("PT_HEADLESS"):
        # Environment variable can override default
        headless = env_headless.lower() in ("true", "1", "yes")
    
    timeout_ms = args.timeout or int(os.getenv("PT_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))
    browser_type = args.browser_type or os.getenv("PT_BROWSER_TYPE", DEFAULT_BROWSER_TYPE)
    
    return Config(
        user_data_dir=user_data_dir,
        ntfy_url=ntfy_url,
        log_level=log_level,
        headless=headless,
        timeout_ms=timeout_ms,
        browser_type=browser_type,
    )


def configure_logging(log_level: str) -> None:
    """Configure application logging."""
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
    )


# ============================= Browser Operations ===============================

def check_login_status(page: Page) -> bool:
    """Check if user is logged in by looking for username keyword."""
    try:
        page_content = page.content()
        return LOGIN_CHECK_KEYWORD in page_content
    except Exception as exc:
        logging.warning("检查登录状态时出错: %s", exc)
        return False


def wait_for_manual_login(page: Page, site_name: str, headless: bool) -> bool:
    """Wait for manual login if in headed mode."""
    if headless:
        logging.warning("%s: 无头模式下未登录，跳过", site_name)
        return False
    
    logging.info("%s: 未登录，等待手动登录 (最多 %d 秒)...", site_name, LOGIN_WAIT_TIMEOUT_SEC)
    
    import time
    elapsed = 0
    
    while elapsed < LOGIN_WAIT_TIMEOUT_SEC:
        time.sleep(LOGIN_CHECK_INTERVAL_SEC)
        elapsed += LOGIN_CHECK_INTERVAL_SEC
        
        if check_login_status(page):
            logging.info("%s: 检测到已登录", site_name)
            return True
        
        logging.debug("%s: 仍未登录，已等待 %d 秒", site_name, elapsed)
    
    logging.warning("%s: 等待登录超时", site_name)
    return False


def launch_browser(config: Config, playwright) -> Browser:
    """Launch browser with user data directory."""
    logging.info("启动浏览器 (类型=%s, headless=%s, user_data=%s)",
                 config.browser_type, config.headless, config.user_data_dir)
    
    # Ensure user data directory exists
    config.user_data_dir.mkdir(parents=True, exist_ok=True)
    
    # Get browser type
    browser_launcher = getattr(playwright, config.browser_type)
    
    # Launch persistent context with user data
    context = browser_launcher.launch_persistent_context(
        user_data_dir=str(config.user_data_dir),
        headless=config.headless,
        args=[
            '--disable-blink-features=AutomationControlled',
        ],
    )
    
    logging.info("浏览器启动成功")
    return context


def visit_hdtime(page: Page, timeout_ms: int, headless: bool) -> SiteResult:
    """Visit hdtime.org and check login status."""
    site_name = "HDTime"
    try:
        logging.info("访问 %s...", HDTIME_URL)
        page.goto(HDTIME_URL, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        
        # Check if logged in
        logged_in = check_login_status(page)
        
        if not logged_in:
            logging.info("%s: 未登录", site_name)
            # Wait for manual login if in headed mode
            logged_in = wait_for_manual_login(page, site_name, headless)
            
            if not logged_in:
                return SiteResult(
                    site_name=site_name,
                    url=HDTIME_URL,
                    success=False if headless else True,
                    logged_in=False,
                    message="未登录" if headless else "等待登录超时",
                    error="无头模式下无法登录" if headless else None,
                )
        
        # Visit attendance page
        logging.info("%s: 已登录，访问签到页面...", site_name)
        page.goto(HDTIME_ATTENDANCE_URL, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        
        logging.info("%s: 签到页面访问成功", site_name)
        return SiteResult(
            site_name=site_name,
            url=HDTIME_ATTENDANCE_URL,
            success=True,
            logged_in=True,
            message="已登录，签到页面访问成功",
        )
        
    except PlaywrightTimeout as exc:
        error_msg = f"页面加载超时: {exc}"
        logging.error("%s: %s", site_name, error_msg)
        return SiteResult(
            site_name=site_name,
            url=HDTIME_URL,
            success=False,
            logged_in=None,
            message="访问失败",
            error=error_msg,
        )
    except Exception as exc:
        error_msg = f"访问出错: {exc}"
        logging.error("%s: %s", site_name, error_msg)
        return SiteResult(
            site_name=site_name,
            url=HDTIME_URL,
            success=False,
            logged_in=None,
            message="访问失败",
            error=error_msg,
        )


def visit_haidan(page: Page, timeout_ms: int, headless: bool) -> SiteResult:
    """Visit haidan.video and check login status."""
    site_name = "海胆"
    try:
        logging.info("访问 %s...", HAIDAN_URL)
        page.goto(HAIDAN_URL, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        
        # Check if logged in
        logged_in = check_login_status(page)
        
        if not logged_in:
            logging.info("%s: 未登录", site_name)
            # Wait for manual login if in headed mode
            logged_in = wait_for_manual_login(page, site_name, headless)
            
            if not logged_in:
                return SiteResult(
                    site_name=site_name,
                    url=HAIDAN_URL,
                    success=False if headless else True,
                    logged_in=False,
                    message="未登录" if headless else "等待登录超时",
                    error="无头模式下无法登录" if headless else None,
                )
        
        # Click modalBtn
        logging.info("%s: 已登录，点击签到按钮...", site_name)
        try:
            button = page.locator(f"#{HAIDAN_BUTTON_ID}")
            button.click(timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            logging.info("%s: 签到按钮点击成功", site_name)
            message = "已登录，签到按钮点击成功"
        except Exception as btn_exc:
            logging.warning("%s: 签到按钮点击失败: %s", site_name, btn_exc)
            message = f"已登录，但签到按钮点击失败: {btn_exc}"
        
        return SiteResult(
            site_name=site_name,
            url=HAIDAN_URL,
            success=True,
            logged_in=True,
            message=message,
        )
        
    except PlaywrightTimeout as exc:
        error_msg = f"页面加载超时: {exc}"
        logging.error("%s: %s", site_name, error_msg)
        return SiteResult(
            site_name=site_name,
            url=HAIDAN_URL,
            success=False,
            logged_in=None,
            message="访问失败",
            error=error_msg,
        )
    except Exception as exc:
        error_msg = f"访问出错: {exc}"
        logging.error("%s: %s", site_name, error_msg)
        return SiteResult(
            site_name=site_name,
            url=HAIDAN_URL,
            success=False,
            logged_in=None,
            message="访问失败",
            error=error_msg,
        )


def visit_mteam(page: Page, timeout_ms: int, headless: bool) -> SiteResult:
    """Visit kp.m-team.cc/index."""
    site_name = "M-Team"
    try:
        logging.info("访问 %s...", MTEAM_URL)
        page.goto(MTEAM_URL, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        
        # Check if logged in
        logged_in = check_login_status(page)
        
        if not logged_in:
            logging.info("%s: 未登录", site_name)
            # Wait for manual login if in headed mode
            logged_in = wait_for_manual_login(page, site_name, headless)
            
            if not logged_in:
                return SiteResult(
                    site_name=site_name,
                    url=MTEAM_URL,
                    success=False if headless else True,
                    logged_in=False,
                    message="未登录" if headless else "等待登录超时",
                    error="无头模式下无法登录" if headless else None,
                )
        
        logging.info("%s: 已登录，页面加载成功", site_name)
        return SiteResult(
            site_name=site_name,
            url=MTEAM_URL,
            success=True,
            logged_in=True,
            message="已登录，页面加载成功",
        )
        
    except PlaywrightTimeout as exc:
        error_msg = f"页面加载超时: {exc}"
        logging.error("%s: %s", site_name, error_msg)
        return SiteResult(
            site_name=site_name,
            url=MTEAM_URL,
            success=False,
            logged_in=None,
            message="访问失败",
            error=error_msg,
        )
    except Exception as exc:
        error_msg = f"访问出错: {exc}"
        logging.error("%s: %s", site_name, error_msg)
        return SiteResult(
            site_name=site_name,
            url=MTEAM_URL,
            success=False,
            logged_in=None,
            message="访问失败",
            error=error_msg,
        )


# ============================== Report Generation ===============================

def format_report(results: list[SiteResult]) -> str:
    """Format results into a report message."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    report_lines = [
        "🤖 PT 站点自动化报告",
        f"⏰ 时间: {timestamp}",
        "",
    ]
    
    success_count = sum(1 for r in results if r.success)
    total_count = len(results)
    
    for result in results:
        status_icon = "✅" if result.success else "❌"
        login_status = ""
        if result.logged_in is not None:
            login_status = " (已登录)" if result.logged_in else " (未登录)"
        
        report_lines.append(f"{status_icon} {result.site_name}{login_status}")
        report_lines.append(f"   {result.message}")
        if result.error:
            report_lines.append(f"   错误: {result.error}")
        report_lines.append("")
    
    report_lines.append(f"📊 总结: {success_count}/{total_count} 站点访问成功")
    
    return "\n".join(report_lines)


def send_ntfy_notification(ntfy_url: str, message: str) -> bool:
    """Send notification to ntfy service."""
    try:
        logging.info("发送报告到 ntfy: %s", ntfy_url)
        response = httpx.post(
            ntfy_url,
            content=message.encode("utf-8"),
            headers={
                "Priority": "default",
                "Tags": "robot,pt",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10.0,
            
        )
        response.raise_for_status()
        logging.info("报告发送成功")
        return True
    except Exception as exc:
        logging.error("报告发送失败: %s", exc)
        return False


# ================================= Main Logic ==================================

def run_automation(config: Config) -> int:
    """Run browser automation workflow."""
    results: list[SiteResult] = []
    
    with sync_playwright() as playwright:
        context = None
        try:
            # Launch browser
            context = launch_browser(config, playwright)
            
            # Get or create page
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()
            
            # Visit sites
            results.append(visit_hdtime(page, config.timeout_ms, config.headless))
            results.append(visit_haidan(page, config.timeout_ms, config.headless))
            results.append(visit_mteam(page, config.timeout_ms, config.headless))
            
        except Exception as exc:
            logging.exception("浏览器自动化执行失败: %s", exc)
            return 1
        finally:
            if context:
                logging.info("关闭浏览器...")
                context.close()
    
    # Generate and send report
    report = format_report(results)
    logging.info("\n" + "=" * 60 + "\n%s\n" + "=" * 60, report)
    
    send_ntfy_notification(config.ntfy_url, report)
    
    # Return exit code based on success
    all_success = all(r.success for r in results)
    return 0 if all_success else 1


def main(argv: list[str] | None = None) -> int:
    """Main application entry point."""
    parser = build_parser()
    args = parser.parse_args(argv or sys.argv[1:])
    
    try:
        config = resolve_config(args)
    except ValueError as exc:
        logging.error("配置错误: %s", exc)
        return 2
    
    configure_logging(config.log_level)
    
    try:
        return run_automation(config)
    except KeyboardInterrupt:
        logging.info("🛑 收到中断信号，退出...")
        return 130
    except Exception as exc:
        logging.exception("💥 程序执行错误: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
