from flask import Flask, Response, render_template, request, redirect, url_for, flash, jsonify
import cv2
import threading
import time
import logging
from datetime import datetime
import numpy as np
import os
import queue
import signal
import sys
import shutil
from werkzeug.utils import secure_filename
import json
import mss  # 屏幕捕获库
import mss.tools
from PIL import Image
import pygetwindow as gw  # 窗口管理库
import pyautogui
import tkinter as tk
from tkinter import ttk
import subprocess

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('video_stream.log')
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # 请在生产环境中更改

# 配置文件上传
UPLOAD_FOLDER = 'uploads'
SCREEN_RECORDINGS_FOLDER = 'recordings'
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'flv', 'wmv'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SCREEN_RECORDINGS_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SCREEN_RECORDINGS_FOLDER'] = SCREEN_RECORDINGS_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 500  # 500MB限制

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_video_info(video_path):
    """获取视频文件信息"""
    try:
        if not os.path.exists(video_path):
            return None
            
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None
            
        info = {
            'filename': os.path.basename(video_path),
            'path': video_path,
            'size': os.path.getsize(video_path),
            'duration': None,
            'fps': None,
            'width': None,
            'height': None,
            'frame_count': None,
            'created_time': datetime.fromtimestamp(os.path.getctime(video_path)).strftime('%Y-%m-%d %H:%M:%S'),
            'type': 'uploaded'
        }
        
        # 获取视频技术信息
        info['fps'] = cap.get(cv2.CAP_PROP_FPS)
        info['width'] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        info['height'] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        info['frame_count'] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if info['fps'] > 0:
            info['duration'] = info['frame_count'] / info['fps']
            
        cap.release()
        return info
        
    except Exception as e:
        logger.error(f"获取视频信息失败: {e}")
        return None

def format_file_size(size):
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"

def format_duration(seconds):
    """格式化时长"""
    if seconds is None:
        return "N/A"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

