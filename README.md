# ZJMF 服务器监控与自动重启系统

基于5状态机架构的云服务器监控，支持自动重启、定时重启、每日重启上限、Webhook通知。

## 架构

### 5状态机

```
healthy ↔ suspect → down → rebooting → recovering → healthy
                                          ↘ down (恢复超时)
```

| 状态 | 含义 | 触发条件 |
|------|------|----------|
| `healthy` | 正常运行 | API返回状态为 `on` |
| `suspect` | 疑似异常 | 首次检测到非 `on` |
| `down` | 确认宕机 | 连续 `suspect_threshold` 次异常 |
| `rebooting` | 正在重启 | 触发 `hard_reboot` |
| `recovering` | 恢复中 | 重启指令发送成功，等待恢复 |

### 监控与重启分离

检测（Monitor）只负责判断健康状态和推进状态机，重启决策（Reboot Decision）独立判断是否执行重启。两者解耦，便于扩展。

## API调用

```
1. POST /v1/login_api?account=xx&password=xx    → 获取 JWT
2. GET  /v1/hosts?page=1&limit=100              → 获取产品列表
3. GET  /v1/hosts/:id/module/status?type=host   → 获取状态（on=正常）
4. PUT  /v1/hosts/:id/module/hard_reboot        → 硬重启
```

关键点：
- 登录参数通过 **query string** 传递，不是 body
- 获取状态必须传 `?type=host`
- 重启用 **hard_reboot**（硬重启）
- 服务器状态为 `on` 表示正常，其他值均视为异常

## 快速开始

### 1. 安装

```bash
pip install requests
```

### 2. 配置

编辑 `servers.json`：

```json
{
  "providers": [
    {
      "name": "heyunidc",
      "display_name": "核云",
      "api_base_url": "https://www.heyunidc.cn/v1",
      "api_account": "你的账号",
      "api_password": "你的API密钥"
    }
  ],
  "servers": [
    {
      "id": "4075",
      "name": "我的服务器",
      "ip": "1.2.3.4",
      "provider": "heyunidc",
      "check_method": "api_only",
      "enabled": true,
      "daily_reboot_limit": 3,
      "scheduled_reboot": "04:00"
    }
  ],
  "global_settings": {
    "check_interval": 300,
    "suspect_threshold": 2,
    "reboot_cooldown": 600,
    "recover_timeout": 300,
    "default_daily_reboot_limit": 3,
    "webhook_url": "",
    "webhook_type": "custom",
    "log_level": "INFO"
  }
}
```

### 3. 运行

```bash
# 单次检查（测试用，无服务器时自动发现）
python server_monitor.py --once

# 查看状态（自动发现+检测）
python server_monitor.py --status

# 持续监控（无服务器时自动发现）
python server_monitor.py

# 查看状态
python server_monitor.py --status

# 自定义检查间隔
python server_monitor.py --interval 60
python server_monitor.py --interval 60
```

## 配置说明

### providers

| 字段 | 说明 | 示例 |
|------|------|------|
| `name` | 服务商标识（servers引用用） | `heyunidc` |
| `display_name` | 显示名称 | `核云` |
| `api_base_url` | API基础URL | `https://www.heyunidc.cn/v1` |
| `api_account` | API账号 | 手机号或邮箱 |
| `api_password` | API密钥 | 后台生成的Key |

### servers

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `id` | 服务器ID（必填） | - |
| `name` | 显示名称 | - |
| `ip` | IP地址（ping用，可选） | 空 |
| `provider` | 对应provider的name | - |
| `check_method` | 检测方式 | `api_only` |
| `enabled` | 是否启用 | `true` |
| `daily_reboot_limit` | 每日重启上限（0=不限） | 全局默认 |
| `scheduled_reboot` | 定时重启（`HH:MM`） | 空 |

### global_settings

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `check_interval` | 检查间隔（秒） | `300` |
| `suspect_threshold` | 连续异常N次确认宕机 | `2` |
| `reboot_cooldown` | 重启冷却时间（秒） | `600` |
| `recover_timeout` | 重启后恢复等待超时（秒） | `300` |
| `default_daily_reboot_limit` | 默认每日重启上限 | `3` |
| `webhook_url` | 通知地址 | 空 |
| `webhook_type` | 通知类型 | `custom` |
| `log_level` | 日志级别（仅控制台输出） | `INFO` |

### 检测方式

| 方式 | 说明 | 适用场景 |
|------|------|----------|
| `api_only` | 只通过API检测（推荐） | 有API，最准确 |
| `ping_only` | 只通过ping | 无API |
| `ping_then_api` | 先ping再API确认 | 需快速筛选 |
| `api_then_ping` | 先API，失败降级ping | API可能不稳定 |

### Webhook通知类型

| 类型 | 说明 |
|------|------|
| `custom` | 通用JSON格式 |
| `dingtalk` | 钉钉机器人 |
| `wecom` | 企业微信机器人 |
| `telegram` | Telegram Bot |

## 状态转换与通知

以下状态转换会触发Webhook通知：

| 转换 | 通知级别 | 说明 |
|------|----------|------|
| suspect → down | 🚨 critical | 确认宕机 |
| down → rebooting | 🚨 critical | 触发重启 |
| rebooting → recovering | ⚠️ warning | 等待恢复 |
| recovering → healthy | ✅ info | 恢复成功 |
| recovering → down | 🚨 critical | 恢复超时 |

## 安全机制

1. **疑似阈值**：首次异常进入suspect，连续N次才确认DOWN，避免误判
2. **重启冷却**：两次重启之间至少间隔 `reboot_cooldown` 秒
3. **每日上限**：每台服务器每天最多重启 `daily_reboot_limit` 次
4. **恢复超时**：重启后超过 `recover_timeout` 秒未恢复，重新标记DOWN
5. **JWT自动刷新**：2小时过期，提前10分钟自动重新登录

## 手动测试API

```bash
# 登录
curl -X POST "https://www.heyunidc.cn/v1/login_api?account=你的账号&password=你的API密钥"

# 获取产品列表
curl -H "Authorization: JWT YOUR_TOKEN" "https://www.heyunidc.cn/v1/hosts?page=1&limit=10"

# 获取服务器状态（注意 type=host）
curl -H "Authorization: JWT YOUR_TOKEN" "https://www.heyunidc.cn/v1/hosts/4075/module/status?type=host"

# 硬重启
curl -X PUT -H "Authorization: JWT YOUR_TOKEN" "https://www.heyunidc.cn/v1/hosts/4075/module/hard_reboot"
```

## 日志示例

```
2026-05-09 22:00:00 [INFO] 配置加载：1 个服务商，0 个服务器，检查间隔 300s，疑似阈值 2次，每日重启上限 3次
2026-05-09 22:00:01 [INFO] [核云] 正在登录...
2026-05-09 22:00:02 [INFO] [核云] 登录成功
2026-05-09 22:00:03 [INFO]   [HEALTHY] 我的服务器 (ID:4075) status=on
2026-05-09 22:05:03 [INFO] [我的服务器] healthy → suspect (检测异常)
2026-05-09 22:10:03 [INFO] [我的服务器] suspect → down (确认宕机)
2026-05-09 22:10:03 [INFO] [我的服务器] down → rebooting (触发重启)
2026-05-09 22:10:03 [WARNING] [4075] 发送硬重启指令...
2026-05-09 22:10:05 [INFO] [4075] 硬重启指令已发送：成功
2026-05-09 22:10:05 [INFO] [我的服务器] rebooting → recovering (重启指令已发送)
2026-05-09 22:15:05 [INFO] [我的服务器] recovering → healthy (恢复成功)
```
