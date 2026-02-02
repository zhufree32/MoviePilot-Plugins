import os
import platform
import threading
import time
import traceback
from pathlib import Path
from typing import List, Tuple, Dict, Any
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.core.event import eventmanager
from app.schemas.types import EventType
from app.chain.storage import StorageChain
from app import schemas


class FileMonitorHandler(FileSystemEventHandler):
    """
    仅保留STRM文件删除监控的Handler
    """

    def __init__(self, monpath: str, sync: Any):
        super(FileMonitorHandler, self).__init__()
        self._watch_path = monpath
        self.sync = sync

    def _is_excluded_file(self, file_path: Path) -> bool:
        """检查文件是否应该被排除（仅保留关键词过滤）"""
        # 排除临时文件
        if file_path.suffix in [".!qB", ".part", ".mp", ".tmp", ".temp"]:
            return True
        # 检查关键字过滤
        if self.sync.exclude_keywords:
            for keyword in self.sync.exclude_keywords.split("\n"):
                if keyword and keyword in str(file_path):
                    logger.debug(f"{file_path} 命中过滤关键字 {keyword}，不处理")
                    return True
        return False

    def on_deleted(self, event):
        """仅处理STRM文件删除事件"""
        file_path = Path(event.src_path)
        if event.is_directory:
            return
        # 只处理.strm文件
        if file_path.suffix.lower() != ".strm":
            return
        # 命中过滤关键字不处理
        if self._is_excluded_file(file_path):
            return
        logger.info(f"监测到删除文件：{file_path}")
        # 处理STRM删除
        self.sync.handle_strm_deleted(file_path)