class ScreenRecorder:
    """Windows屏幕录制类"""
    def __init__(self, target_fps=24, recording_mode='full_screen'):
        self.target_fps = target_fps
        self.recording_mode = recording_mode  # 'full_screen', 'window', 'region'
        self.frame_interval = 1.0 / target_fps
        self.last_frame_time = 0
        self.frame_queue = queue.Queue(maxsize=3)
        self.initialized = False
        self.running = False
        self.thread = None
        self.stats = {
            'frames_captured': 0,
            'frames_served': 0,
            'errors': 0,
            'start_time': time.time(),
            'recording_size': None
        }
        
        # 录制配置
        self.recording_config = {
            'monitor': 1,  # 默认显示器
            'region': None,  # 录制区域 (left, top, width, height)
            'window_title': None,  # 录制的窗口标题
            'output_path': None
        }
        
        # 初始化MSS - 在每个线程中单独创建
        self.sct = None  # 不在主线程初始化
        self.initialized = True  # 标记为已初始化，但会在工作线程中创建mss实例
        
    def _initialize_mss_in_thread(self):
        """在线程中初始化MSS屏幕捕获"""
        try:
            # 在线程中创建新的mss实例
            self.sct = mss.mss()
            logger.info(f"MSS屏幕捕获在线程 {threading.current_thread().name} 中初始化成功")
            
            # 显示显示器信息
            for i, monitor in enumerate(self.sct.monitors):
                if i == 0:
                    logger.info(f"显示器 {i}: 所有显示器组合 ({monitor['width']}x{monitor['height']})")
                else:
                    logger.info(f"显示器 {i}: {monitor['width']}x{monitor['height']} "
                              f"(位置: {monitor['left']}, {monitor['top']})")
            
            return True
            
        except Exception as e:
            logger.error(f"在线程中初始化MSS失败: {e}")
            self.sct = None
            return False
    
    def set_recording_region(self, region):
        """设置录制区域"""
        self.recording_config['region'] = region
    
    def set_window_title(self, window_title):
        """设置要录制的窗口标题"""
        self.recording_config['window_title'] = window_title
    
    def set_output_path(self, output_path):
        """设置录制输出路径"""
        self.recording_config['output_path'] = output_path
    
    def get_available_windows(self):
        """获取可录制的窗口列表"""
        try:
            windows = gw.getAllTitles()
            return [title for title in windows if title.strip()]  # 过滤空标题
        except Exception as e:
            logger.error(f"获取窗口列表失败: {e}")
            return []
    
    def get_window_region(self, window_title):
        """获取指定窗口的区域"""
        try:
            window = gw.getWindowsWithTitle(window_title)
            if window:
                win = window[0]
                return {
                    'left': win.left,
                    'top': win.top,
                    'width': win.width,
                    'height': win.height,
                    'title': win.title
                }
        except Exception as e:
            logger.error(f"获取窗口区域失败: {e}")
        return None
    
    def start_recording(self):
        """开始屏幕录制"""
        if not self.initialized:
            logger.error("录制器未初始化")
            return False
            
        if self.running:
            logger.warning("录制已在运行中")
            return False
            
        self.running = True
        self.thread = threading.Thread(target=self._recording_loop, daemon=True)
        self.thread.start()
        logger.info("屏幕录制线程已启动")
        return True
    
    def _recording_loop(self):
        """录制主循环"""
        consecutive_errors = 0
        max_consecutive_errors = 10
        
        # 在线程中初始化MSS
        if not self._initialize_mss_in_thread():
            logger.error("无法在线程中初始化MSS，停止录制")
            self.running = False
            return
        
        while self.running and self.sct:
            try:
                current_time = time.time()
                
                # 控制帧率
                if current_time - self.last_frame_time < self.frame_interval:
                    time.sleep(0.001)
                    continue
                
                # 根据录制模式获取屏幕截图
                screenshot = None
                
                if self.recording_mode == 'full_screen':
                    # 全屏录制
                    monitor = self.sct.monitors[self.recording_config['monitor']]
                    screenshot = self.sct.grab(monitor)
                    
                elif self.recording_mode == 'window' and self.recording_config['window_title']:
                    # 窗口录制
                    region = self.get_window_region(self.recording_config['window_title'])
                    if region:
                        screenshot = self.sct.grab({
                            'left': region['left'],
                            'top': region['top'],
                            'width': region['width'],
                            'height': region['height']
                        })
                    else:
                        logger.warning(f"未找到窗口: {self.recording_config['window_title']}")
                        time.sleep(0.1)
                        continue
                        
                elif self.recording_mode == 'region' and self.recording_config['region']:
                    # 区域录制
                    screenshot = self.sct.grab(self.recording_config['region'])
                    
                else:
                    # 默认全屏录制
                    monitor = self.sct.monitors[1]  # 主显示器
                    screenshot = self.sct.grab(monitor)
                
                if screenshot:
                    # 转换为numpy数组
                    frame = np.array(screenshot)
                    
                    # 转换颜色空间 BGR -> RGB
                    # frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
                    
                    # 调整大小（如果需要）
                    target_width = 320
                    target_height = 172
                    if frame.shape[1] != target_width or frame.shape[0] != target_height:
                        frame = cv2.resize(frame, (target_width, target_height))
                    
                    # 添加录制信息
                    frame = self._add_recording_info(frame)
                    
                    # JPEG编码
                    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
                    ret, jpeg_data = cv2.imencode('.jpg', frame, encode_param)
                    
                    if ret:
                        # 非阻塞方式放入队列
                        try:
                            if self.frame_queue.full():
                                try:
                                    self.frame_queue.get_nowait()
                                except queue.Empty:
                                    pass
                            
                            self.frame_queue.put(jpeg_data.tobytes(), timeout=0.05)
                            self.stats['frames_captured'] += 1
                            consecutive_errors = 0
                            
                        except queue.Full:
                            pass  # 静默丢弃帧
                            
                    else:
                        logger.error("JPEG编码失败")
                        consecutive_errors += 1
                
                self.last_frame_time = current_time
                
                # 错误处理
                if consecutive_errors >= max_consecutive_errors:
                    logger.error("连续错误过多，尝试重新初始化")
                    consecutive_errors = 0
                    # 重新初始化MSS
                    try:
                        if self.sct:
                            self.sct.close()
                        self._initialize_mss_in_thread()
                    except Exception as e:
                        logger.error(f"重新初始化MSS失败: {e}")
                    time.sleep(0.5)
                
                # 定期输出状态
                if self.stats['frames_captured'] % 100 == 0:
                    self._log_stats()
                    
            except Exception as e:
                logger.error(f"录制循环错误: {e}")
                consecutive_errors += 1
                time.sleep(0.1)
        
        # 清理资源
        if self.sct:
            try:
                self.sct.close()
            except:
                pass
    
    def _add_recording_info(self, frame):
        """在帧上添加录制信息"""
        try:
            display_frame = frame.copy()
            height, width = display_frame.shape[:2]
            
            # 添加时间戳和帧计数
            timestamp = datetime.now().strftime("%H:%M:%S")
            info_text = f"录制中 | {timestamp} | 帧: {self.stats['frames_captured']} | FPS: {self.target_fps}"
            
            # 添加半透明背景条
            overlay = display_frame.copy()
            cv2.rectangle(overlay, (0, 0), (width, 20), (0, 0, 0), -1)
            display_frame = cv2.addWeighted(overlay, 0.3, display_frame, 0.7, 0)
            
            # 添加文本
            cv2.putText(display_frame, info_text, (5, 15), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
            
            # 添加红色录制指示器
            cv2.circle(display_frame, (width - 20, 10), 5, (0, 0, 255), -1)
            
            return display_frame
        except Exception as e:
            logger.error(f"添加录制信息失败: {e}")
            return frame
    
    def get_frame(self):
        """获取当前帧（非阻塞）"""
        try:
            frame = self.frame_queue.get_nowait()
            self.stats['frames_served'] += 1
            return frame
        except queue.Empty:
            return None
    
    def _log_stats(self):
        """记录统计信息"""
        elapsed = time.time() - self.stats['start_time']
        captured_fps = self.stats['frames_captured'] / elapsed
        served_fps = self.stats['frames_served'] / elapsed
        
        logger.info(
            f"录制状态: 捕获 {self.stats['frames_captured']} 帧, "
            f"服务 {self.stats['frames_served']} 帧, "
            f"捕获FPS: {captured_fps:.2f}, 服务FPS: {served_fps:.2f}"
        )
    
    def get_stats(self):
        """获取统计信息"""
        elapsed = time.time() - self.stats['start_time']
        stats = self.stats.copy()
        stats['elapsed_time'] = elapsed
        stats['captured_fps'] = stats['frames_captured'] / elapsed if elapsed > 0 else 0
        stats['served_fps'] = stats['frames_served'] / elapsed if elapsed > 0 else 0
        stats['queue_size'] = self.frame_queue.qsize()
        stats['recording_mode'] = self.recording_mode
        stats['target_fps'] = self.target_fps
        return stats
    
    def stop_recording(self):
        """停止录制"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=3.0)
        # 在线程中清理MSS资源
        if self.sct:
            try:
                self.sct.close()
            except:
                pass
        logger.info("屏幕录制已停止")
    
    def save_recording(self, duration=10, output_filename=None):
        """保存录制视频到文件"""
        if not output_filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"screen_recording_{timestamp}.mp4"
        
        output_path = os.path.join(app.config['SCREEN_RECORDINGS_FOLDER'], output_filename)
        
        try:
            # 在新线程中创建独立的mss实例用于录制
            with mss.mss() as sct:
                # 获取显示器信息
                monitor = sct.monitors[1]  # 主显示器
                
                # 设置视频编码器
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                fps = self.target_fps
                
                # 创建VideoWriter
                out = cv2.VideoWriter(output_path, fourcc, fps, 
                                     (monitor['width'], monitor['height']))
                
                logger.info(f"开始录制视频: {output_filename}")
                logger.info(f"分辨率: {monitor['width']}x{monitor['height']}")
                logger.info(f"帧率: {fps} FPS")
                logger.info(f"时长: {duration} 秒")
                
                start_time = time.time()
                frame_count = 0
                
                while time.time() - start_time < duration and self.running:
                    try:
                        # 捕获屏幕
                        screenshot = sct.grab(monitor)
                        frame = np.array(screenshot)
                        
                        # 转换颜色空间
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                        
                        # 写入帧
                        out.write(frame)
                        frame_count += 1
                        
                        # 控制帧率
                        time.sleep(1.0 / fps)
                        
                    except Exception as e:
                        logger.error(f"录制帧时出错: {e}")
                        break
                
                out.release()
                
                actual_duration = time.time() - start_time
                actual_fps = frame_count / actual_duration if actual_duration > 0 else 0
                
                logger.info(f"录制完成: {output_filename}")
                logger.info(f"实际录制: {actual_duration:.2f} 秒, {frame_count} 帧, {actual_fps:.2f} FPS")
                
                if os.path.exists(output_path):
                    file_size = os.path.getsize(output_path)
                    logger.info(f"文件大小: {format_file_size(file_size)}")
                
                return output_path
                
        except Exception as e:
            logger.error(f"保存录制失败: {e}")
            return None

class VideoStreamer:
    def __init__(self, video_path, target_fps=25, target_width=320, target_height=172):
        self.video_path = video_path
        self.target_fps = target_fps
        self.target_width = target_width
        self.target_height = target_height
        self.frame_interval = 1.0 / target_fps
        self.last_frame_time = 0
        self.frame_queue = queue.Queue(maxsize=3)  # 限制队列大小
        self.initialized = False
        self.running = False
        self.thread = None
        self.stats = {
            'frames_processed': 0,
            'frames_served': 0,
            'errors': 0,
            'start_time': time.time()
        }
        
        # 初始化视频捕获
        self.cap = self._initialize_capture()
        if not self.cap:
            return
            
        self.initialized = True
        self.start_streaming()
    
    def _initialize_capture(self):
        """初始化视频捕获"""
        try:
            if not os.path.exists(self.video_path):
                logger.error(f"视频文件不存在: {self.video_path}")
                return None
                
            cap = cv2.VideoCapture(self.video_path)
            if not cap.isOpened():
                logger.error(f"无法打开视频文件: {self.video_path}")
                return None
                
            # 获取视频信息
            original_fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = total_frames / original_fps if original_fps > 0 else 0
            
            logger.info(f"视频加载成功: {self.video_path}")
            logger.info(f"原始分辨率: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
            logger.info(f"原始FPS: {original_fps:.2f}")
            logger.info(f"目标FPS: {self.target_fps}")
            logger.info(f"目标分辨率: {self.target_width}x{self.target_height}")
            logger.info(f"总帧数: {total_frames}, 时长: {duration:.2f}秒")
            
            return cap
            
        except Exception as e:
            logger.error(f"初始化视频捕获失败: {e}")
            return None
    
    def start_streaming(self):
        """开始视频流线程"""
        if self.initialized and not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._stream_loop, daemon=True)
            self.thread.start()
            logger.info("视频流线程已启动")
    
    def _stream_loop(self):
        """视频流主循环"""
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while self.running and self.cap.isOpened():
            try:
                current_time = time.time()
                
                # 控制帧率
                if current_time - self.last_frame_time < self.frame_interval:
                    time.sleep(0.01)
                    continue
                
                # 读取帧
                ret, frame = self.cap.read()
                if not ret:
                    # 循环播放
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    logger.info("视频循环播放")
                    continue
                
                # 调整帧大小
                resized_frame = cv2.resize(frame, (self.target_width, self.target_height))
                
                # 添加帧信息
                info_frame = self._add_frame_info(resized_frame, self.stats['frames_processed'])
                
                # JPEG编码（降低质量以减少带宽）
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 70]  # 降低质量到70%
                ret, jpeg_data = cv2.imencode('.jpg', info_frame, encode_param)
                
                if ret:
                    # 非阻塞方式放入队列
                    try:
                        if self.frame_queue.full():
                            # 队列已满时丢弃最旧的帧
                            try:
                                self.frame_queue.get_nowait()
                            except queue.Empty:
                                pass
                        
                        self.frame_queue.put(jpeg_data.tobytes(), timeout=0.1)
                        self.stats['frames_processed'] += 1
                        consecutive_errors = 0
                        
                    except queue.Full:
                        logger.warning("帧队列已满，丢弃帧")
                        
                else:
                    logger.error("JPEG编码失败")
                    consecutive_errors += 1
                
                self.last_frame_time = current_time
                
                # 错误过多时重启捕获
                if consecutive_errors >= max_consecutive_errors:
                    logger.error("连续错误过多，尝试重新初始化捕获")
                    self._restart_capture()
                    consecutive_errors = 0
                
                # 定期输出状态
                if self.stats['frames_processed'] % 100 == 0:
                    self._log_stats()
                    
            except Exception as e:
                logger.error(f"流处理循环错误: {e}")
                consecutive_errors += 1
                time.sleep(0.01)
    
    def _restart_capture(self):
        """重启视频捕获"""
        try:
            if self.cap:
                self.cap.release()
            self.cap = self._initialize_capture()
            if self.cap:
                logger.info("视频捕获重启成功")
            else:
                logger.error("视频捕获重启失败")
        except Exception as e:
            logger.error(f"重启视频捕获失败: {e}")
    
    def _add_frame_info(self, frame, frame_count):
        """在帧上添加信息文本"""
        try:
            display_frame = frame.copy()
            
            # 添加帧计数和时间戳
            timestamp = datetime.now().strftime("%H:%M:%S")
            info_text = f"F:{frame_count} T:{timestamp}"
            
            # 在帧顶部添加信息栏
            cv2.rectangle(display_frame, (0, 0), (self.target_width, 16), (0, 0, 0), -1)
            
            # 添加文本
            cv2.putText(display_frame, info_text, (5, 12), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1)
            
            return display_frame
        except Exception as e:
            logger.error(f"添加帧信息失败: {e}")
            return frame
    
    def get_frame(self):
        """获取当前帧（非阻塞）"""
        try:
            frame = self.frame_queue.get_nowait()
            self.stats['frames_served'] += 1
            return frame
        except queue.Empty:
            return None
    
    def _log_stats(self):
        """记录统计信息"""
        elapsed = time.time() - self.stats['start_time']
        processed_fps = self.stats['frames_processed'] / elapsed
        served_fps = self.stats['frames_served'] / elapsed
        
        logger.info(
            f"状态: 处理 {self.stats['frames_processed']} 帧, "
            f"服务 {self.stats['frames_served']} 帧, "
            f"处理FPS: {processed_fps:.2f}, 服务FPS: {served_fps:.2f}, "
            f"错误: {self.stats['errors']}"
        )
    
    def get_stats(self):
        """获取统计信息"""
        elapsed = time.time() - self.stats['start_time']
        stats = self.stats.copy()
        stats['elapsed_time'] = elapsed
        stats['processed_fps'] = stats['frames_processed'] / elapsed if elapsed > 0 else 0
        stats['served_fps'] = stats['frames_served'] / elapsed if elapsed > 0 else 0
        stats['queue_size'] = self.frame_queue.qsize()
        return stats
    
    def stop_streaming(self):
        """停止视频流"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=3.0)
        if self.cap:
            self.cap.release()
        logger.info("视频流已停止")


class VideoStreamer:
    def __init__(self, video_path, target_fps=25, target_width=320, target_height=172):
        self.video_path = video_path
        self.target_fps = target_fps
        self.target_width = target_width
        self.target_height = target_height
        self.frame_interval = 1.0 / target_fps
        self.last_frame_time = 0
        self.frame_queue = queue.Queue(maxsize=3)  # 限制队列大小
        self.initialized = False
        self.running = False
        self.thread = None
        self.stats = {
            'frames_processed': 0,
            'frames_served': 0,
            'errors': 0,
            'start_time': time.time()
        }
        
        # 初始化视频捕获
        self.cap = self._initialize_capture()
        if not self.cap:
            return
            
        self.initialized = True
        self.start_streaming()
    
    def _initialize_capture(self):
        """初始化视频捕获"""
        try:
            if not os.path.exists(self.video_path):
                logger.error(f"视频文件不存在: {self.video_path}")
                return None
                
            cap = cv2.VideoCapture(self.video_path)
            if not cap.isOpened():
                logger.error(f"无法打开视频文件: {self.video_path}")
                return None
                
            # 获取视频信息
            original_fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = total_frames / original_fps if original_fps > 0 else 0
            
            logger.info(f"视频加载成功: {self.video_path}")
            logger.info(f"原始分辨率: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
            logger.info(f"原始FPS: {original_fps:.2f}")
            logger.info(f"目标FPS: {self.target_fps}")
            logger.info(f"目标分辨率: {self.target_width}x{self.target_height}")
            logger.info(f"总帧数: {total_frames}, 时长: {duration:.2f}秒")
            
            return cap
            
        except Exception as e:
            logger.error(f"初始化视频捕获失败: {e}")
            return None
    
    def start_streaming(self):
        """开始视频流线程"""
        if self.initialized and not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._stream_loop, daemon=True)
            self.thread.start()
            logger.info("视频流线程已启动")
    
    def _stream_loop(self):
        """视频流主循环"""
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while self.running and self.cap.isOpened():
            try:
                current_time = time.time()
                
                # 控制帧率
                if current_time - self.last_frame_time < self.frame_interval:
                    time.sleep(0.01)
                    continue
                
                # 读取帧
                ret, frame = self.cap.read()
                if not ret:
                    # 循环播放
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    logger.info("视频循环播放")
                    continue
                
                # 调整帧大小
                resized_frame = cv2.resize(frame, (self.target_width, self.target_height))
                
                # 添加帧信息
                info_frame = self._add_frame_info(resized_frame, self.stats['frames_processed'])
                
                # JPEG编码（降低质量以减少带宽）
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 70]  # 降低质量到70%
                ret, jpeg_data = cv2.imencode('.jpg', info_frame, encode_param)
                
                if ret:
                    # 非阻塞方式放入队列
                    try:
                        if self.frame_queue.full():
                            # 队列已满时丢弃最旧的帧
                            try:
                                self.frame_queue.get_nowait()
                            except queue.Empty:
                                pass
                        
                        self.frame_queue.put(jpeg_data.tobytes(), timeout=0.1)
                        self.stats['frames_processed'] += 1
                        consecutive_errors = 0
                        
                    except queue.Full:
                        logger.warning("帧队列已满，丢弃帧")
                        
                else:
                    logger.error("JPEG编码失败")
                    consecutive_errors += 1
                
                self.last_frame_time = current_time
                
                # 错误过多时重启捕获
                if consecutive_errors >= max_consecutive_errors:
                    logger.error("连续错误过多，尝试重新初始化捕获")
                    self._restart_capture()
                    consecutive_errors = 0
                
                # 定期输出状态
                if self.stats['frames_processed'] % 100 == 0:
                    self._log_stats()
                    
            except Exception as e:
                logger.error(f"流处理循环错误: {e}")
                consecutive_errors += 1
                time.sleep(0.01)
    
    def _restart_capture(self):
        """重启视频捕获"""
        try:
            if self.cap:
                self.cap.release()
            self.cap = self._initialize_capture()
            if self.cap:
                logger.info("视频捕获重启成功")
            else:
                logger.error("视频捕获重启失败")
        except Exception as e:
            logger.error(f"重启视频捕获失败: {e}")
    
    def _add_frame_info(self, frame, frame_count):
        """在帧上添加信息文本"""
        try:
            display_frame = frame.copy()
            
            # 添加帧计数和时间戳
            timestamp = datetime.now().strftime("%H:%M:%S")
            info_text = f"F:{frame_count} T:{timestamp}"
            
            # 在帧顶部添加信息栏
            cv2.rectangle(display_frame, (0, 0), (self.target_width, 16), (0, 0, 0), -1)
            
            # 添加文本
            cv2.putText(display_frame, info_text, (5, 12), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1)
            
            return display_frame
        except Exception as e:
            logger.error(f"添加帧信息失败: {e}")
            return frame
    
    def get_frame(self):
        """获取当前帧（非阻塞）"""
        try:
            frame = self.frame_queue.get_nowait()
            self.stats['frames_served'] += 1
            return frame
        except queue.Empty:
            return None
    
    def _log_stats(self):
        """记录统计信息"""
        elapsed = time.time() - self.stats['start_time']
        processed_fps = self.stats['frames_processed'] / elapsed
        served_fps = self.stats['frames_served'] / elapsed
        
        logger.info(
            f"状态: 处理 {self.stats['frames_processed']} 帧, "
            f"服务 {self.stats['frames_served']} 帧, "
            f"处理FPS: {processed_fps:.2f}, 服务FPS: {served_fps:.2f}, "
            f"错误: {self.stats['errors']}"
        )
    
    def get_stats(self):
        """获取统计信息"""
        elapsed = time.time() - self.stats['start_time']
        stats = self.stats.copy()
        stats['elapsed_time'] = elapsed
        stats['processed_fps'] = stats['frames_processed'] / elapsed if elapsed > 0 else 0
        stats['served_fps'] = stats['frames_served'] / elapsed if elapsed > 0 else 0
        stats['queue_size'] = self.frame_queue.qsize()
        return stats
    
    def stop_streaming(self):
        """停止视频流"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=3.0)
        if self.cap:
            self.cap.release()
        logger.info("视频流已停止")

def create_test_frame(width=320, height=172):
    """创建测试帧"""
    test_frame = np.zeros((height, width, 3), dtype=np.uint8)
    
    # 创建彩色渐变
    for i in range(width):
        color = int(255 * i / width)
        test_frame[:, i] = [color, color // 2, 255 - color]
    
    # 添加文本
    text = "Waiting for Video"
    text_size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0]
    text_x = (width - text_size[0]) // 2
    text_y = (height + text_size[1]) // 2
    
    cv2.putText(test_frame, text, (text_x, text_y), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    
    # 编码为JPEG
    ret, jpeg_data = cv2.imencode('.jpg', test_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return jpeg_data.tobytes() if ret else None

def create_error_frame(error_msg, width=320, height=172):
    """创建错误信息帧"""
    error_frame = np.zeros((height, width, 3), dtype=np.uint8)
    error_frame[:] = [0, 0, 100]  # 深蓝色背景
    
    # 添加错误信息
    lines = error_msg.split('\n')
    y_offset = 30
    for line in lines[:5]:  # 最多显示5行
        if y_offset < height - 10:
            cv2.putText(error_frame, line, (10, y_offset), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1)
            y_offset += 15
    
    ret, jpeg_data = cv2.imencode('.jpg', error_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return jpeg_data.tobytes() if ret else None

def create_screen_preview_frame():
    """创建屏幕预览帧"""
    try:
        with mss.mss() as sct:
            # 获取主显示器截图
            monitor = sct.monitors[1]
            screenshot = sct.grab(monitor)
            
            # 转换为numpy数组并调整大小
            frame = np.array(screenshot)
            # frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
            
            # 调整到合适的大小
            target_width = 320
            target_height = 170
            frame = cv2.resize(frame, (target_width, target_height))
            
            # 添加预览文本
            cv2.putText(frame, "屏幕预览", (10, 20), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.putText(frame, f"{monitor['width']}x{monitor['height']}", 
                       (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
            
            # 编码为JPEG
            ret, jpeg_data = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return jpeg_data.tobytes() if ret else None
            
    except Exception as e:
        logger.error(f"创建屏幕预览帧失败: {e}")
        return None

# 全局实例
streamer = None
screen_recorder = None
CURRENT_VIDEO = None
CURRENT_MODE = 'video'  # 'video' 或 'screen'
CONFIG_FILE = 'config.json'

def save_config():
    """保存配置到文件"""
    config = {
        'current_video': CURRENT_VIDEO,
        'current_mode': CURRENT_MODE,
        'target_fps': 25,
        'target_width': 320,
        'target_height': 172
    }
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error(f"保存配置失败: {e}")

def load_config():
    """从文件加载配置"""
    global CURRENT_VIDEO, CURRENT_MODE
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                CURRENT_VIDEO = config.get('current_video')
                CURRENT_MODE = config.get('current_mode', 'video')
                return config
    except Exception as e:
        logger.error(f"加载配置失败: {e}")
    return None

def get_available_videos():
    """获取可用的视频文件列表"""
    videos = []
    
    # 获取上传的视频
    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        if allowed_file(filename):
            video_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            video_info = get_video_info(video_path)
            if video_info:
                videos.append(video_info)
    
    # 获取录制的视频
    for filename in os.listdir(app.config['SCREEN_RECORDINGS_FOLDER']):
        if allowed_file(filename):
            video_path = os.path.join(app.config['SCREEN_RECORDINGS_FOLDER'], filename)
            video_info = get_video_info(video_path)
            if video_info:
                video_info['type'] = 'recording'
                videos.append(video_info)
    
    return sorted(videos, key=lambda x: x['filename'])

def get_available_windows():
    """获取可录制的窗口列表"""
    if screen_recorder:
        return screen_recorder.get_available_windows()
    return []

def signal_handler(sig, frame):
    """处理退出信号"""
    logger.info("接收到退出信号，正在关闭...")
    if streamer:
        streamer.stop_streaming()
    if screen_recorder:
        screen_recorder.stop_recording()
    sys.exit(0)

# 注册信号处理器
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# 加载配置
load_config()

@app.route('/')
def index():
    """主页显示状态信息和视频管理界面"""
    status = "运行中" if (streamer and streamer.initialized) or (screen_recorder and screen_recorder.initialized) else "未初始化"
    
    if CURRENT_MODE == 'video' and streamer:
        stats = streamer.get_stats()
        stats['mode'] = 'video'
    elif CURRENT_MODE == 'screen' and screen_recorder:
        stats = screen_recorder.get_stats()
        stats['mode'] = 'screen'
    else:
        stats = {'mode': 'none'}
    
    # 获取当前播放的视频信息
    current_video_info = None
    if CURRENT_VIDEO and os.path.exists(CURRENT_VIDEO):
        current_video_info = get_video_info(CURRENT_VIDEO)
    
    # 获取所有视频文件
    available_videos = get_available_videos()
    
    # 获取窗口列表
    available_windows = get_available_windows()
    
    return render_template('index.html',
                         status=status,
                         current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                         CURRENT_MODE=CURRENT_MODE,
                         streamer=streamer,
                         screen_recorder=screen_recorder,
                         stats=stats,
                         current_video_info=current_video_info,
                         available_videos=available_videos,
                         available_windows=available_windows,
                         format_file_size=format_file_size,
                         format_duration=format_duration)

@app.route('/switch_mode', methods=['POST'])
def switch_mode():
    """切换模式（视频/屏幕）"""
    global CURRENT_MODE, streamer, screen_recorder
    
    data = request.get_json()
    if not data or 'mode' not in data:
        return jsonify({"error": "缺少模式参数"}), 400
    
    mode = data['mode']
    if mode not in ['video', 'screen']:
        return jsonify({"error": "无效的模式"}), 400
    
    try:
        # 停止当前模式
        if CURRENT_MODE == 'video' and streamer:
            streamer.stop_streaming()
        elif CURRENT_MODE == 'screen' and screen_recorder:
            screen_recorder.stop_recording()
        
        # 切换到新模式
        CURRENT_MODE = mode
        
        # 初始化新模式
        if mode == 'screen' and not screen_recorder:
            screen_recorder = ScreenRecorder(target_fps=15)
        
        save_config()
        
        return jsonify({"success": True, "message": f"已切换到{mode}模式"})
        
    except Exception as e:
        return jsonify({"error": f"切换模式失败: {str(e)}"}), 500

@app.route('/upload', methods=['POST'])
def upload_video():
    """上传视频文件"""
    if 'file' not in request.files:
        flash('没有选择文件', 'error')
        return redirect(url_for('index'))
    
    file = request.files['file']
    
    if file.filename == '':
        flash('没有选择文件', 'error')
        return redirect(url_for('index'))
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        try:
            file.save(filepath)
            flash(f'文件 {filename} 上传成功！', 'success')
            logger.info(f"文件上传成功: {filename}")
        except Exception as e:
            flash(f'文件上传失败: {str(e)}', 'error')
            logger.error(f"文件上传失败: {e}")
    else:
        flash('不支持的文件类型', 'error')
    
    return redirect(url_for('index'))

@app.route('/play/<filename>')
def play_video(filename):
    """播放指定视频文件"""
    global streamer, screen_recorder, CURRENT_VIDEO, CURRENT_MODE
    
    # 检查文件是否存在
    filepath = None
    for folder in [app.config['UPLOAD_FOLDER'], app.config['SCREEN_RECORDINGS_FOLDER']]:
        test_path = os.path.join(folder, secure_filename(filename))
        if os.path.exists(test_path):
            filepath = test_path
            break
    
    if not filepath:
        flash('视频文件不存在', 'error')
        return redirect(url_for('index'))
    
    try:
        # 停止当前的流
        if CURRENT_MODE == 'video' and streamer:
            streamer.stop_streaming()
        elif CURRENT_MODE == 'screen' and screen_recorder:
            screen_recorder.stop_recording()
        
        # 切换到视频模式
        CURRENT_MODE = 'video'
        
        # 设置当前视频
        CURRENT_VIDEO = filepath
        save_config()
        
        # 初始化新的流
        streamer = VideoStreamer(filepath, target_fps=25)
        
        if streamer.initialized:
            flash(f'正在播放视频: {filename}', 'success')
        else:
            flash(f'无法播放视频: {filename}', 'error')
            
    except Exception as e:
        flash(f'播放视频失败: {str(e)}', 'error')
        logger.error(f"播放视频失败: {e}")
    
    return redirect(url_for('index'))

@app.route('/delete/<filename>')
def delete_video(filename):
    """删除视频文件"""
    # 检查文件在哪个文件夹
    filepath = None
    folder_type = None
    
    for folder in [app.config['UPLOAD_FOLDER'], app.config['SCREEN_RECORDINGS_FOLDER']]:
        test_path = os.path.join(folder, secure_filename(filename))
        if os.path.exists(test_path):
            filepath = test_path
            folder_type = 'uploaded' if folder == app.config['UPLOAD_FOLDER'] else 'recording'
            break
    
    if not filepath:
        flash('视频文件不存在', 'error')
        return redirect(url_for('index'))
    
    try:
        os.remove(filepath)
        
        # 如果删除的是当前播放的视频，停止流
        if CURRENT_VIDEO == filepath:
            global streamer, CURRENT_MODE
            if streamer:
                streamer.stop_streaming()
                streamer = None
            CURRENT_VIDEO = None
            CURRENT_MODE = 'video'
            save_config()
        
        flash(f'文件 {filename} 已删除', 'success')
        logger.info(f"文件删除成功: {filename}")
        
    except Exception as e:
        flash(f'删除文件失败: {str(e)}', 'error')
        logger.error(f"删除文件失败: {e}")
    
    return redirect(url_for('index'))

@app.route('/stop')
def stop_stream():
    """停止当前视频流"""
    global streamer, screen_recorder, CURRENT_VIDEO, CURRENT_MODE
    
    if CURRENT_MODE == 'video' and streamer:
        streamer.stop_streaming()
        streamer = None
        CURRENT_VIDEO = None
        flash('视频流已停止', 'success')
    elif CURRENT_MODE == 'screen' and screen_recorder:
        screen_recorder.stop_recording()
        flash('屏幕录制已停止', 'success')
    else:
        flash('没有正在运行的流', 'info')
    
    save_config()
    return redirect(url_for('index'))

@app.route('/jpeg_frame')
def jpeg_frame():
    """主视频流端点"""
    try:
        if CURRENT_MODE == 'video' and streamer and streamer.initialized:
            frame = streamer.get_frame()
            if frame:
                return Response(frame, mimetype='image/jpeg')
        
        elif CURRENT_MODE == 'screen' and screen_recorder and screen_recorder.initialized:
            frame = screen_recorder.get_frame()
            if frame:
                return Response(frame, mimetype='image/jpeg')
        
        # 返回等待帧
        test_frame = create_test_frame()
        if test_frame:
            return Response(test_frame, mimetype='image/jpeg')
        else:
            return "Server error", 500
            
    except Exception as e:
        logger.error(f"处理jpeg_frame请求时出错: {e}")
        error_msg = f"Server Error:\n{str(e)}"
        error_frame = create_error_frame(error_msg)
        if error_frame:
            return Response(error_frame, mimetype='image/jpeg')
        else:
            return "Server error", 500

@app.route('/screen_preview')
def screen_preview():
    """屏幕预览端点"""
    try:
        preview_frame = create_screen_preview_frame()
        if preview_frame:
            return Response(preview_frame, mimetype='image/jpeg')
        else:
            return "Preview not available", 404
    except Exception as e:
        logger.error(f"创建屏幕预览失败: {e}")
        return "Preview error", 500

@app.route('/status')
def status():
    """状态信息端点"""
    if CURRENT_MODE == 'video' and streamer and streamer.initialized:
        status_info = {
            "status": "running",
            "mode": "video",
            "video_file": streamer.video_path,
            "video_filename": os.path.basename(streamer.video_path),
            "target_fps": streamer.target_fps,
            "resolution": f"{streamer.target_width}x{streamer.target_height}",
            "queue_size": streamer.frame_queue.qsize(),
            "current_video": CURRENT_VIDEO,
            "available_videos": [v['filename'] for v in get_available_videos()]
        }
        status_info.update(streamer.get_stats())
    elif CURRENT_MODE == 'screen' and screen_recorder and screen_recorder.initialized:
        status_info = {
            "status": "running",
            "mode": "screen",
            "recording_mode": screen_recorder.recording_mode,
            "target_fps": screen_recorder.target_fps,
            "queue_size": screen_recorder.frame_queue.qsize(),
            "current_mode": CURRENT_MODE
        }
        status_info.update(screen_recorder.get_stats())
    else:
        status_info = {
            "status": "not_initialized",
            "current_mode": CURRENT_MODE,
            "current_video": CURRENT_VIDEO,
            "available_videos": [v['filename'] for v in get_available_videos()]
        }
    
    return jsonify(status_info)

@app.route('/stats')
def stats():
    """统计信息端点"""
    if CURRENT_MODE == 'video' and streamer:
        return jsonify(streamer.get_stats())
    elif CURRENT_MODE == 'screen' and screen_recorder:
        return jsonify(screen_recorder.get_stats())
    else:
        return jsonify({"error": "没有活动的流"})

@app.route('/api/videos')
def api_videos():
    """API: 获取视频列表"""
    videos = get_available_videos()
    return jsonify(videos)

@app.route('/api/windows')
def api_windows():
    """API: 获取窗口列表"""
    windows = get_available_windows()
    return jsonify(windows)

@app.route('/api/play', methods=['POST'])
def api_play():
    """API: 播放视频"""
    global streamer, screen_recorder, CURRENT_VIDEO, CURRENT_MODE
    
    data = request.get_json()
    if not data or 'filename' not in data:
        return jsonify({"error": "缺少文件名参数"}), 400
    
    filename = secure_filename(data['filename'])
    
    # 检查文件在哪个文件夹
    filepath = None
    for folder in [app.config['UPLOAD_FOLDER'], app.config['SCREEN_RECORDINGS_FOLDER']]:
        test_path = os.path.join(folder, filename)
        if os.path.exists(test_path):
            filepath = test_path
            break
    
    if not filepath:
        return jsonify({"error": "视频文件不存在"}), 404
    
    try:
        # 停止当前的流
        if CURRENT_MODE == 'video' and streamer:
            streamer.stop_streaming()
        elif CURRENT_MODE == 'screen' and screen_recorder:
            screen_recorder.stop_recording()
        
        # 切换到视频模式
        CURRENT_MODE = 'video'
        
        # 设置当前视频
        CURRENT_VIDEO = filepath
        save_config()
        
        # 初始化新的流
        streamer = VideoStreamer(filepath, target_fps=25)
        
        if streamer.initialized:
            return jsonify({"success": True, "message": f"正在播放 {filename}"})
        else:
            return jsonify({"error": "无法初始化视频流"}), 500
            
    except Exception as e:
        return jsonify({"error": f"播放视频失败: {str(e)}"}), 500

@app.route('/api/start_recording', methods=['POST'])
def api_start_recording():
    """API: 开始屏幕录制"""
    global screen_recorder, streamer, CURRENT_MODE
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "缺少参数"}), 400
    
    try:
        # 停止当前的视频流
        if streamer:
            streamer.stop_streaming()
            streamer = None
        
        # 切换到屏幕录制模式
        CURRENT_MODE = 'screen'
        
        # 创建或重新配置录制器
        fps = data.get('fps', 15)
        mode = data.get('mode', 'full_screen')
        
        if not screen_recorder:
            screen_recorder = ScreenRecorder(target_fps=fps, recording_mode=mode)
        else:
            screen_recorder.target_fps = fps
            screen_recorder.recording_mode = mode
        
        # 配置录制参数
        if mode == 'window' and 'window_title' in data:
            screen_recorder.set_window_title(data['window_title'])
        
        # 开始录制
        if screen_recorder.start_recording():
            save_config()
            return jsonify({"success": True, "message": "屏幕录制已开始"})
        else:
            return jsonify({"error": "无法开始录制"}), 500
            
    except Exception as e:
        return jsonify({"error": f"开始录制失败: {str(e)}"}), 500

@app.route('/api/stop_recording', methods=['POST'])
def api_stop_recording():
    """API: 停止屏幕录制"""
    global screen_recorder
    
    try:
        if screen_recorder:
            screen_recorder.stop_recording()
            return jsonify({"success": True, "message": "屏幕录制已停止"})
        else:
            return jsonify({"error": "没有正在运行的录制"}), 400
    except Exception as e:
        return jsonify({"error": f"停止录制失败: {str(e)}"}), 500

@app.route('/api/save_recording', methods=['POST'])
def api_save_recording():
    """API: 保存屏幕录制"""
    data = request.get_json()
    if not data or 'duration' not in data:
        return jsonify({"error": "缺少时长参数"}), 400
    
    duration = data['duration']
    if not isinstance(duration, (int, float)) or duration <= 0:
        return jsonify({"error": "无效的时长参数"}), 400
    
    try:
        if not screen_recorder:
            return jsonify({"error": "没有正在运行的录制"}), 400
        
        output_filename = data.get('filename')
        saved_path = screen_recorder.save_recording(duration=duration, output_filename=output_filename)
        
        if saved_path:
            return jsonify({
                "success": True,
                "message": "录制保存成功",
                "filename": os.path.basename(saved_path),
                "path": saved_path
            })
        else:
            return jsonify({"error": "保存录制失败"}), 500
            
    except Exception as e:
        return jsonify({"error": f"保存录制失败: {str(e)}"}), 500

def init_video_streamer(video_path, target_fps=25):
    """初始化视频流"""
    global streamer, CURRENT_VIDEO, CURRENT_MODE
    try:
        if not os.path.exists(video_path):
            logger.error(f"视频文件不存在: {video_path}")
            return False
            
        CURRENT_VIDEO = video_path
        CURRENT_MODE = 'video'
        streamer = VideoStreamer(video_path, target_fps=target_fps)
        save_config()
        return streamer.initialized
    except Exception as e:
        logger.error(f"初始化视频流失败: {e}")
        return False

if __name__ == '__main__':
    # 配置参数
    DEFAULT_VIDEO = None  # 初始时没有默认视频
    TARGET_FPS = 10
    HOST = '0.0.0.0'  # 监听所有网络接口
    PORT = 5000
    
    print("=" * 60)
    print("🎥 视频流服务器 - 带文件管理和屏幕录制功能")
    print("=" * 60)
    
    # 检查是否有保存的配置
    config = load_config()
    if config and config.get('current_video') and os.path.exists(config['current_video']):
        DEFAULT_VIDEO = config['current_video']
        CURRENT_MODE = config.get('current_mode', 'video')
        print(f"加载上次的模式: {CURRENT_MODE}")
        if CURRENT_MODE == 'video' and DEFAULT_VIDEO:
            print(f"加载上次播放的视频: {os.path.basename(DEFAULT_VIDEO)}")
    
    # 根据模式初始化
    if CURRENT_MODE == 'video' and DEFAULT_VIDEO:
        if init_video_streamer(DEFAULT_VIDEO, TARGET_FPS):
            print(f"✅ 视频流初始化成功: {os.path.basename(DEFAULT_VIDEO)}")
        else:
            print(f"❌ 视频流初始化失败")
            DEFAULT_VIDEO = None
    elif CURRENT_MODE == 'screen':
        screen_recorder = ScreenRecorder(target_fps=15)
        if screen_recorder.initialized:
            print(f"✅ 屏幕录制器初始化成功")
            if screen_recorder.start_recording():
                print(f"✅ 屏幕录制已开始")
        else:
            print(f"❌ 屏幕录制器初始化失败")
    
    # 获取可用视频数量
    available_videos = get_available_videos()
    print(f"📁 发现 {len(available_videos)} 个视频文件")
    
    print(f"🌐 服务器地址: http://{HOST}:{PORT}")
    print(f"📺 视频流地址: http://{HOST}:{PORT}/jpeg_frame")
    print(f"🖥️ 屏幕预览: http://{HOST}:{PORT}/screen_preview")
    print(f"📱 管理界面: http://{HOST}:{PORT}/")
    print("=" * 60)
    
    try:
        # 启动Flask服务器
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\n🔄 服务器关闭中...")
    except Exception as e:
        logger.error(f"服务器运行错误: {e}")
    finally:
        if streamer:
            streamer.stop_streaming()
        if screen_recorder:
            screen_recorder.stop_recording()
        print("✅ 服务器已关闭")