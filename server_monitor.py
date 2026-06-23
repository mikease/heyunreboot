#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZJMF 服务器监控与自动重启系统
5状态机架构：healthy → suspect → down → rebooting → recovering

API调用流程：
  1. POST /v1/login_api?account=xx&password=xx  → 获取 JWT
  2. GET  /v1/hosts?page=1&limit=100            → 获取产品列表
  3. GET  /v1/hosts/:id/module/status?type=host → 获取状态（on=正常）
  4. PUT  /v1/hosts/:id/module/hard_reboot      → 硬重启
"""

import requests
import time
import logging
import json
import os
import subprocess
import platform
import hashlib
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum


# ==================== 状态定义 ====================

class ServerState(Enum):
    """服务器5状态机"""
    HEALTHY = "healthy"        # 正常运行（状态为 on）
    SUSPECT = "suspect"        # 疑似异常（首次检测到非 on）
    DOWN = "down"              # 确认宕机（连续N次异常）
    REBOOTING = "rebooting"    # 正在重启（已发送重启指令）
    RECOVERING = "recovering"  # 重启后恢复中（等待状态回到 on）


# 状态转换规则：
# HEALTHY  → SUSPECT     : 检测到1次异常
# SUSPECT  → DOWN        : 连续 suspect_count 次异常
# SUSPECT  → HEALTHY     : 检测恢复正常
# DOWN     → REBOOTING   : 触发自动重启
# REBOOTING → RECOVERING : 重启指令发送成功
# RECOVERING → HEALTHY   : 检测到状态恢复为 on
# RECOVERING → DOWN      : 恢复超时，重新回到 DOWN（可能再次重启）

STATE_TRANSITION_LABELS = {
    ("HEALTHY", "SUSPECT"): "检测异常",
    ("SUSPECT", "DOWN"): "确认宕机",
    ("SUSPECT", "HEALTHY"): "虚惊一场",
    ("DOWN", "REBOOTING"): "触发重启",
    ("REBOOTING", "RECOVERING"): "重启指令已发送",
    ("RECOVERING", "HEALTHY"): "恢复成功",
    ("RECOVERING", "DOWN"): "恢复超时",
}


# ==================== 数据结构 ====================

@dataclass
class ProviderConfig:
    """服务商API配置（ZJMF魔方财务系统通用）"""
    name: str = ""                       # 服务商标识
    display_name: str = ""               # 显示名称
    api_base_url: str = ""               # API基础URL
    api_account: str = ""                # API账号
    api_password: str = ""               # API密钥
    jwt_token: str = field(default="", repr=False)
    jwt_expire_time: float = 0
    _login_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.name


@dataclass
class ServerConfig:
    """服务器监控配置"""
    id: str                              # 服务器ID
    name: str = ""                       # 显示名称
    ip: str = ""                         # IP地址（ping用，可选）
    provider: str = ""                   # 对应 provider name
    check_method: str = "api_only"       # ping_only / api_only / ping_then_api / api_then_ping
    ping_timeout: int = 5
    ping_count: int = 3
    enabled: bool = True
    # 每日重启上限（0=不限）
    daily_reboot_limit: int = 0
    # 定时重启（cron格式 "HH:MM"，如 "04:00" 表示每天凌晨4点重启）
    scheduled_reboot: str = ""


@dataclass
class ServerRuntimeState:
    """服务器运行时状态"""
    state: ServerState = ServerState.HEALTHY
    consecutive_failures: int = 0        # 连续失败次数
    consecutive_successes: int = 0       # 连续成功次数
    last_check_time: float = 0
    last_reboot_time: float = 0
    reboot_count_today: int = 0          # 今日重启次数
    reboot_date: str = ""                # 当前计数的日期（YYYY-MM-DD）
    last_status_value: str = ""          # 最后一次API返回的状态值
    state_changed_at: float = 0          # 当前状态的进入时间
    first_failure_at: float = 0          # 首次检测到失败的时间
    reboot_initiated_at: float = 0       # 重启发起时间


@dataclass
class GlobalSettings:
    """全局监控设置"""
    check_interval: int = 60             # 正常检查间隔（秒）
    suspect_threshold: int = 2           # 连续异常N次 → 确认DOWN
    reboot_cooldown: int = 900           # 重启冷却时间（秒），硬重启后需要较长恢复时间
    recover_timeout: int = 600           # 重启后恢复等待超时（秒），服务器启动较慢
    recover_check_interval: int = 60     # 恢复期快速轮询间隔（秒），重启后每分钟检测一次直到开机
    api_timeout: int = 60                # API请求超时（秒），status接口可能较慢
    default_daily_reboot_limit: int = 3  # 默认每日重启上限
    webhook_url: str = ""                # Webhook通知地址
    webhook_type: str = "custom"         # custom / dingtalk / wecom / telegram
    log_level: str = "INFO"


# ==================== 通知模块 ====================

class Notifier:
    """Webhook通知"""

    def __init__(self, settings: GlobalSettings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger

    def send(self, title: str, message: str, level: str = "info"):
        """发送通知，level: info/warning/critical"""
        if not self.settings.webhook_url:
            return

        webhook_type = self.settings.webhook_type

        try:
            if webhook_type == "dingtalk":
                payload = self._dingtalk_payload(title, message, level)
            elif webhook_type == "wecom":
                payload = self._wecom_payload(title, message, level)
            elif webhook_type == "telegram":
                payload = self._telegram_payload(title, message, level)
            else:
                payload = self._custom_payload(title, message, level)

            resp = requests.post(
                self.settings.webhook_url,
                json=payload,
                timeout=10,
            )
            if resp.status_code >= 300:
                self.logger.warning(f"Webhook通知发送失败：HTTP {resp.status_code}")
            else:
                self.logger.debug(f"Webhook通知已发送：{title}")

        except Exception as e:
            self.logger.warning(f"Webhook通知异常：{e}")

    def _custom_payload(self, title, message, level):
        return {
            "title": title,
            "message": message,
            "level": level,
            "timestamp": datetime.now().isoformat(),
        }

    def _dingtalk_payload(self, title, message, level):
        emoji = {"info": "✅", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": f"{emoji} {title}",
                "text": f"### {emoji} {title}\n\n{message}\n\n> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            },
        }

    def _wecom_payload(self, title, message, level):
        emoji = {"info": "✅", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
        return {
            "msgtype": "markdown",
            "markdown": {
                "content": f"{emoji} **{title}**\n{message}\n> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            },
        }

    def _telegram_payload(self, title, message, level):
        emoji = {"info": "✅", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
        return {
            "text": f"{emoji} *{title}*\n{message}",
            "parse_mode": "Markdown",
        }


# ==================== ZJMF API 客户端 ====================

class ZJMFClient:
    """
    魔方财务（ZJMF）API客户端
    严格按接口文档实现
    """

    # ZJMF系统中，服务器状态为 "on" 表示正常运行
    HEALTHY_STATUS = "on"

    def __init__(self, provider: ProviderConfig, logger: logging.Logger, api_timeout: int = 60):
        self.provider = provider
        self.logger = logger
        self.api_timeout = api_timeout

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"JWT {self.provider.jwt_token}"}

    def login(self, force: bool = False) -> bool:
        """
        登录API获取JWT
        POST /v1/login_api?account=xx&password=xx
        参数通过query string传递

        Args:
            force: 强制重新登录，忽略现有token（用于401后重试）
        """
        p = self.provider
        if not p.api_base_url or not p.api_account or not p.api_password:
            self.logger.error(f"[{p.display_name}] API配置不完整")
            return False

        # Token有效且非强制刷新则跳过
        if not force and p.jwt_token and time.time() < p.jwt_expire_time:
            return True

        with p._login_lock:
            # 双重检查
            if not force and p.jwt_token and time.time() < p.jwt_expire_time:
                return True

            url = f"{p.api_base_url}/login_api"
            params = {"account": p.api_account, "password": p.api_password}

            try:
                label = "强制重新登录" if force else "正在登录"
                self.logger.info(f"[{p.display_name}] {label}...")
                resp = requests.post(url, params=params, timeout=self.api_timeout)

                if resp.status_code == 200:
                    data = resp.json()
                    # 兼容多种返回结构
                    jwt = data.get("jwt") or data.get("data", {}).get("jwt")
                    if jwt:
                        p.jwt_token = jwt
                        # JWT 2小时有效，提前10分钟刷新
                        p.jwt_expire_time = time.time() + 7000
                        self.logger.info(f"[{p.display_name}] 登录成功")
                        return True
                    else:
                        self.logger.error(f"[{p.display_name}] 登录返回中无jwt：{data}")
                        return False
                else:
                    self.logger.error(
                        f"[{p.display_name}] 登录失败：HTTP {resp.status_code} - {resp.text[:200]}"
                    )
                    return False

            except Exception as e:
                self.logger.error(f"[{p.display_name}] 登录异常：{e}")
                return False

    def _is_auth_error(self, resp: requests.Response) -> bool:
        """判断响应是否为认证失败（JWT过期/无效）"""
        if resp.status_code in (401, 403):
            return True
        # 有些魔方财务系统返回200但内容表示认证失败
        if resp.status_code == 200:
            try:
                data = resp.json()
                msg = str(data.get("msg", "")).lower()
                if any(kw in msg for kw in ["token", "jwt", "expired", "unauthorized", "登录", "过期", "认证"]):
                    return True
            except Exception:
                pass
        return False

    def _request(self, method: str, url: str, retry_on_auth: bool = True, **kwargs) -> Optional[requests.Response]:
        """
        带认证重试的统一请求方法
        检测到401/JWT过期时自动强制重新登录并重试一次
        """
        if not self.login():
            return None

        headers = kwargs.pop("headers", {})
        headers.update(self._auth_headers())
        kwargs["headers"] = headers

        try:
            resp = requests.request(method, url, **kwargs)

            # 检测JWT过期，强制重新登录后重试一次
            if retry_on_auth and self._is_auth_error(resp):
                self.logger.warning(
                    f"[{self.provider.display_name}] JWT过期或无效，强制重新登录..."
                )
                # 清除旧token
                self.provider.jwt_token = ""
                self.provider.jwt_expire_time = 0

                if self.login(force=True):
                    headers.update(self._auth_headers())
                    resp = requests.request(method, url, **kwargs)

            return resp

        except Exception as e:
            self.logger.error(f"[{self.provider.display_name}] 请求异常：{e}")
            return None

    def get_hosts(self, page: int = 1, limit: int = 100) -> Optional[List[Dict]]:
        """
        获取产品列表
        GET /v1/hosts?page=1&limit=100

        实际返回结构:
        {
          "data": {
            "total": 1,
            "host": [{"id": 4075, "dedicatedip": "...", ...}],
            "domainstatus": {"Active": {"name": "已激活", ...}, ...}
          }
        }
        """
        url = f"{self.provider.api_base_url}/hosts"
        params = {"page": str(page), "limit": str(limit)}

        resp = self._request("GET", url, params=params, timeout=self.api_timeout)
        if resp is None:
            return None

        if resp.status_code == 200:
            data = resp.json()

            # 调试：打印原始响应结构
            self.logger.debug(f"[{self.provider.display_name}] /hosts 原始响应: {json.dumps(data, ensure_ascii=False)[:500]}")

            # ZJMF实际结构：data.host 是产品列表
            raw = data.get("data")

            if isinstance(raw, dict):
                # ZJMF实际结构：data.host 是产品列表
                hosts = raw.get("host") or raw.get("list") or []
            elif isinstance(raw, list):
                hosts = raw
            else:
                self.logger.warning(
                    f"[{self.provider.display_name}] 未知的 /hosts 响应结构: {type(raw).__name__}"
                )
                hosts = []

            # 确保每个元素是dict
            valid_hosts = [h for h in hosts if isinstance(h, dict)]

            total = raw.get("total", "?") if isinstance(raw, dict) else "?"
            self.logger.info(
                f"[{self.provider.display_name}] 获取到 {len(valid_hosts)} 个产品（总计 {total}）"
            )
            return valid_hosts
        else:
            self.logger.error(
                f"[{self.provider.display_name}] 获取产品列表失败：HTTP {resp.status_code}"
            )
            return None

    def get_status(self, host_id: str) -> Optional[str]:
        """
        获取服务器状态
        GET /v1/hosts/:id/module/status?type=host
        返回状态字符串（on=正常），失败返回None
        """
        url = f"{self.provider.api_base_url}/hosts/{host_id}/module/status"
        params = {"type": "host"}

        resp = self._request("GET", url, params=params, timeout=self.api_timeout)
        if resp is None:
            return None

        if resp.status_code == 200:
            data = resp.json()
            self.logger.info(
                f"[{host_id}] /status 原始响应: {json.dumps(data, ensure_ascii=False)[:300]}"
            )
            # 尝试从多种返回结构中提取状态
            # 可能的结构: {"data": {"status": "on"}} 或 {"data": {"power_status": "on"}} 等
            raw_data = data.get("data", {})
            if isinstance(raw_data, dict):
                status = (
                    raw_data.get("status")
                    or raw_data.get("state")
                    or raw_data.get("power_status")
                    or raw_data.get("power_state")
                )
            else:
                status = data.get("status") or data.get("state")
            return status
        else:
            self.logger.error(
                f"获取状态失败 [{host_id}]：HTTP {resp.status_code}"
            )
            return None

    def hard_reboot(self, host_id: str) -> bool:
        """
        硬重启
        PUT /v1/hosts/:id/module/hard_reboot
        """
        url = f"{self.provider.api_base_url}/hosts/{host_id}/module/hard_reboot"

        resp = self._request("PUT", url, timeout=self.api_timeout)
        if resp is None:
            return False

        if resp.status_code == 200:
            data = resp.json()
            msg = data.get("msg", "成功")
            self.logger.info(f"[{host_id}] 硬重启指令已发送：{msg}")
            return True
        else:
            self.logger.error(
                f"[{host_id}] 硬重启失败：HTTP {resp.status_code} - {resp.text[:200]}"
            )
            return False


# ==================== Ping 检测 ====================

def ping_host(ip: str, timeout: int = 5) -> bool:
    """Ping指定IP"""
    if not ip:
        return False

    if platform.system().lower() == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout * 1000), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout), ip]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        return result.returncode == 0
    except Exception:
        return False


def check_by_ping(ip: str, count: int = 3, timeout: int = 5) -> bool:
    """多次ping，全部失败才返回False"""
    if not ip:
        return True  # 无IP不判定为异常

    for _ in range(count):
        if ping_host(ip, timeout):
            return True
        time.sleep(1)
    return False


# ==================== 状态机引擎 ====================

class StateMachine:
    """
    5状态机引擎
    healthy ↔ suspect → down → rebooting → recovering → healthy
                                                  ↘ down (恢复超时)
    """

    def __init__(self, settings: GlobalSettings, notifier: Notifier, logger: logging.Logger):
        self.settings = settings
        self.notifier = notifier
        self.logger = logger
        self.clients: Dict[str, ZJMFClient] = {}
        self.runtimes: Dict[str, ServerRuntimeState] = {}

    def register(self, server: ServerConfig, client: ZJMFClient):
        """注册服务器"""
        self.clients[server.id] = client
        self.runtimes[server.id] = ServerRuntimeState()

    def _transition(self, server: ServerConfig, new_state: ServerState):
        """状态转换，带通知"""
        rt = self.runtimes[server.id]
        old_state = rt.state

        if old_state == new_state:
            return

        rt.state = new_state
        rt.state_changed_at = time.time()

        # 生成日志标签
        key = (old_state.value.upper(), new_state.value.upper())
        label = STATE_TRANSITION_LABELS.get(key, "")

        self.logger.info(
            f"[{server.name}] {old_state.value} → {new_state.value}"
            f"{' (' + label + ')' if label else ''}"
        )

        # 通知
        self._notify_state_change(server, old_state, new_state, label)

    def _notify_state_change(self, server: ServerConfig, old: ServerState, new: ServerState, label: str):
        """状态变更通知"""
        # 以下状态转换需要通知
        notify_transitions = {
            (ServerState.SUSPECT, ServerState.DOWN),
            (ServerState.DOWN, ServerState.REBOOTING),
            (ServerState.REBOOTING, ServerState.RECOVERING),
            (ServerState.RECOVERING, ServerState.HEALTHY),
            (ServerState.RECOVERING, ServerState.DOWN),
        }

        if (old, new) not in notify_transitions:
            return

        rt = self.runtimes[server.id]
        level_map = {
            ServerState.DOWN: "critical",
            ServerState.REBOOTING: "critical",
            ServerState.RECOVERING: "warning",
            ServerState.HEALTHY: "info",
        }
        level = level_map.get(new, "info")

        messages = {
            ServerState.DOWN: f"服务器确认宕机\n最后状态值：{rt.last_status_value}\n连续异常：{rt.consecutive_failures}次",
            ServerState.REBOOTING: f"正在执行自动重启\n今日已重启：{rt.reboot_count_today}次",
            ServerState.RECOVERING: "重启指令已发送，等待恢复...",
            ServerState.HEALTHY: f"服务器恢复正常\n宕机时长：{self._downtime_duration(rt)}",
        }

        msg = messages.get(new, label)
        self.notifier.send(f"[{server.name}] {label or new.value}", msg, level)

    def _downtime_duration(self, rt: ServerRuntimeState) -> str:
        """计算宕机时长"""
        if rt.first_failure_at <= 0:
            return "未知"
        duration = int(time.time() - rt.first_failure_at)
        if duration < 60:
            return f"{duration}秒"
        elif duration < 3600:
            return f"{duration // 60}分钟"
        else:
            return f"{duration // 3600}小时{(duration % 3600) // 60}分钟"

    def _check_daily_limit(self, server: ServerConfig) -> bool:
        """检查每日重启限制"""
        rt = self.runtimes[server.id]
        today = datetime.now().strftime("%Y-%m-%d")

        # 日期切换，重置计数
        if rt.reboot_date != today:
            rt.reboot_date = today
            rt.reboot_count_today = 0

        limit = server.daily_reboot_limit or self.settings.default_daily_reboot_limit
        if limit <= 0:
            return True  # 0=不限制

        if rt.reboot_count_today >= limit:
            self.logger.warning(
                f"[{server.name}] 今日已达重启上限（{rt.reboot_count_today}/{limit}），不再重启"
            )
            return False
        return True

    def _check_reboot_cooldown(self, server: ServerConfig) -> bool:
        """检查重启冷却时间"""
        rt = self.runtimes[server.id]
        elapsed = time.time() - rt.last_reboot_time
        if elapsed < self.settings.reboot_cooldown:
            remaining = int(self.settings.reboot_cooldown - elapsed)
            self.logger.debug(
                f"[{server.name}] 冷却中（剩余{remaining}s）"
            )
            return False
        return True

    def _is_scheduled_reboot_time(self, server: ServerConfig) -> bool:
        """检查是否到了定时重启时间"""
        if not server.scheduled_reboot:
            return False

        try:
            now = datetime.now()
            target = server.scheduled_reboot.strip()
            parts = target.split(":")
            target_hour = int(parts[0])
            target_minute = int(parts[1]) if len(parts) > 1 else 0

            # 在目标时间的±check_interval秒内触发
            target_minutes = target_hour * 60 + target_minute
            now_minutes = now.hour * 60 + now.minute
            diff = abs(now_minutes - target_minutes)
            # 允许的偏差（分钟），不超过检查间隔
            tolerance = max(1, self.settings.check_interval // 60)

            return diff <= tolerance

        except (ValueError, IndexError):
            self.logger.warning(f"[{server.name}] 定时重启配置无效：{server.scheduled_reboot}")
            return False

    # ==================== 核心检测与状态推进 ====================

    def check_health(self, server: ServerConfig) -> Optional[bool]:
        """
        检测服务器健康状态
        返回：True=健康，False=异常，None=检测失败
        """
        method = server.check_method
        client = self.clients.get(server.id)

        if method == "api_only":
            return self._check_api(server, client)
        elif method == "ping_only":
            return check_by_ping(server.ip, server.ping_count, server.ping_timeout)
        elif method == "ping_then_api":
            if not check_by_ping(server.ip, server.ping_count, server.ping_timeout):
                return False
            if client:
                result = self._check_api(server, client)
                if result is not None:
                    return result
            return True
        elif method == "api_then_ping":
            if client:
                result = self._check_api(server, client)
                if result is not None:
                    return result
            return check_by_ping(server.ip, server.ping_count, server.ping_timeout)
        else:
            self.logger.warning(f"[{server.name}] 未知检测方式：{method}，使用api_only")
            return self._check_api(server, client)

    def _check_api(self, server: ServerConfig, client: ZJMFClient) -> Optional[bool]:
        """
        通过API检测
        状态判断规则：
        - "on"       → 健康
        - "off"      → 异常（关机）
        - "unknown"  → 异常（服务器状态不可知，需要重启恢复）
        - None       → API调用失败，不确定
        - 其他值     → 视为异常
        """
        if not client:
            return None

        status = client.get_status(server.id)
        rt = self.runtimes[server.id]
        rt.last_status_value = str(status) if status is not None else "N/A"

        if status is None:
            return None  # API调用失败，不确定

        status_lower = status.lower() if isinstance(status, str) else ""

        if status_lower == "on":
            return True   # 明确健康
        else:
            return False  # off / unknown / 其他 → 均视为异常

    def advance_state(self, server: ServerConfig, health: Optional[bool]):
        """根据检测结果推进状态机"""
        rt = self.runtimes[server.id]
        rt.last_check_time = time.time()

        if health is True:
            # 检测正常
            rt.consecutive_failures = 0
            rt.consecutive_successes += 1

            if rt.state == ServerState.HEALTHY:
                pass  # 保持

            elif rt.state == ServerState.SUSPECT:
                # 虚惊一场，恢复
                self._transition(server, ServerState.HEALTHY)

            elif rt.state == ServerState.RECOVERING:
                # 重启后恢复成功
                rt.first_failure_at = 0
                self._transition(server, ServerState.HEALTHY)

            elif rt.state == ServerState.REBOOTING:
                # 还在重启中但已检测到正常（不太可能），视作恢复
                rt.first_failure_at = 0
                self._transition(server, ServerState.HEALTHY)

            elif rt.state == ServerState.DOWN:
                # DOWN状态但检测正常？可能是临时恢复，保持在DOWN让下一轮继续确认
                # 或者直接恢复，取决于策略。这里保守地转到suspect
                rt.consecutive_successes = 1
                self._transition(server, ServerState.SUSPECT)

        elif health is False:
            # 检测异常
            rt.consecutive_failures += 1
            rt.consecutive_successes = 0

            if rt.first_failure_at == 0:
                rt.first_failure_at = time.time()

            if rt.state == ServerState.HEALTHY:
                self._transition(server, ServerState.SUSPECT)

            elif rt.state == ServerState.SUSPECT:
                if rt.consecutive_failures >= self.settings.suspect_threshold:
                    self._transition(server, ServerState.DOWN)

            elif rt.state == ServerState.RECOVERING:
                # 恢复超时检查
                elapsed = time.time() - rt.state_changed_at
                if elapsed > self.settings.recover_timeout:
                    self.logger.warning(
                        f"[{server.name}] 恢复超时（{int(elapsed)}s > {self.settings.recover_timeout}s），"
                        f"重启后仍未恢复（status={rt.last_status_value}），重新标记为DOWN"
                    )
                    # 恢复超时意味着上次重启没起作用，重置冷却时间允许再次重启
                    rt.last_reboot_time = 0
                    self._transition(server, ServerState.DOWN)
                else:
                    remaining = int(self.settings.recover_timeout - elapsed)
                    self.logger.info(
                        f"[{server.name}] 恢复期等待中（{int(elapsed)}s/{self.settings.recover_timeout}s，"
                        f"剩余{remaining}s），status={rt.last_status_value}"
                    )

            elif rt.state == ServerState.REBOOTING:
                # 重启指令已发但还没恢复，等
                pass

            elif rt.state == ServerState.DOWN:
                # 仍在DOWN，检查是否需要再次重启
                pass

        else:
            # health is None - 检测失败（API不可用等）
            self.logger.warning(f"[{server.name}] 检测失败，保持当前状态 {rt.state.value}")

    def should_reboot(self, server: ServerConfig) -> bool:
        """判断是否应该执行重启"""
        rt = self.runtimes[server.id]

        # 状态不是DOWN则不需要重启
        if rt.state != ServerState.DOWN:
            return False

        # 冷却时间检查
        if not self._check_reboot_cooldown(server):
            return False

        # 每日重启上限
        if not self._check_daily_limit(server):
            return False

        return True

    def execute_reboot(self, server: ServerConfig) -> bool:
        """执行重启"""
        client = self.clients.get(server.id)
        if not client:
            return False

        rt = self.runtimes[server.id]
        self._transition(server, ServerState.REBOOTING)

        success = client.hard_reboot(server.id)
        if success:
            rt.last_reboot_time = time.time()
            rt.reboot_initiated_at = time.time()
            rt.reboot_count_today += 1
            self._transition(server, ServerState.RECOVERING)
        else:
            # 重启失败，回退到DOWN
            self._transition(server, ServerState.DOWN)

        return success

    def check_scheduled_reboot(self, server: ServerConfig) -> bool:
        """检查并执行定时重启"""
        rt = self.runtimes[server.id]

        if not self._is_scheduled_reboot_time(server):
            return False

        # 今天是否已经执行过定时重启（用日期+定时标志判断）
        today = datetime.now().strftime("%Y-%m-%d")
        scheduled_key = f"scheduled_reboot_{today}"
        if getattr(rt, scheduled_key, False):
            return False

        # 执行定时重启
        if not self._check_reboot_cooldown(server):
            return False

        self.logger.info(f"[{server.name}] 执行定时重启（{server.scheduled_reboot}）")
        success = self.execute_reboot(server)
        if success:
            setattr(rt, scheduled_key, True)
        return success

    def get_server_summary(self, server: ServerConfig) -> Dict:
        """获取服务器状态摘要"""
        rt = self.runtimes[server.id]
        return {
            "id": server.id,
            "name": server.name,
            "state": rt.state.value,
            "last_status": rt.last_status_value,
            "consecutive_failures": rt.consecutive_failures,
            "consecutive_successes": rt.consecutive_successes,
            "reboot_count_today": rt.reboot_count_today,
            "last_check": datetime.fromtimestamp(rt.last_check_time).isoformat() if rt.last_check_time else "never",
            "last_reboot": datetime.fromtimestamp(rt.last_reboot_time).isoformat() if rt.last_reboot_time else "never",
            "first_failure": datetime.fromtimestamp(rt.first_failure_at).isoformat() if rt.first_failure_at else "n/a",
        }


# ==================== 主监控器 ====================

class ZJMFMonitor:
    """
    ZJMF服务器监控主类
    监控与自动重启分离
    """

    def __init__(self, config_file: str = "servers.json"):
        self.config_file = config_file
        self.servers: List[ServerConfig] = []
        self.providers: Dict[str, ProviderConfig] = {}
        self.settings = GlobalSettings()
        self.notifier: Optional[Notifier] = None
        self.engine: Optional[StateMachine] = None
        self.logger: Optional[logging.Logger] = None

        self._setup_logging()
        self._load_config()
        _ = self.notifier  # ensure initialized
        _ = self.engine    # ensure initialized

    def _setup_logging(self):
        """配置日志（仅控制台输出，不写文件）"""
        self.logger = logging.getLogger("zjmf_monitor")
        if not self.logger.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(message)s",
                handlers=[
                    logging.StreamHandler(),
                ],
            )

    def _load_config(self):
        """加载配置"""
        if not os.path.exists(self.config_file):
            self.logger.warning(f"配置文件 {self.config_file} 不存在，创建示例...")
            self._create_example_config()
            return

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 全局设置
            gs = config.get("global_settings", {})
            self.settings = GlobalSettings(
                check_interval=gs.get("check_interval", 60),
                suspect_threshold=gs.get("suspect_threshold", 2),
                reboot_cooldown=gs.get("reboot_cooldown", 900),
                recover_timeout=gs.get("recover_timeout", 600),
                recover_check_interval=gs.get("recover_check_interval", 60),
                api_timeout=gs.get("api_timeout", 60),
                default_daily_reboot_limit=gs.get("default_daily_reboot_limit", 3),
                webhook_url=gs.get("webhook_url", ""),
                webhook_type=gs.get("webhook_type", "custom"),
                log_level=gs.get("log_level", "INFO"),
            )

            # 设置日志级别
            self.logger.setLevel(getattr(logging, self.settings.log_level.upper(), logging.INFO))

            # 服务商
            for pd in config.get("providers", []):
                p = ProviderConfig(**pd)
                self.providers[p.name] = p

            # 服务器
            for sd in config.get("servers", []):
                s = ServerConfig(**sd)
                if s.enabled:
                    self.servers.append(s)

            # 初始化通知器和状态机
            self.notifier = Notifier(self.settings, self.logger)
            self.engine = StateMachine(self.settings, self.notifier, self.logger)

            # 注册服务器
            for server in self.servers:
                provider = self.providers.get(server.provider)
                if not provider:
                    self.logger.error(f"服务器 {server.name} 的服务商 {server.provider} 未配置，跳过")
                    continue
                client = ZJMFClient(provider, self.logger, api_timeout=self.settings.api_timeout)
                self.engine.register(server, client)

            self.logger.info(
                f"配置加载：{len(self.providers)} 个服务商，"
                f"{len(self.servers)} 个服务器，"
                f"检查间隔 {self.settings.check_interval}s，"
                f"疑似阈值 {self.settings.suspect_threshold}次，"
                f"每日重启上限 {self.settings.default_daily_reboot_limit}次"
            )

        except Exception as e:
            self.logger.error(f"加载配置失败：{e}")
            raise

    def _create_example_config(self):
        """创建示例配置"""
        example = {
            "providers": [
                {
                    "name": "heyunidc",
                    "display_name": "核云",
                    "api_base_url": "https://www.heyunidc.cn/v1",
                    "api_account": "your_phone_or_email",
                    "api_password": "your_api_key",
                }
            ],
            "servers": [],
            "global_settings": {
                "check_interval": 60,
                "suspect_threshold": 2,
                "reboot_cooldown": 900,
                "recover_timeout": 600,
                "recover_check_interval": 60,
                "api_timeout": 60,
                "default_daily_reboot_limit": 3,
                "webhook_url": "",
                "webhook_type": "custom",
                "log_level": "INFO",
            },
        }

        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(example, f, ensure_ascii=False, indent=2)

        self.logger.info(f"已创建示例配置：{self.config_file}")

    # ==================== 自动发现 ====================

    def auto_discover(self) -> List[ServerConfig]:
        """从API自动发现服务器"""
        discovered = []
        existing_ids = {s.id for s in self.servers}

        for pname, provider in self.providers.items():
            client = ZJMFClient(provider, self.logger)
            hosts = client.get_hosts()

            if not hosts:
                continue

            for host in hosts:
                host_id = str(host.get("id", host.get("host_id", "")))
                if not host_id or host_id in existing_ids:
                    continue

                # 优先用 product_name（产品名称），其次 domain（主机名）
                host_name = host.get("product_name") or host.get("name") or host.get("domain") or f"Server-{host_id}"
                # ZJMF实际字段：dedicatedip
                host_ip = host.get("dedicatedip") or host.get("ip") or host.get("host_ip") or ""
                host_status = host.get("domainstatus", "")

                self.logger.info(
                    f"  发现产品: ID={host_id} IP={host_ip} "
                    f"名称={host_name} 状态={host_status}"
                )

                server = ServerConfig(
                    id=host_id,
                    name=host_name,
                    ip=host_ip,
                    provider=pname,
                    check_method="api_only",
                    enabled=True,
                )
                discovered.append(server)
                existing_ids.add(host_id)

                # 注册到状态机
                self.engine.register(server, client)

            self.logger.info(f"[{provider.display_name}] 发现 {len(discovered)} 个新服务器")

        return discovered

    # ==================== 运行入口 ====================

    def run_once(self):
        """运行一轮检查"""
        # 如果没有服务器，先自动发现
        if not self.servers:
            self.logger.info("没有配置服务器，尝试自动发现...")
            discovered = self.auto_discover()
            if discovered:
                self.servers.extend(discovered)
                self.logger.info(f"发现 {len(discovered)} 个服务器")
            else:
                self.logger.warning("未发现任何服务器")
                return

        self.logger.info("=" * 60)
        self.logger.info(f"开始检查 {len(self.servers)} 个服务器...")
        self.logger.info("=" * 60)

        for server in self.servers:
            # 1. 检测健康
            health = self.engine.check_health(server)

            # 2. 推进状态
            self.engine.advance_state(server, health)

            # 3. 判断是否需要重启
            if self.engine.should_reboot(server):
                self.engine.execute_reboot(server)

            # 4. 定时重启检查
            self.engine.check_scheduled_reboot(server)

            # 输出状态摘要
            summary = self.engine.get_server_summary(server)
            self.logger.info(
                f"  [{summary['state'].upper()}] {server.name} "
                f"(ID:{server.id}) status={summary['last_status']}"
            )

        self.logger.info("-" * 60)

    def run_loop(self, discover: bool = False):
        """持续监控循环"""
        discovered = None

        # 配置中没有服务器时，自动从API发现
        if not self.servers or discover:
            self.logger.info("正在自动发现服务器...")
            discovered = self.auto_discover()
            if discovered:
                self.servers.extend(discovered)
                self.logger.info(f"自动发现 {len(discovered)} 个服务器，已加入监控")
            else:
                if not self.servers:
                    self.logger.error("未发现任何服务器，请检查API配置或手动配置servers")
                    return

        all_count = len(self.servers)
        self.logger.info("=" * 60)
        self.logger.info("ZJMF 服务器监控启动")
        self.logger.info(f"监控数量：{all_count}")
        self.logger.info(f"检查间隔：{self.settings.check_interval}s")
        self.logger.info(f"疑似阈值：{self.settings.suspect_threshold}次连续异常")
        self.logger.info(f"每日重启上限：{self.settings.default_daily_reboot_limit}次")
        self.logger.info(f"恢复超时：{self.settings.recover_timeout}s")
        self.logger.info("=" * 60)

        while True:
            try:
                self.run_once()

                # 智能间隔：有服务器处于 recovering/rebooting/suspect 时用快速轮询
                active_states = {ServerState.RECOVERING, ServerState.REBOOTING, ServerState.SUSPECT}
                needs_fast_poll = any(
                    self.engine.runtimes.get(s.id, ServerRuntimeState()).state in active_states
                    for s in self.servers
                )

                if needs_fast_poll:
                    wait = self.settings.recover_check_interval
                    self.logger.info(f"恢复期快速轮询，等待 {wait}s ...")
                else:
                    wait = self.settings.check_interval
                    self.logger.info(f"等待 {wait}s ...")

                time.sleep(wait)

            except KeyboardInterrupt:
                self.logger.info("收到中断信号，退出")
                break
            except Exception as e:
                self.logger.error(f"监控循环异常：{e}", exc_info=True)
                time.sleep(self.settings.check_interval)

    def print_status(self):
        """打印所有服务器当前状态"""
        # 如果没有服务器，先自动发现
        if not self.servers:
            self.logger.info("没有配置服务器，尝试自动发现...")
            discovered = self.auto_discover()
            if discovered:
                self.servers.extend(discovered)

        print("\n" + "=" * 70)
        print(f"{'服务器':<20} {'ID':<8} {'状态':<12} {'API状态':<8} {'今日重启':<8}")
        print("-" * 70)

        for server in self.servers:
            summary = self.engine.get_server_summary(server)
            print(
                f"{summary['name']:<20} "
                f"{summary['id']:<8} "
                f"{summary['state']:<12} "
                f"{summary['last_status']:<8} "
                f"{summary['reboot_count_today']:<8}"
            )

        print("=" * 70 + "\n")


# ==================== CLI入口 ====================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="ZJMF 服务器监控与自动重启系统"
    )
    parser.add_argument("--config", default="servers.json", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只运行一次检查")
    parser.add_argument("--discover", action="store_true", help="自动发现服务器")
    parser.add_argument("--status", action="store_true", help="显示当前状态")
    parser.add_argument("--interval", type=int, default=None, help="覆盖检查间隔（秒）")

    args = parser.parse_args()

    monitor = ZJMFMonitor(config_file=args.config)

    if args.interval is not None:
        monitor.settings.check_interval = args.interval

    if args.status:
        # 需要先跑一轮检测才有状态
        monitor.run_once()
        monitor.print_status()
    elif args.once:
        monitor.run_once()
    else:
        monitor.run_loop(discover=args.discover)


if __name__ == "__main__":
    main()
