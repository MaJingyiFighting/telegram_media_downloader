"""Download Stat"""
import asyncio
import time
from enum import Enum

from pyrogram import Client
from loguru import logger

from module.app import TaskNode
from module.language import _t


class DownloadState(Enum):
    """Download state"""

    Downloading = 1
    StopDownload = 2


_download_result: dict = {}
_total_download_speed: int = 0
_total_download_size: int = 0
_last_download_time: float = time.time()
_download_state: DownloadState = DownloadState.Downloading

# variables for download speed monitoring
_low_speed_start_time: float = 0  # time when download speed drops below min_speed
_is_speed_low: bool = False  # flag to indicate if speed is low


def get_download_result() -> dict:
    """get global download result"""
    return _download_result


def get_total_download_speed() -> int:
    """get total download speed"""
    return _total_download_speed


def get_download_state() -> DownloadState:
    """get download state"""
    return _download_state


# pylint: disable = W0603
def set_download_state(state: DownloadState):
    """set download state"""
    global _download_state
    _download_state = state


def check_download_speed(app):
    """
    Check download speed and restart program if necessary
    
    Parameters
    ----------
    app: Application
        Application instance
    """
    global _is_speed_low, _low_speed_start_time
    
    current_time = time.time()
    min_speed = app.min_download_speed
    restart_limit_time = app.restart_limit_time
    
    # If no download is happening, reset the low speed timer
    if _total_download_speed == 0 and len(_download_result) == 0:
        _is_speed_low = False
        _low_speed_start_time = 0
        return
    
    # Check if speed is below minimum
    if _total_download_speed < min_speed:
        if not _is_speed_low:
            # First time speed drops below minimum
            _is_speed_low = True
            _low_speed_start_time = current_time
            logger.warning(
                f"{_t('Download speed')} ({_total_download_speed} B/s) {_t('is below minimum')} ({min_speed} B/s), "
                f"{_t('monitoring started')} (下载速度低于最低要求，开始监控)"
            )
        elif current_time - _low_speed_start_time > restart_limit_time:
            # Speed has been low for too long, restart program
            logger.warning(
                f"{_t('Download speed has been below')} {min_speed} B/s {_t('for')} {restart_limit_time} {_t('seconds')}, "
                f"{_t('restarting program')} (下载速度持续低于最低要求，重启程序)"
            )
            app.restart_program = True
    else:
        # Speed is above minimum, reset the low speed timer
        if _is_speed_low:
            logger.info(
                f"{_t('Download speed')} ({_total_download_speed} B/s) {_t('is back to normal')} (下载速度恢复正常)"
            )
        _is_speed_low = False
        _low_speed_start_time = 0


async def update_download_status(
    down_byte: int,
    total_size: int,
    message_id: int,
    file_name: str,
    start_time: float,
    node: TaskNode,
    client: Client,
):
    """update_download_status"""
    cur_time = time.time()
    # pylint: disable = W0603
    global _total_download_speed
    global _total_download_size
    global _last_download_time

    if node.is_stop_transmission:
        client.stop_transmission()

    chat_id = node.chat_id

    while get_download_state() == DownloadState.StopDownload:
        if node.is_stop_transmission:
            client.stop_transmission()
        await asyncio.sleep(1)

    if not _download_result.get(chat_id):
        _download_result[chat_id] = {}

    if _download_result[chat_id].get(message_id):
        last_download_byte = _download_result[chat_id][message_id]["down_byte"]
        last_time = _download_result[chat_id][message_id]["end_time"]
        download_speed = _download_result[chat_id][message_id]["download_speed"]
        each_second_total_download = _download_result[chat_id][message_id][
            "each_second_total_download"
        ]
        end_time = _download_result[chat_id][message_id]["end_time"]

        _total_download_size += down_byte - last_download_byte
        each_second_total_download += down_byte - last_download_byte

        if cur_time - last_time >= 1.0:
            download_speed = int(each_second_total_download / (cur_time - last_time))
            end_time = cur_time
            each_second_total_download = 0

        download_speed = max(download_speed, 0)

        _download_result[chat_id][message_id]["down_byte"] = down_byte
        _download_result[chat_id][message_id]["end_time"] = end_time
        _download_result[chat_id][message_id]["download_speed"] = download_speed
        _download_result[chat_id][message_id][
            "each_second_total_download"
        ] = each_second_total_download
    else:
        each_second_total_download = down_byte
        _download_result[chat_id][message_id] = {
            "down_byte": down_byte,
            "total_size": total_size,
            "file_name": file_name,
            "start_time": start_time,
            "end_time": cur_time,
            "download_speed": down_byte / (cur_time - start_time),
            "each_second_total_download": each_second_total_download,
            "task_id": node.task_id,
        }
        _total_download_size += down_byte

    if cur_time - _last_download_time >= 1.0:
        # update speed
        _total_download_speed = int(
            _total_download_size / (cur_time - _last_download_time)
        )
        _total_download_speed = max(_total_download_speed, 0)
        _total_download_size = 0
        _last_download_time = cur_time
