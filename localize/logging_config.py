import logging
import sys
import os
from logging import Handler
from logging.handlers import RotatingFileHandler

# Import tqdm here, as it's now a dependency for our custom handler.
from tqdm import tqdm


class TqdmLoggingHandler(Handler):
    """
    Custom logging handler that uses tqdm.write to output log messages.
    This prevents log messages from interfering with the tqdm progress bar.
    """
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg, file=sys.stderr)  # Write to stderr to match tqdm's default
            self.flush()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:  # noqa: BLE001
            self.handleError(record)


def setup_logger(log_level_str: str, log_file_path: str, log_to_console: bool) -> logging.Logger:
    """
    Set up the logger for the translation script.

    Configures a logger with a file handler and a custom tqdm-aware stream handler.
    This ensures that log messages do not interfere with the tqdm progress bars
    while providing both file and console logging.

    Args:
        log_level_str: The logging level as a string (e.g., 'INFO', 'DEBUG').
        log_file_path: The path to the log file.
        log_to_console: A boolean indicating whether to log to the console.

    Returns:
        The configured logger instance.
    """
    # Set the logging level from the config string
    requested_level = str(log_level_str or "INFO").upper()
    if requested_level not in logging._nameToLevel:  # noqa: SLF001 - stdlib exposes configured level names here.
        print(
            f"Warning: invalid log_level '{log_level_str}', falling back to INFO.",
            file=sys.stderr,
        )
        requested_level = "INFO"
    log_level = logging._nameToLevel[requested_level]  # noqa: SLF001

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    logger = logging.getLogger("translation_script")
    logger.setLevel(log_level)
    logger.handlers.clear()
    logger.propagate = True

    # Define a standard formatter for log messages
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # --- File Handler ---
    try:
        resolved_log_file_path = os.path.abspath(log_file_path)
        log_dir = os.path.dirname(resolved_log_file_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            resolved_log_file_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding='utf-8',
            delay=True,
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError as exc:
        print(
            f"Warning: could not configure file logging at '{log_file_path}': {exc}",
            file=sys.stderr,
        )
    # --- End File Handler ---

    # --- Console Handler ---
    # Use our custom TqdmLoggingHandler for console output
    if log_to_console:
        # We now use the custom handler that plays nice with tqdm.
        tqdm_handler = TqdmLoggingHandler()
        tqdm_handler.setFormatter(formatter)
        root_logger.addHandler(tqdm_handler)
    # --- End Console Handler ---

    return logger
