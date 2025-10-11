#!/usr/bin/env python3
"""
Telegram Message Archiver

归档 Telethon 捕获的 Telegram 媒体消息到本地，可在 Bot/用户模式间切换，支持 chat 过滤、大小限制与每日 JSON 元数据。
实现概述:
- 下载媒体并生成安全文件名，支持超大文件。
- 保存发送者/错误等元数据到 save_dir/.cache/YYYY-MM-DD.json。
- 归档过程实时回执，环境变量与 CLI 任意组合配置。
用法:
    python telegram_message_archiver.py --api-id 123456 --api-hash <hash> [--bot-token <token> ...]
环境变量:
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TELEGRAM_SAVE_DIR, TELEGRAM_LOG_LEVEL, TELEGRAM_MAX_FILE_SIZE_MB
依赖: Python 3.8+, telethon
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import Message, User, MessageMediaPhoto, MessageMediaDocument

# Configuration constants
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
TELEGRAM_API_ID = "TELEGRAM_API_ID"
TELEGRAM_API_HASH = "TELEGRAM_API_HASH"
TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
TELEGRAM_SAVE_DIR = "telegram_messages"
TELEGRAM_GREETING = "🤖 Bot 已上线，开始归档所有消息。"
TELEGRAM_LOG_LEVEL = "INFO"
TELEGRAM_MAX_FILE_SIZE_MB = 2000  # 2GB default (Telethon handles large files)

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


@dataclass(slots=True)
class Config:
    """Application configuration container."""
    api_id: int
    api_hash: str
    bot_token: str | None = None
    chat_ids: list[str] | None = None
    save_dir: Path = Path(TELEGRAM_SAVE_DIR)
    log_level: str = TELEGRAM_LOG_LEVEL
    greeting: str = TELEGRAM_GREETING
    max_file_size_bytes: int = TELEGRAM_MAX_FILE_SIZE_MB * 1024 * 1024


# ============================= Configuration Setup ==============================

def build_parser() -> argparse.ArgumentParser:
    """Build command line argument parser."""
    parser = argparse.ArgumentParser(
        description="Archive Telegram messages with media to local storage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --api-id 12345 --api-hash abc123 --bot-token token123
  %(prog)s --api-id 12345 --api-hash abc123 --chat-id -1001234567890
        """
    )
    parser.add_argument("--api-id", type=int, help="Telegram API ID (required)")
    parser.add_argument("--api-hash", help="Telegram API Hash (required)")
    parser.add_argument("--bot-token", help="Bot token for bot mode (optional)")
    parser.add_argument("--chat-id", action="append", dest="chat_ids", 
                       help="Chat ID to monitor (can specify multiple)")
    parser.add_argument("--save-dir", help="Storage directory (default: telegram_messages)")
    parser.add_argument("--max-file-size-mb", type=int,
                       help=f"Max file size in MB (default: {TELEGRAM_MAX_FILE_SIZE_MB})")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="Logging level (default: INFO)")
    parser.add_argument("--greeting", help="Startup greeting message")
    return parser


def resolve_config(args: argparse.Namespace) -> Config:
    """Resolve configuration from arguments and environment variables."""
    # Required API credentials
    api_id = args.api_id or os.getenv(TELEGRAM_API_ID)
    api_hash = args.api_hash or os.getenv(TELEGRAM_API_HASH)
    
    if not api_id or not api_hash:
        raise ValueError(
            f"Missing API credentials. Set {TELEGRAM_API_ID} and {TELEGRAM_API_HASH} "
            "environment variables or use --api-id and --api-hash arguments."
        )
    
    try:
        api_id = int(api_id)
    except (ValueError, TypeError):
        raise ValueError("API ID must be a valid integer")

    # Optional configuration
    bot_token = args.bot_token or os.getenv(TELEGRAM_BOT_TOKEN)
    
    # Parse chat IDs from arguments or environment
    chat_ids = None
    if args.chat_ids:
        chat_ids = args.chat_ids
    elif env_chat_ids := os.getenv(TELEGRAM_CHAT_ID):
        chat_ids = [cid.strip() for cid in env_chat_ids.split(",") if cid.strip()]
    
    # Other settings with defaults
    log_level = (args.log_level or os.getenv(TELEGRAM_LOG_LEVEL) or "INFO").upper()
    greeting = args.greeting or os.getenv(TELEGRAM_GREETING) or TELEGRAM_GREETING
    save_dir = Path(args.save_dir or os.getenv(TELEGRAM_SAVE_DIR) or TELEGRAM_SAVE_DIR)
    
    # File size limit
    max_size_mb = args.max_file_size_mb or int(os.getenv("TELEGRAM_MAX_FILE_SIZE_MB", TELEGRAM_MAX_FILE_SIZE_MB))
    
    return Config(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        chat_ids=chat_ids,
        save_dir=save_dir.expanduser().resolve(),
        log_level=log_level,
        greeting=greeting,
        max_file_size_bytes=max_size_mb * 1024 * 1024
    )