class RemoveLink(_PluginBase):
    # 插件基础信息
    plugin_name = "STRM文件清理"
    plugin_desc = "监控STRM文件删除，同步删除目标目录同名视频+字幕文件（.srt/.ass）"
    plugin_icon = "Ombi_A.png"
    plugin_version = "1.1"
    plugin_author = "DzAvril（精简版）"
    author_url = "https://github.com/zhufree32"
    plugin_config_prefix = "linkdeleted_"
    plugin_order = 0
    auth_level = 1

    # 仅保留核心配置项
    _enabled = False
    _notify = False
    exclude_keywords = ""
    _monitor_strm_deletion = False
    strm_path_mappings = ""
    _storagechain = None
    _observer = []

    # 后缀白名单（精准匹配用）
    VIDEO_EXTENSIONS = [".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".flv", ".wmv", ".mpeg", ".mpg"]
    SUBTITLE_EXTENSIONS = [".srt", ".ass"]  # 新增字幕后缀

    @staticmethod
    def __choose_observer():
        """选择最优的监控模式"""
        system = platform.system()
        try:
            if system == "Linux":
                from watchdog.observers.inotify import InotifyObserver
                return InotifyObserver()
            elif system == "Darwin":
                from watchdog.observers.fsevents import FSEventsObserver
                return FSEventsObserver()
            elif system == "Windows":
                from watchdog.observers.read_directory_changes import WindowsApiObserver
                return WindowsApiObserver()
        except Exception as error:
            logger.warn(f"导入模块错误：{error}，将使用 PollingObserver 监控目录")
        return PollingObserver()

    def init_plugin(self, config: dict = None):
        logger.info(f"初始化STRM文件清理插件（含字幕清理）")
        self._storagechain = StorageChain()

        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self.exclude_keywords = config.get("exclude_keywords") or ""
            self._monitor_strm_deletion = config.get("monitor_strm_deletion", False)
            self.strm_path_mappings = config.get("strm_path_mappings") or ""

        # 停止现有监控
        self.stop_service()

        if self._enabled and self._monitor_strm_deletion:
            # 解析STRM路径映射
            mappings = self._parse_strm_path_mappings()
            if not mappings:
                logger.warning("STRM监控已启用但未配置有效路径映射")
                return
            logger.info(f"配置了 {len(mappings)} 个 STRM 路径映射")
            strm_monitor_dirs = list(mappings.keys())
            logger.info(f"STRM 监控目录：{strm_monitor_dirs}")

            # 启动STRM监控
            for mon_path in strm_monitor_dirs:
                if not mon_path or not os.path.exists(mon_path):
                    logger.warning(f"STRM监控目录不存在：{mon_path}，跳过")
                    continue
                try:
                    observer = self.__choose_observer()
                    self._observer.append(observer)
                    observer.schedule(
                        FileMonitorHandler(mon_path, self),
                        mon_path,
                        recursive=True
                    )
                    observer.daemon = True
                    observer.start()
                    logger.info(f"{mon_path} 的 STRM 监控服务启动")
                except Exception as e:
                    err_msg = str(e)
                    if "inotify" in err_msg and "reached" in err_msg:
                        logger.warn(
                            f"目录监控启动异常：{err_msg}，请在宿主机执行：\n"
                            "echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf\n"
                            "echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf\n"
                            "sudo sysctl -p"
                        )
                    else:
                        logger.error(f"{mon_path} 启动STRM监控失败：{err_msg}")
                    self.systemmessage.put(
                        f"{mon_path} 启动STRM监控失败：{err_msg}",
                        title="STRM文件清理",
                    )

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """仅保留STRM相关配置表单"""
        return [
            {
                "component": "VForm",
                "content": [
                    # 插件说明
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "title": "🧹 STRM文件清理插件（含字幕清理）",
                                            "text": "监控STRM文件删除，同步删除目标目录中「文件名完全一致」的视频文件（MKV/MP4等）和字幕文件（.srt/.ass）。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 核心开关
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "monitor_strm_deletion",
                                            "label": "启用STRM文件监控",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 通知和过滤
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "删除后发送通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "exclude_keywords",
                                            "label": "排除关键词",
                                            "rows": 2,
                                            "placeholder": "每行一个关键词，命中的STRM文件不会触发删除",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # STRM路径映射
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "strm_path_mappings",
                                            "label": "STRM路径映射",
                                            "rows": 3,
                                            "placeholder": "格式：STRM目录:存储类型:网盘目录\n示例：/ssd/strm:local:/media\n支持存储类型：local（本地）、alipan、u115、rclone、alist",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # 格式说明
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "success",
                                            "variant": "tonal",
                                            "text": "支持的视频格式：MKV、MP4、TS、M2TS、AVI、MOV、FLV、WMV、MPEG、MPG；支持的字幕格式：SRT、ASS；仅删除「文件名（去后缀）与STRM文件名（去.strm）完全一致」的文件。",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "exclude_keywords": "",
            "monitor_strm_deletion": False,
            "strm_path_mappings": "",
        }

    def stop_service(self):
        """停止监控服务"""
        logger.debug("停止STRM监控服务")
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    logger.error(f"停止STRM监控失败：{str(e)}")
        self._observer = []

    def _parse_strm_path_mappings(self) -> Dict[str, Tuple[str, str]]:
        """解析STRM路径映射"""
        mappings = {}
        if not self.strm_path_mappings:
            return mappings
        for line in self.strm_path_mappings.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            try:
                parts = line.split(":", 2)
                if len(parts) == 2:
                    strm_path, storage_path = parts
                    storage_type = "local"
                elif len(parts) == 3:
                    strm_path, storage_type, storage_path = parts
                else:
                    logger.warning(f"无效的STRM路径映射：{line}")
                    continue
                # 校验路径合法性
                strm_path = strm_path.strip()
                storage_path = storage_path.strip()
                if not strm_path:
                    continue
                mappings[strm_path] = (storage_type.strip(), storage_path)
            except ValueError:
                logger.warning(f"解析STRM路径映射失败：{line}")
        return mappings

    def _get_storage_path_from_strm(self, strm_file_path: Path) -> Tuple[str, str]:
        """获取STRM对应的目标存储路径（仅去掉.strm后缀）"""
        mappings = self._parse_strm_path_mappings()
        strm_path_str = str(strm_file_path)
        for strm_prefix, (storage_type, storage_prefix) in mappings.items():
            if strm_path_str.startswith(strm_prefix):
                relative_path = strm_path_str[len(strm_prefix):].lstrip("/")
                storage_file_path = f"{storage_prefix.rstrip('/')}/{relative_path}"
                # 安全去掉.strm后缀
                if storage_file_path.lower().endswith(".strm"):
                    storage_file_path = storage_file_path[:-5]
                logger.debug(f"STRM文件 {strm_file_path} 映射到：[{storage_type}] {storage_file_path}")
                return storage_type, storage_file_path
        return None, None

    def _find_matched_files(self, storage_type: str, base_path: str, target_extensions: list) -> List[schemas.FileItem]:
        """
        通用方法：查找与STRM主名完全匹配的指定后缀文件
        :param storage_type: 存储类型
        :param base_path: STRM映射后的基础路径
        :param target_extensions: 目标后缀列表（如[.mkv, .mp4]）
        :return: 匹配的文件列表
        """
        # 获取STRM主名（仅去.strm后缀）
        strm_base_name = Path(base_path).name
        logger.debug(f"待匹配STRM主名：{strm_base_name}，目标后缀：{target_extensions}")
        
        # 获取目标目录
        parent_path = str(Path(base_path).parent)
        parent_item = schemas.FileItem(
            storage=storage_type,
            path=parent_path if parent_path.endswith("/") else parent_path + "/",
            type="dir",
        )
        if not self._storagechain.exists(parent_item):
            logger.debug(f"目标目录不存在：[{storage_type}] {parent_path}")
            return []

        # 遍历目录找完全匹配的文件
        files = self._storagechain.list_files(parent_item, recursion=False)
        if not files:
            logger.debug(f"目标目录为空：[{storage_type}] {parent_path}")
            return []

        matched_files = []
        for file_item in files:
            if file_item.type != "file":
                continue
            # 提取文件基础名（去后缀）和后缀（转小写）
            file_name = file_item.name
            file_base_name = Path(file_name).stem
            file_ext = Path(file_name).suffix.lower()
            
            logger.debug(f"对比：文件基础名={file_base_name} | STRM主名={strm_base_name} | 后缀={file_ext}")
            
            # 完全匹配：基础名一致 + 后缀在目标列表中
            if file_base_name == strm_base_name and file_ext in target_extensions:
                logger.info(f"找到完全匹配的文件：[{storage_type}] {file_item.path}")
                matched_files.append(file_item)
        
        if not matched_files:
            logger.info(f"未找到与「{strm_base_name}」完全匹配的{target_extensions}文件")
        return matched_files

    def _delete_file_item(self, storage_type: str, file_item: schemas.FileItem) -> bool:
        """删除单个文件，返回是否删除成功"""
        try:
            logger.info(f"准备删除文件：[{storage_type}] {file_item.path}")
            if self._storagechain.delete_file(file_item):
                logger.info(f"成功删除文件：[{storage_type}] {file_item.path}")
                return True
            else:
                logger.error(f"删除文件失败：[{storage_type}] {file_item.path}")
                return False
        except Exception as e:
            logger.error(f"删除文件异常：[{storage_type}] {file_item.path} - {str(e)}")
            return False

    def handle_strm_deleted(self, strm_file_path: Path):
        """处理STRM文件删除（核心逻辑：删视频+删字幕）"""
        logger.info(f"处理STRM文件删除：{strm_file_path}")
        deleted_files = []  # 记录删除成功的文件
        try:
            # 1. 获取目标存储路径
            storage_type, storage_path = self._get_storage_path_from_strm(strm_file_path)
            if not storage_type or not storage_path:
                logger.warning(f"未找到STRM文件 {strm_file_path} 的路径映射")
                return

            # 2. 查找并删除同名视频文件（完全匹配）
            video_files = self._find_matched_files(storage_type, storage_path, self.VIDEO_EXTENSIONS)
            for video_file in video_files:
                if self._delete_file_item(storage_type, video_file):
                    deleted_files.append(f"视频：[{storage_type}] {video_file.path}")

            # 3. 查找并删除同名字幕文件（.srt/.ass，完全匹配）
            subtitle_files = self._find_matched_files(storage_type, storage_path, self.SUBTITLE_EXTENSIONS)
            for subtitle_file in subtitle_files:
                if self._delete_file_item(storage_type, subtitle_file):
                    deleted_files.append(f"字幕：[{storage_type}] {subtitle_file.path}")

            # 4. 发送通知（如果开启且有删除成功的文件）
            if self._notify and deleted_files:
                notification_text = [f"✅ 成功删除以下文件", f"STRM文件：{strm_file_path}"] + deleted_files
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="🧹 STRM文件清理",
                    text="\n".join(notification_text),
                )

        except Exception as e:
            logger.error(f"处理STRM删除失败：{strm_file_path} - {str(e)} - {traceback.format_exc()}")

    def get_page(self) -> List[dict]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_command(self) -> List[Dict[str, Any]]:
        return []
