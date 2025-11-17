#!/usr/bin/env python3
"""
PT 站点自动化浏览器脚本

使用 Playwright 同步 API 自动访问多个 PT 站点，检查登录状态并执行相关操作，最后通过 ntfy 发送报告。

功能说明:
- 使用指定的 state.json 文件启动浏览器实例（同步模式）。
- 访问 hdtime.org 并检查登录状态，登录时访问签到页面。
- 访问 haidan.video 并检查登录状态，登录时点击签到按钮。
- 访问 kp.m-team.cc/index 页面。
- 访问 v2ex.com 并领取每日铜币。
- 记录所有操作详情，并通过 ntfy 发送结果报告。
- 支持守护进程模式，定时执行签到任务。

用法:
    python pt_browser_automation.py --headed  # 有头模式，登录并保存状态
    python pt_browser_automation.py  # 无头模式，执行一次签到
    python pt_browser_automation.py --daemon  # 守护进程模式，定时执行签到

环境变量:
    PT_STATE_FILE: 浏览器状态文件路径，默认 .state.json
    PT_USER_DATA_DIR: 浏览器用户数据目录（仅有头模式），默认 .browser_data
    PT_NTFY_URL: ntfy 通知服务 URL，默认 https://ntfy.sh/signal
    PT_LOG_LEVEL: 日志级别，默认 INFO
    PT_HEADLESS: 是否无头模式，默认 true
    PT_TIMEOUT_MS: 页面加载超时时间（毫秒），默认 30000

守护进程模式:
    - 启动时立即执行一次签到
    - 记录首次执行时间，之后每天在相同时刻执行
    - 使用 schedule 库进行任务调度

依赖: Python 3.12+, playwright, httpx, schedule
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import schedule
from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PlaywrightTimeout

# ============================= Constants & Defaults =============================

DEFAULT_STATE_FILE = ".state.json"
DEFAULT_USER_DATA_DIR = ".browser_data"
DEFAULT_NTFY_URL = "https://ntfy.sh/signal"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_HEADLESS = True
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_BROWSER_TYPE = "chromium"
LOGIN_WAIT_TIMEOUT_SEC = 180
LOGIN_CHECK_INTERVAL_SEC = 10

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 站点配置：统一数据结构
SITES = [
    {
        "name": "HDTime",
        "url": "https://hdtime.org",
        "username": "chancel",
        "action": "visit_attendance",
        "attendance_url": "https://hdtime.org/attendance.php",
    },
    {
        "name": "海胆",
        "url": "https://www.haidan.video",
        "username": "chancel",
        "action": "click_button",
        "button_id": "modalBtn",
        "checked_text": "已经打卡",
    },
    {
        "name": "M-Team",
        "url": "https://kp.m-team.cc/index",
        "username": "chancel",
        "action": "visit_only",
    },
    {
        "name": "V2EX",
        "url": "https://www.v2ex.com",
        "username": "Chancel",
        "action": "v2ex_daily_mission",
        "mission_url": "https://www.v2ex.com/mission/daily",
    },
]


# ================================ Data Models ==================================

@dataclass(slots=True)
class Config:
    """Application configuration container."""
    state_file: Path
    ntfy_url: str
    log_level: str
    headless: bool
    timeout_ms: int
    browser_type: str
    daemon: bool


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
  %(prog)s --headed  # 有头模式，用于首次登录并保存状态
  %(prog)s  # 无头模式，使用保存的状态执行一次自动化
  %(prog)s --daemon  # 守护进程模式，定时执行签到
  %(prog)s --state-file /path/to/state.json
  %(prog)s --ntfy-url https://ntfy.sh/signal
        """
    )
    parser.add_argument("--state-file", help=f"浏览器状态文件路径 (默认: {DEFAULT_STATE_FILE})")
    parser.add_argument("--ntfy-url", help=f"ntfy 通知服务 URL (默认: {DEFAULT_NTFY_URL})")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help=f"日志级别 (默认: {DEFAULT_LOG_LEVEL})")
    parser.add_argument("--headed", action="store_true",
                       help="使用有头模式，用于登录并保存状态 (默认: 无头模式)")
    parser.add_argument("--daemon", action="store_true",
                       help="守护进程模式，定时执行签到任务")
    parser.add_argument("--timeout", type=int,
                       help=f"页面加载超时时间（毫秒）(默认: {DEFAULT_TIMEOUT_MS})")
    parser.add_argument("--browser-type", choices=["chromium", "firefox", "webkit"],
                       help=f"浏览器类型 (默认: {DEFAULT_BROWSER_TYPE})")
    return parser