def configure_logging(log_level: str) -> None:
    """Configure application logging."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
    
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root.setLevel(numeric_level)
    logging.getLogger("telethon").setLevel(logging.WARNING)


# =============================== Utility Functions =============================

def ensure_directory(path: Path) -> None:
    """Ensure directory exists, creating if necessary."""
    path.mkdir(parents=True, exist_ok=True)


def ensure_timezone(dt: datetime) -> datetime:
    """Ensure datetime has timezone info."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone()


def sanitize_filename(text: str, default: str, max_length: int = 100) -> str:
    """Convert text to safe filename, supporting Unicode characters."""
    if not text or not text.strip():
        return default
    
    # Remove filesystem-unsafe characters, keep Unicode
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', text.strip())
    sanitized = re.sub(r'\s+', '_', sanitized).strip('. ')
    
    if not sanitized:
        return default
    
    # Truncate if too long
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip('_. ')
    
    return sanitized or default


def format_file_size(size_bytes: int) -> str:
    """Format file size in human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != 'B' else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


# ============================= Media Handling Functions ==========================

def get_media_info(media) -> tuple[int, str]:
    """Extract file size and suggested filename from media object."""
    file_size = 0
    extension = ".bin"
    
    if hasattr(media, 'document') and media.document:
        file_size = getattr(media.document, 'size', 0)
        
        # Check for filename in document attributes
        if hasattr(media.document, 'attributes'):
            for attr in media.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    if '.' in attr.file_name:
                        extension = '.' + attr.file_name.split('.')[-1]
                    break
        
        # Fallback to MIME type
        if extension == ".bin" and hasattr(media.document, 'mime_type'):
            mime_map = {
                'image/jpeg': '.jpg', 'image/png': '.png', 'video/mp4': '.mp4',
                'audio/mpeg': '.mp3', 'audio/ogg': '.ogg', 'image/gif': '.gif'
            }
            extension = mime_map.get(media.document.mime_type, '.bin')
    
    elif hasattr(media, 'photo') and media.photo:
        extension = '.jpg'
        # For photos, estimate size from largest available size
        if hasattr(media.photo, 'sizes'):
            sizes = [getattr(size, 'size', 0) for size in media.photo.sizes if hasattr(size, 'size')]
            file_size = max(sizes) if sizes else 0
    
    return file_size, extension


async def download_media_file(client: TelegramClient, message: Message, 
                            destination: Path, max_size: int) -> tuple[bool, str]:
    """Download media file with size checking and error handling."""
    if not message.media:
        return False, "No media in message"
    
    try:
        file_size, extension = get_media_info(message.media)
        
        # Check size limit
        if file_size > max_size:
            return False, f"File size ({format_file_size(file_size)}) exceeds limit ({format_file_size(max_size)})"
        
        ensure_directory(destination.parent)
        
        # Handle filename conflicts
        counter = 1
        original_dest = destination
        while destination.exists():
            stem = original_dest.stem
            suffix = original_dest.suffix
            destination = original_dest.parent / f"{stem}_{counter}{suffix}"
            counter += 1
        
        logging.debug("Downloading %s to %s", format_file_size(file_size), destination.name)
        await client.download_media(message, file=str(destination))
        
        return True, f"Downloaded {destination.name} ({format_file_size(file_size)})"
        
    except Exception as exc:
        return False, f"Download failed: {exc}"


# ============================= Metadata Management ===========================

async def save_message_metadata(cache_dir: Path, message: Message, 
                               media_files: list[Path], errors: list[str]) -> None:
    """Save message metadata to daily JSON file."""
    date_str = ensure_timezone(message.date).strftime("%Y-%m-%d")
    metadata_file = cache_dir / f"{date_str}.json"
    
    # Extract sender information
    sender_info = {"id": None, "username": None, "first_name": None, "last_name": None}
    if message.sender and isinstance(message.sender, User):
        sender_info.update({
            "id": message.sender.id,
            "username": getattr(message.sender, "username", None),
            "first_name": getattr(message.sender, "first_name", None),
            "last_name": getattr(message.sender, "last_name", None),
        })
    
    # Determine media types
    media_types = []
    caption = None
    if message.media:
        if isinstance(message.media, MessageMediaPhoto):
            media_types.append("photo")
        elif isinstance(message.media, MessageMediaDocument) and message.media.document:
            mime_type = getattr(message.media.document, 'mime_type', '')
            if mime_type.startswith('video/'):
                media_types.append("video")
            elif mime_type.startswith('audio/'):
                media_types.append("audio")
            elif mime_type.startswith('image/'):
                media_types.append("image")
            else:
                media_types.append("document")
        
        caption = getattr(message.media, 'caption', None)
    
    # Build metadata record
    metadata = {
        "message_id": message.id,
        "chat_id": message.chat_id,
        "date": ensure_timezone(message.date).isoformat(),
        "from": sender_info,
        "text": message.message,
        "caption": caption,
        "media_types": media_types,
        "saved_files": [path.name for path in media_files],
        "errors": errors,
    }
    
    # Load existing data and append
    daily_data = []
    if metadata_file.exists():
        try:
            daily_data = json.loads(metadata_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logging.warning("Failed to read metadata file %s: %s", metadata_file, exc)
    
    daily_data.append(metadata)
    
    # Save updated data
    try:
        metadata_file.write_text(
            json.dumps(daily_data, ensure_ascii=False, indent=2), 
            encoding="utf-8"
        )
    except OSError as exc:
        logging.error("Failed to save metadata: %s", exc)


# ============================= Main Message Handler ==============================

async def handle_message(client: TelegramClient, message: Message, config: Config) -> None:
    """Process incoming message and archive media."""
    # Filter by chat if specified
    if config.chat_ids and str(message.chat_id) not in config.chat_ids:
        logging.debug("Ignoring message from chat %s (not monitored)", message.chat_id)
        return
    
    # Skip non-media messages
    if not message.media:
        logging.debug("Skipping message %s - no media", message.id)
        return
    
    logging.info("Processing message %s from chat %s", message.id, message.chat_id)
    
    # Send acknowledgment
    try:
        await client.send_message(message.chat_id, "📥 收到消息，正在存档中...", reply_to=message.id)
    except Exception as exc:
        logging.warning("Failed to send acknowledgment: %s", exc)
    
    # Setup directories
    cache_dir = config.save_dir / ".cache"
    ensure_directory(cache_dir)
    ensure_directory(config.save_dir)
    
    # Generate filename from caption or message text
    caption_text = ""
    if message.media and hasattr(message.media, 'caption') and message.media.caption:
        caption_text = message.media.caption
    elif message.message:
        caption_text = message.message
    
    base_filename = sanitize_filename(caption_text, f"msg_{message.id}") if caption_text else f"msg_{message.id}"
    
    # Download media
    _, extension = get_media_info(message.media)
    destination = config.save_dir / f"{base_filename}{extension}"
    
    success, result_msg = await download_media_file(client, message, destination, config.max_file_size_bytes)
    
    # Prepare results
    saved_files = [destination] if success else []
    errors = [] if success else [result_msg]
    
    # Log results
    if success:
        logging.info("✓ %s", result_msg)
    else:
        logging.warning("✗ %s", result_msg)
    
    # Save metadata
    try:
        await save_message_metadata(cache_dir, message, saved_files, errors)
    except Exception as exc:
        logging.error("Failed to save metadata: %s", exc)
    
    # Send completion notification
    try:
        await client.send_message(message.chat_id, f"{'✅' if success else '❌'} {result_msg}", reply_to=message.id)
    except Exception as exc:
        logging.warning("Failed to send completion message: %s", exc)


# ============================= Client Setup and Main =============================

async def setup_client(config: Config) -> TelegramClient:
    """Setup Telegram client with event handlers."""
    session_name = "archiver_bot" if config.bot_token else "archiver_user"
    client = TelegramClient(session_name, config.api_id, config.api_hash)
    
    @client.on(events.NewMessage)
    async def message_handler(event):
        await handle_message(client, event.message, config)
    
    return client


async def send_startup_messages(client: TelegramClient, config: Config) -> None:
    """Send startup greeting messages to monitored chats."""
    if not config.chat_ids:
        logging.info("Monitoring all chats (no specific chat IDs configured)")
        return
    
    for chat_id in config.chat_ids:
        try:
            await client.send_message(int(chat_id), config.greeting)
            logging.info("Sent greeting to chat %s", chat_id)
        except Exception as exc:
            logging.error("Failed to send greeting to chat %s: %s", chat_id, exc)
    
    logging.info("Monitoring chats: %s", ", ".join(config.chat_ids))


async def run_archiver(config: Config) -> None:
    """Run the main archiver application."""
    client = await setup_client(config)
    
    try:
        # Start client (bot or user mode)
        if config.bot_token:
            await client.start(bot_token=config.bot_token)
            logging.info("Started in bot mode")
        else:
            await client.start()
            logging.info("Started in user mode")
        
        # Send startup messages
        await send_startup_messages(client, config)
        logging.info("🚀 Archiver ready | Storage: %s | Max file size: %s",
                     config.save_dir, format_file_size(config.max_file_size_bytes))
        
        # Run until disconnected
        await client.run_until_disconnected()
        
    finally:
        await client.disconnect()
        logging.info("Client disconnected")


def main(argv: list[str] | None = None) -> int:
    """Main application entry point."""
    parser = build_parser()
    args = parser.parse_args(argv or sys.argv[1:])
    
    # Initial logging setup
    configure_logging("INFO")
    
    try:
        config = resolve_config(args)
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        return 2
    
    # Configure logging with user settings
    configure_logging(config.log_level)
    ensure_directory(config.save_dir)
    
    try:
        import asyncio
        asyncio.run(run_archiver(config))
    except KeyboardInterrupt:
        logging.info("🛑 Received interrupt signal, shutting down...")
    except Exception as exc:
        logging.exception("💥 Application error: %s", exc)
        return 1
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())