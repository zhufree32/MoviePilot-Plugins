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
    监控STRM/字幕文件删除的Handler
    - STRM删除：触发视频清理
    - SRT/ASS删除：触发同名字幕清理（完全匹配文件名+后缀）
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
        """处理文件删除事件：区分STRM/字幕文件"""
        file_path = Path(event.src_path)
        if event.is_directory:
            return
        
        # 获取文件后缀（转小写）
        file_ext = file_path.suffix.lower()
        
        # 过滤排除文件
        if self._is_excluded_file(file_path):
            return
        
        # 1. 处理STRM文件删除
        if file_ext == ".strm":
            logger.info(f"监测到删除STRM文件：{file_path}")
            self.sync.handle_strm_deleted(file_path)
        
        # 2. 处理字幕文件（.srt/.ass）删除
        elif file_ext in [".srt", ".ass"]:
            logger.info(f"监测到删除字幕文件：{file_path}")
            self.sync.handle_subtitle_deleted(file_path)


class RemoveLink(_PluginBase):
    # 插件基础信息
    plugin_name = "STRM&字幕文件清理"
    plugin_desc = "监控STRM/字幕文件删除，同步删除目标目录对应文件+空目录（STRM→同名视频，字幕→完全同名文件）"
    plugin_icon = "Ombi_A.png"
    plugin_version = "2.7"
    plugin_author = "zhufree（自用简版）"
    author_url = "https://github.com/DzAvril"
    plugin_config_prefix = "linkdeleted_"
    plugin_order = 0
    auth_level = 1

    # 核心配置项
    _enabled = False
    _notify = False
    exclude_keywords = ""
    _monitor_strm_deletion = False
    strm_path_mappings = ""
    _storagechain = None
    _observer = []

    # 后缀白名单
    VIDEO_EXTENSIONS = [".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".flv", ".wmv", ".mpeg", ".mpg"]
    SUBTITLE_EXTENSIONS = [".srt", ".ass"]

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
        logger.info(f"初始化STRM&字幕文件清理插件（含空目录清理）")
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
            # 解析路径映射（STRM/字幕共用同一套映射）
            mappings = self._parse_strm_path_mappings()
            if not mappings:
                logger.warning("监控已启用但未配置有效路径映射")
                return
            logger.info(f"配置了 {len(mappings)} 个路径映射")
            monitor_dirs = list(mappings.keys())
            logger.info(f"监控目录：{monitor_dirs}")

            # 启动监控（同时监控STRM/字幕文件）
            for mon_path in monitor_dirs:
                if not mon_path or not os.path.exists(mon_path):
                    logger.warning(f"监控目录不存在：{mon_path}，跳过")
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
                    logger.info(f"{mon_path} 的监控服务启动（监控STRM/SRT/ASS）")
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
                        logger.error(f"{mon_path} 启动监控失败：{err_msg}")
                    self.systemmessage.put(
                        f"{mon_path} 启动监控失败：{err_msg}",
                        title="STRM&字幕文件清理",
                    )

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置表单（更新说明）"""
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
                                            "title": "🧹 STRM&字幕文件清理插件（含空目录清理）",
                                            "text": "1. 监控STRM删除 → 同步删除目标目录「同名（去后缀）」视频文件；2. 监控SRT/ASS删除 → 同步删除目标目录「完全同名（含后缀）」字幕文件；3. 删除文件后自动清理空目录（逐级向上）。",
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
                                            "label": "启用文件监控",
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
                                            "placeholder": "每行一个关键词，命中的文件不会触发删除",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # 路径映射
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
                                            "label": "路径映射（STRM/字幕共用）",
                                            "rows": 3,
                                            "placeholder": "格式：本地目录:存储类型:目标目录\n示例：/ssd/media:local:/media\n支持存储类型：local（本地）、alipan、u115、rclone、alist",
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
                                            "text": "视频格式：MKV、MP4、TS、M2TS、AVI、MOV、FLV、WMV、MPEG、MPG；字幕格式：SRT、ASS；STRM匹配规则：去后缀同名，字幕匹配规则：完全同名（含后缀）；删除文件后自动清理空目录。",
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
        logger.debug("停止STRM&字幕监控服务")
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    logger.error(f"停止监控失败：{str(e)}")
        self._observer = []

    def _parse_strm_path_mappings(self) -> Dict[str, Tuple[str, str]]:
        """解析路径映射（STRM/字幕共用）"""
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
                    local_path, storage_path = parts
                    storage_type = "local"
                elif len(parts) == 3:
                    local_path, storage_type, storage_path = parts
                else:
                    logger.warning(f"无效的路径映射：{line}")
                    continue
                # 校验路径合法性
                local_path = local_path.strip()
                storage_path = storage_path.strip()
                if not local_path:
                    continue
                mappings[local_path] = (storage_type.strip(), storage_path)
            except ValueError:
                logger.warning(f"解析路径映射失败：{line}")
        return mappings

    def _get_target_path(self, local_file_path: Path, keep_suffix: bool = True) -> Tuple[str, str]:
        """
        获取本地文件对应的目标存储路径
        :param local_file_path: 本地文件路径
        :param keep_suffix: 是否保留后缀（True=保留，False=去掉后缀，仅STRM用）
        :return: (存储类型, 目标路径)
        """
        mappings = self._parse_strm_path_mappings()
        local_path_str = str(local_file_path)
        
        for local_prefix, (storage_type, storage_prefix) in mappings.items():
            if local_path_str.startswith(local_prefix):
                # 计算相对路径
                relative_path = local_path_str[len(local_prefix):].lstrip("/")
                
                # 处理后缀（STRM去掉.strm，字幕保留完整后缀）
                if not keep_suffix and relative_path.lower().endswith(".strm"):
                    relative_path = relative_path[:-5]
                
                # 构建目标路径
                target_path = f"{storage_prefix.rstrip('/')}/{relative_path}"
                logger.debug(f"本地文件 {local_file_path} 映射到：[{storage_type}] {target_path}")
                return storage_type, target_path
        
        return None, None

    def _find_file_by_full_name(self, storage_type: str, target_path: str) -> schemas.FileItem:
        """
        按完全文件名（含路径+后缀）查找目标文件
        用于字幕文件的精准匹配
        """
        # 拆分目录和文件名
        target_dir = str(Path(target_path).parent)
        target_filename = Path(target_path).name
        
        # 构建目录Item
        dir_item = schemas.FileItem(
            storage=storage_type,
            path=target_dir if target_dir.endswith("/") else target_dir + "/",
            type="dir",
        )
        
        # 检查目录是否存在
        if not self._storagechain.exists(dir_item):
            logger.debug(f"目标目录不存在：[{storage_type}] {target_dir}")
            return None
        
        # 遍历目录找完全同名的文件
        files = self._storagechain.list_files(dir_item, recursion=False)
        for file_item in files:
            if file_item.type == "file" and file_item.name == target_filename:
                logger.info(f"找到完全匹配的文件：[{storage_type}] {file_item.path}")
                return file_item
        
        logger.info(f"未找到完全匹配的文件：[{storage_type}] {target_path}")
        return None

    def _find_video_by_basename(self, storage_type: str, target_path: str) -> List[schemas.FileItem]:
        """按基础名（去后缀）查找同名视频文件（原STRM逻辑）"""
        # 获取基础名
        base_name = Path(target_path).name
        target_dir = str(Path(target_path).parent)
        
        # 构建目录Item
        dir_item = schemas.FileItem(
            storage=storage_type,
            path=target_dir if target_dir.endswith("/") else target_dir + "/",
            type="dir",
        )
        
        if not self._storagechain.exists(dir_item):
            logger.debug(f"目标目录不存在：[{storage_type}] {target_dir}")
            return []
        
        # 查找同名视频
        files = self._storagechain.list_files(dir_item, recursion=False)
        matched = []
        for file_item in files:
            if file_item.type != "file":
                continue
            file_basename = Path(file_item.name).stem
            file_ext = Path(file_item.name).suffix.lower()
            if file_basename == base_name and file_ext in self.VIDEO_EXTENSIONS:
                logger.info(f"找到同名视频文件：[{storage_type}] {file_item.path}")
                matched.append(file_item)
        
        if not matched:
            logger.info(f"未找到与「{base_name}」同名的视频文件")
        return matched

    def _delete_file(self, storage_type: str, file_item: schemas.FileItem) -> bool:
        """删除文件，返回是否成功"""
        try:
            logger.info(f"准备删除：[{storage_type}] {file_item.path}")
            if self._storagechain.delete_file(file_item):
                logger.info(f"成功删除：[{storage_type}] {file_item.path}")
                return True
            else:
                logger.error(f"删除失败：[{storage_type}] {file_item.path}")
                return False
        except Exception as e:
            logger.error(f"删除异常：[{storage_type}] {file_item.path} - {str(e)}")
            return False

    def _delete_empty_dirs(self, storage_type: str, file_item: schemas.FileItem) -> List[str]:
        """
        逐级删除空目录（从文件所在目录向上）
        :param storage_type: 存储类型
        :param file_item: 已删除文件的FileItem
        :return: 已删除的空目录列表
        """
        deleted_dirs = []
        try:
            # 获取文件所在目录
            current_dir_path = str(Path(file_item.path).parent)
            # 根目录标识（避免无限循环）
            root_markers = ["/", "\\", ""]
            
            while current_dir_path not in root_markers:
                # 构建目录Item
                dir_item = schemas.FileItem(
                    storage=storage_type,
                    path=current_dir_path if current_dir_path.endswith("/") else current_dir_path + "/",
                    type="dir",
                )
                
                # 检查目录是否存在
                if not self._storagechain.exists(dir_item):
                    logger.debug(f"目录已不存在：[{storage_type}] {current_dir_path}")
                    # 继续检查上级目录
                    current_dir_path = str(Path(current_dir_path).parent)
                    continue
                
                # 列出目录内容（仅一级，不递归）
                dir_contents = self._storagechain.list_files(dir_item, recursion=False)
                if not dir_contents:
                    # 目录为空，删除
                    logger.info(f"准备删除空目录：[{storage_type}] {current_dir_path}")
                    if self._storagechain.delete_file(dir_item):
                        logger.info(f"成功删除空目录：[{storage_type}] {current_dir_path}")
                        deleted_dirs.append(f"空目录：[{storage_type}] {current_dir_path}")
                        # 继续检查上级目录
                        current_dir_path = str(Path(current_dir_path).parent)
                    else:
                        logger.error(f"删除空目录失败：[{storage_type}] {current_dir_path}")
                        break
                else:
                    # 目录非空，停止检查
                    logger.debug(f"目录非空，停止清理：[{storage_type}] {current_dir_path}")
                    break
            
        except Exception as e:
            logger.error(f"清理空目录异常：{str(e)}")
        
        return deleted_dirs

    def handle_strm_deleted(self, strm_file_path: Path):
        """处理STRM文件删除：删同名视频 + 空目录"""
        deleted_files = []
        deleted_dirs = []
        try:
            # 获取目标路径（去掉.strm后缀）
            storage_type, target_path = self._get_target_path(strm_file_path, keep_suffix=False)
            if not storage_type or not target_path:
                logger.warning(f"未找到STRM文件 {strm_file_path} 的路径映射")
                return

            # 查找同名视频
            video_files = self._find_video_by_basename(storage_type, target_path)
            for video in video_files:
                if self._delete_file(storage_type, video):
                    deleted_files.append(f"视频：[{storage_type}] {video.path}")
                    # 清理空目录
                    dirs = self._delete_empty_dirs(storage_type, video)
                    deleted_dirs.extend(dirs)

            # 发送通知
            if self._notify and (deleted_files or deleted_dirs):
                notification_lines = [f"✅ STRM删除触发", f"STRM文件：{strm_file_path}"]
                notification_lines.extend(deleted_files)
                notification_lines.extend(deleted_dirs)
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="🧹 STRM文件清理",
                    text="\n".join(notification_lines),
                )
        except Exception as e:
            logger.error(f"处理STRM删除失败：{strm_file_path} - {str(e)} - {traceback.format_exc()}")

    def handle_subtitle_deleted(self, subtitle_file_path: Path):
        """处理字幕文件删除：删完全同名文件 + 空目录"""
        deleted_files = []
        deleted_dirs = []
        try:
            # 获取目标路径（保留完整后缀）
            storage_type, target_path = self._get_target_path(subtitle_file_path, keep_suffix=True)
            if not storage_type or not target_path:
                logger.warning(f"未找到字幕文件 {subtitle_file_path} 的路径映射")
                return

            # 查找完全同名的字幕文件
            subtitle_file = self._find_file_by_full_name(storage_type, target_path)
            if subtitle_file:
                if self._delete_file(storage_type, subtitle_file):
                    deleted_files.append(f"字幕：[{storage_type}] {subtitle_file.path}")
                    # 清理空目录
                    dirs = self._delete_empty_dirs(storage_type, subtitle_file)
                    deleted_dirs.extend(dirs)

            # 发送通知
            if self._notify and (deleted_files or deleted_dirs):
                notification_lines = [f"✅ 字幕删除触发", f"字幕文件：{subtitle_file_path}"]
                notification_lines.extend(deleted_files)
                notification_lines.extend(deleted_dirs)
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="🧹 字幕文件清理",
                    text="\n".join(notification_lines),
                )
        except Exception as e:
            logger.error(f"处理字幕删除失败：{subtitle_file_path} - {str(e)} - {traceback.format_exc()}")

    def get_page(self) -> List[dict]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_command(self) -> List[Dict[str, Any]]:
        return []