def resolve_config(args: argparse.Namespace) -> Config:
    """Resolve configuration from arguments and environment variables."""
    state_file_str = args.state_file or os.getenv("PT_STATE_FILE", DEFAULT_STATE_FILE)
    state_file = Path(state_file_str).expanduser().resolve()
    
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
    daemon = args.daemon
    
    return Config(
        state_file=state_file,
        ntfy_url=ntfy_url,
        log_level=log_level,
        headless=headless,
        timeout_ms=timeout_ms,
        browser_type=browser_type,
        daemon=daemon,
    )


def configure_logging(log_level: str) -> None:
    """Configure application logging."""
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
    )


# ============================= Browser Operations ===============================

def check_login_status(page: Page, username: str) -> bool:
    """Check if user is logged in by looking for username keyword."""
    try:
        page_content = page.content()
        return username in page_content
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


def launch_browser_and_context(config: Config, playwright):
    """Launch browser and create context with optional storage state.

    - Headed: use persistent user_data_dir (from PT_USER_DATA_DIR or DEFAULT_USER_DATA_DIR)
    - Headless: launch browser + new_context and load storage_state from state_file if present
    """
    logging.info("启动浏览器 (类型=%s, headless=%s)", config.browser_type, config.headless)

    browser_launcher = getattr(playwright, config.browser_type)

    # Headed: use persistent context with user data dir to allow manual login/export
    if not config.headless:
        user_data_dir_str = os.getenv("PT_USER_DATA_DIR", DEFAULT_USER_DATA_DIR)
        user_data_dir = Path(user_data_dir_str).expanduser().resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        logging.info("使用持久用户数据目录启动浏览器: %s", user_data_dir)

        context = browser_launcher.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=config.headless,
            args=['--disable-blink-features=AutomationControlled'],
        )
        logging.info("浏览器（持久上下文）启动成功")
        return None, context

    # Headless: regular browser + context, optionally load storage_state
    browser = browser_launcher.launch(
        headless=config.headless,
        args=['--disable-blink-features=AutomationControlled'],
    )

    context_options = {}
    if config.headless and config.state_file.exists():
        logging.info("加载浏览器状态: %s", config.state_file)
        context_options["storage_state"] = str(config.state_file)

    context = browser.new_context(**context_options)
    logging.info("浏览器启动成功")
    return browser, context


def run_login_mode(context, config: Config) -> int:
    """Run in headed mode: wait for user to login and save state."""
    logging.info("=" * 60)
    logging.info("🌐 有头模式：请在浏览器中登录所有需要的网站")
    logging.info("=" * 60)
    logging.info("")
    logging.info("建议访问以下网站并登录：")
    for site in SITES:
        logging.info("  - %s", site["url"])
    logging.info("")
    logging.info("登录完成后，请按回车键继续...")
    logging.info("=" * 60)
    
    # Open first site
    page = context.new_page()
    page.goto(SITES[0]["url"], wait_until="domcontentloaded")
    
    # Wait for user input
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        logging.info("收到中断信号")
        return 130
    
    # Save storage state
    logging.info("保存浏览器状态到: %s", config.state_file)
    try:
        # Ensure parent directory exists
        config.state_file.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(config.state_file))
        logging.info("✅ 状态保存成功！")
        logging.info("")
        logging.info("现在你可以使用无头模式运行脚本：")
        logging.info("  python %s", Path(__file__).name)
        return 0
    except Exception as exc:
        logging.error("❌ 保存状态失败: %s", exc)
        return 1


def run_automation_mode(context, config: Config) -> list[SiteResult]:
    """Run in headless mode: execute automation with saved state."""
    results: list[SiteResult] = []
    
    try:
        # Get or create page
        if context.pages:
            page = context.pages[0]
        else:
            page = context.new_page()
        
        # Visit each site
        for site in SITES:
            result = visit_site(page, site, config.timeout_ms)
            results.append(result)
        
    except Exception as exc:
        logging.exception("浏览器自动化执行失败: %s", exc)
    
    return results


def visit_site(page: Page, site: dict, timeout_ms: int) -> SiteResult:
    """通用站点访问函数，根据站点配置执行相应操作。"""
    site_name = site["name"]
    site_url = site["url"]
    username = site["username"]
    action = site["action"]
    
    try:
        logging.info("访问 %s (%s)...", site_name, site_url)
        page.goto(site_url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        
        time.sleep(10) # 等待额外加载
        
        # Check if logged in
        logged_in = check_login_status(page, username)
        
        if not logged_in:
            logging.warning("%s: 未登录，状态文件可能已过期", site_name)
            return SiteResult(
                site_name=site_name,
                url=site_url,
                success=False,
                logged_in=False,
                message="未登录，请重新运行 --headed 模式更新状态",
                error="登录状态已失效",
            )
        
        # Execute site-specific action
        if action == "visit_attendance":
            return _handle_attendance(page, site, timeout_ms)
        elif action == "click_button":
            return _handle_button_click(page, site, timeout_ms)
        elif action == "v2ex_daily_mission":
            return _handle_v2ex_daily_mission(page, site, timeout_ms)
        elif action == "visit_only":
            logging.info("%s: 已登录，页面加载成功", site_name)
            return SiteResult(
                site_name=site_name,
                url=site_url,
                success=True,
                logged_in=True,
                message="已登录，页面加载成功",
            )
        else:
            logging.warning("%s: 未知操作类型: %s", site_name, action)
            return SiteResult(
                site_name=site_name,
                url=site_url,
                success=True,
                logged_in=True,
                message=f"已登录，但未知操作类型: {action}",
            )
        
    except PlaywrightTimeout as exc:
        error_msg = f"页面加载超时: {exc}"
        logging.error("%s: %s", site_name, error_msg)
        return SiteResult(
            site_name=site_name,
            url=site_url,
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
            url=site_url,
            success=False,
            logged_in=None,
            message="访问失败",
            error=error_msg,
        )


def _handle_attendance(page: Page, site: dict, timeout_ms: int) -> SiteResult:
    """处理签到页面访问（如 HDTime）。"""
    site_name = site["name"]
    attendance_url = site.get("attendance_url")
    
    if not attendance_url:
        return SiteResult(
            site_name=site_name,
            url=site["url"],
            success=False,
            logged_in=True,
            message="配置错误：缺少 attendance_url",
            error="站点配置不完整",
        )
    
    logging.info("%s: 已登录，访问签到页面...", site_name)
    page.goto(attendance_url, timeout=timeout_ms, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=timeout_ms)
    
    logging.info("%s: 签到页面访问成功", site_name)
    return SiteResult(
        site_name=site_name,
        url=attendance_url,
        success=True,
        logged_in=True,
        message="已登录，签到页面访问成功",
    )


def _handle_button_click(page: Page, site: dict, timeout_ms: int) -> SiteResult:
    """处理按钮点击操作（如海胆）。"""
    site_name = site["name"]
    site_url = site["url"]
    button_id = site.get("button_id")
    checked_text = site.get("checked_text", "")
    
    if not button_id:
        return SiteResult(
            site_name=site_name,
            url=site_url,
            success=False,
            logged_in=True,
            message="配置错误：缺少 button_id",
            error="站点配置不完整",
        )
    
    # Try to find the button and check its text
    button = page.locator(f"#{button_id}")
    try:
        btn_count = button.count()
    except Exception:
        btn_count = 0
    
    if btn_count > 0:
        # Read button text safely
        btn_text = ""
        try:
            btn_text = button.get_attribute('value')
        except Exception:
            btn_text = ""
        
        if checked_text and btn_text == checked_text:
            logging.info("%s: 已登录，按钮文本显示已打卡 (%s)", site_name, btn_text)
            return SiteResult(
                site_name=site_name,
                url=site_url,
                success=True,
                logged_in=True,
                message="已登录，已经打卡（通过按钮文本检测）",
            )
        
        # Not already checked: try to click
        logging.info("%s: 已登录，尝试点击签到按钮 (文本: %s)...", site_name, btn_text)
        try:
            button.click(timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            logging.info("%s: 签到按钮点击成功", site_name)
            message = "已登录，签到按钮点击成功"
        except Exception as btn_exc:
            logging.warning("%s: 签到按钮点击失败: %s", site_name, btn_exc)
            message = f"已登录，但签到按钮点击失败: {btn_exc}"
    else:
        logging.warning("%s: 未找到签到按钮 (id=%s)", site_name, button_id)
        message = "已登录，但未找到签到按钮"
    
    return SiteResult(
        site_name=site_name,
        url=site_url,
        success=True,
        logged_in=True,
        message=message,
    )


def _handle_v2ex_daily_mission(page: Page, site: dict, timeout_ms: int) -> SiteResult:
    """处理 V2EX 每日任务领取铜币。"""
    site_name = site["name"]
    mission_url = site.get("mission_url")
    
    if not mission_url:
        return SiteResult(
            site_name=site_name,
            url=site["url"],
            success=False,
            logged_in=True,
            message="配置错误：缺少 mission_url",
            error="站点配置不完整",
        )
    
    logging.info("%s: 已登录，访问每日任务页面...", site_name)
    page.goto(mission_url, timeout=timeout_ms, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=timeout_ms)
    
    # 查找领取铜币的按钮
    # 按钮特征：class="super normal button" 且 value 包含 "领取" 和 "铜币"
    button_selector = 'input.button[type="button"][value*="领取"][value*="铜币"]'
    
    try:
        button = page.locator(button_selector)
        btn_count = button.count()
    except Exception:
        btn_count = 0
    
    if btn_count > 0:
        # 获取按钮文本
        try:
            btn_value = button.get_attribute('value')
            logging.info("%s: 找到铜币按钮: %s", site_name, btn_value)
            
            # 检查是否已经领取过（按钮可能显示"明天再来"等）
            if "已领取" in btn_value or "明天" in btn_value:
                logging.info("%s: 今日已领取铜币", site_name)
                return SiteResult(
                    site_name=site_name,
                    url=mission_url,
                    success=True,
                    logged_in=True,
                    message=f"已登录，今日已领取铜币 ({btn_value})",
                )
            
            # 点击领取按钮
            logging.info("%s: 点击领取铜币按钮...", site_name)
            button.click(timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            
            logging.info("%s: 铜币领取成功", site_name)
            return SiteResult(
                site_name=site_name,
                url=mission_url,
                success=True,
                logged_in=True,
                message="已登录，铜币领取成功",
            )
            
        except Exception as btn_exc:
            logging.warning("%s: 铜币领取失败: %s", site_name, btn_exc)
            return SiteResult(
                site_name=site_name,
                url=mission_url,
                success=True,
                logged_in=True,
                message=f"已登录，但铜币领取失败: {btn_exc}",
            )
    else:
        # 未找到按钮，可能已经领取过
        page_content = page.content()
        if "每日登录奖励已领取" in page_content or "明天再来" in page_content:
            logging.info("%s: 检测到今日已领取铜币", site_name)
            return SiteResult(
                site_name=site_name,
                url=mission_url,
                success=True,
                logged_in=True,
                message="已登录，今日已领取铜币（页面检测）",
            )
        
        logging.warning("%s: 未找到铜币领取按钮", site_name)
        return SiteResult(
            site_name=site_name,
            url=mission_url,
            success=True,
            logged_in=True,
            message="已登录，但未找到铜币领取按钮",
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
    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            # Launch browser and context
            browser, context = launch_browser_and_context(config, playwright)
            
            # Run in appropriate mode
            if not config.headless:
                # Headed mode: wait for login and save state
                return run_login_mode(context, config)
            else:
                # Headless mode: run automation
                if not config.state_file.exists():
                    logging.error("❌ 状态文件不存在: %s", config.state_file)
                    logging.error("请先使用 --headed 模式运行脚本以保存登录状态")
                    return 2
                
                results = run_automation_mode(context, config)
                
                all_success = all(r.success for r in results)
                report = format_report(results)
                logging.info("\n" + "=" * 60 + "\n%s\n" + "=" * 60, report)
                
                if all_success:
                    logging.info("所有站点访问成功，跳过通知推送")
                else:
                    send_ntfy_notification(config.ntfy_url, report)
                
                return 0 if all_success else 1
            
        except Exception as exc:
            logging.exception("浏览器自动化执行失败: %s", exc)
            return 1
        finally:
            if context:
                logging.info("关闭浏览器上下文...")
                context.close()
            if browser:
                logging.info("关闭浏览器...")
                browser.close()


def run_scheduled_task(config: Config) -> None:
    """运行定时任务（守护进程模式）。"""
    logging.info("🔄 开始执行定时签到任务...")
    try:
        exit_code = run_automation(config)
        if exit_code == 0:
            logging.info("✅ 定时任务执行成功")
        else:
            logging.warning("⚠️ 定时任务执行完成，但存在错误 (exit_code=%d)", exit_code)
    except Exception as exc:
        logging.exception("❌ 定时任务执行失败: %s", exc)


def run_daemon_mode(config: Config) -> int:
    """运行守护进程模式，定时执行签到。"""
    logging.info("=" * 60)
    logging.info("🤖 守护进程模式启动")
    logging.info("=" * 60)
    
    # 立即执行一次
    logging.info("⚡ 首次执行签到任务...")
    first_run_time = datetime.now()
    run_scheduled_task(config)
    
    # 计算下次执行时间（明天的同一时刻）
    schedule_time = first_run_time.strftime("%H:%M")
    logging.info("")
    logging.info("📅 定时任务已设置：每天 %s 执行", schedule_time)
    logging.info("⏰ 下次执行时间：明天 %s", schedule_time)
    logging.info("💡 提示：按 Ctrl+C 停止守护进程")
    logging.info("=" * 60)
    
    # 设置每日定时任务
    schedule.every().day.at(schedule_time).do(run_scheduled_task, config)
    
    # 主循环
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次
    except KeyboardInterrupt:
        logging.info("")
        logging.info("🛑 收到停止信号，守护进程退出")
        return 0


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
        # 守护进程模式
        if config.daemon:
            if not config.headless:
                logging.error("守护进程模式不支持有头模式，请移除 --headed 参数")
                return 2
            return run_daemon_mode(config)
        
        # 普通模式（单次执行）
        return run_automation(config)
        
    except KeyboardInterrupt:
        logging.info("🛑 收到中断信号，退出...")
        return 130
    except Exception as exc:
        logging.exception("💥 程序执行错误: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
