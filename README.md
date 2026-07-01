# auto-register

`auto-register` 是一个基于 pydoll 的注册自动化项目，用于串联账号资料生成、邮箱验证码、手机号验证码、Codex OAuth 授权和账号导出流程。

项目的核心目标是把注册动作拆成可组合的节点：每个节点只负责一个明确操作，节点执行后返回状态，注册流程根据状态自动流转到下一个节点。

## 功能概览

- 使用 pydoll 启动 Chrome，并打开 `https://chatgpt.com/`。
- 自动生成账号资料：姓名、年龄、密码。
- 接入自部署 OutlookMail 邮箱服务。
- 支持 OutlookMail 临时邮箱模式和 Outlook 邮箱池模式。
- 自动填写邮箱，兼容注册弹窗和登录页邮箱表单。
- 支持创建初始密码页面 `/create-account/password`。
- 自动轮询邮箱验证码，提取 6 位数字验证码并提交。
- 支持资料页姓名、年龄填写。
- 接入 Codex OAuth 授权，提交 OAuth 回调地址到账号导出服务。
- 支持手机号验证流程。
- 支持 HeroSMS 和 SMSBower 短信服务。
- 启动本地 `localhost:1455` 回调服务，避免 OAuth 回调页因本地无服务而加载失败。
- 注册成功后打印账号摘要信息。

## 运行环境

- Python `>= 3.12`
- uv
- Google Chrome
- 可用的 OutlookMail 自部署服务
- 可用的 CPA 账号导出管理服务
- 如果流程需要手机号验证，还需要配置 HeroSMS 或 SMSBower

## 快速开始

1. 安装依赖：

```bash
uv sync
```

2. 创建本地配置文件：

```bash
cp config.toml.example config.toml
```

`config.toml` 已加入 `.gitignore`，可以安全写入真实密钥、邮箱服务地址和短信服务密钥。

3. 修改 `config.toml`。

最少需要确认这些配置：

- `[email_service.providers.outlook_mail]`
- `[account_export_service.providers.cpa]`
- `[sms_service]` 及对应短信服务配置
- `[http_service]` 代理、UA、超时时间

4. 启动注册流程：

```bash
uv run python main.py
```

也可以指定配置文件：

```bash
uv run python main.py --config /path/to/config.toml
```

## 注册流程

当前主流程在 `main.py` 中组装，节点顺序和分支如下：

```text
OpenChatGptTabNode
  -> FillEmailAndSubmitNode
      -> CreatePasswordNode
          -> WaitEmailVerificationCodeNode
      -> WaitEmailVerificationCodeNode
      -> WaitSmsVerificationCodeNode
  -> FillAboutYouNode
  -> SelectCodexAccountNode
      -> WaitEmailVerificationCodeNode
      -> AddPhoneNumberNode
      -> SubmitCodexConsentNode
  -> AddPhoneNumberNode
  -> WaitSmsVerificationCodeNode
  -> SubmitCodexConsentNode
```

关键分支说明：

- 邮箱提交后如果进入 `/email-verification`，直接等待邮箱验证码。
- 邮箱提交后如果进入 `/create-account/password`，先填写账号密码，再等待邮箱验证码。
- 邮箱验证码提交后可能进入资料页，也可能直接进入 ChatGPT 登录成功状态。
- Codex OAuth 选择账号后可能直接进入 consent 页面，也可能要求手机号验证。
- 短信验证码等待超时会调用短信服务回调取消交易，并按配置从 Codex 账号选择节点重试。
- OAuth consent 提交后，浏览器会跳转到 `http://localhost:1455/auth/callback?...`，程序读取该地址并提交给账号导出服务。

## 配置说明

完整示例见 `config.toml.example`。

### 账号配置

```toml
[account_service]
specified_password = ""
```

- `specified_password` 为空时，每次自动生成 12 位强密码。
- 不为空时，所有账号使用指定密码。

### HTTP 配置

```toml
[http_service]
default_timeout = 30
user_agent = ""
proxy_url = ""
```

所有外部服务共享同一个 HTTP session。这里可以统一配置超时时间、User-Agent 和代理。

### 注册流程配置

```toml
[register]
verification_code_wait_timeout = 60
phone_number_retry_attempts = 5
sms_verification_retry_attempts = 5
```

- `verification_code_wait_timeout`：邮箱验证码最长等待时间。
- `phone_number_retry_attempts`：手机号无效、已使用、仅支持 WhatsApp 等情况的换号重试次数。
- `sms_verification_retry_attempts`：进入短信验证码页后迟迟收不到验证码时，从 Codex 账号选择节点重试的次数。

### OutlookMail 邮箱服务

```toml
[email_service]
provider = "outlook_mail"

[email_service.providers.outlook_mail]
base_url = "https://your-outlook-mail.example.com"
admin_password = "your-outlook-mail-admin-password"
use_temp_email = false
```

初始化时会依次调用：

1. `POST /api/extension/login`
2. `GET launch_url`
3. `GET /api/csrf-token`

后续请求会自动携带 Cookie 和 `X-CSRFToken`。

#### 临时邮箱模式

```toml
use_temp_email = true

[email_service.providers.outlook_mail.temp_email]
provider = "cloudflare"
channel_id = "1"
domain = "temp-mail.example.com"
```

当前临时邮箱只支持 Cloudflare tempmail。临时邮箱没有移动分组 API，所以邮箱回调不会执行移动操作。

#### Outlook 邮箱池模式

```toml
use_temp_email = false

[email_service.providers.outlook_mail.outlook]
pool_group_id = 1
registered_group_id = 2
```

- `pool_group_id`：分配新邮箱时从这个分组获取账号。
- `registered_group_id`：注册成功后，把 Outlook 邮箱移动到这个分组。

## 短信服务

```toml
[sms_service]
provider = "hero_sms"
```

可选值：

- `hero_sms`
- `sms_bower`
- `smsbower`
- 留空表示不启用短信服务

HeroSMS 和 SMSBower 都会在服务内部轮询等待验证码。流程节点只负责调用短信服务，不再自己轮询。

### HeroSMS

```toml
[sms_service.providers.hero_sms]
base_url = "https://hero-sms.com/stubs/handler_api.php"
api_key = "your-hero-sms-api-key"
country_id = "31"
max_price = 0.05
verification_code_wait_timeout = 125
```

### SMSBower

```toml
[sms_service.providers.sms_bower]
base_url = "https://smsbower.page/stubs/handler_api.php"
api_key = "your-sms-bower-api-key"
country_id = "31"
min_price = 0.045
max_price = 0.055
verification_code_wait_timeout = 60
```

## 账号导出服务

当前默认使用 CPA：

```toml
[account_export_service]
provider = "cpa"

[account_export_service.providers.cpa]
base_url = "http://localhost:8317/v0/management"
secret_key = "your-management-secret"
```

接口调用：

- `GET /codex-auth-url?is_webui=true` 获取 OAuth 链接。
- `POST /oauth-callback` 提交浏览器最终跳转到的 redirect URL。
- 管理密钥通过请求头 `X-Management-Key` 发送。

## 项目结构

```text
account/          账号资料生成
account_export/   账号导出服务抽象和 CPA 实现
core/             配置、HTTP 服务、应用上下文、日志
email/            邮箱服务抽象和 OutlookMail 实现
register/         注册流程、pydoll 浏览器上下文、流程节点
sms/              短信服务抽象、HeroSMS、SMSBower
tests/            单元测试
main.py           程序入口和注册流程组装
```

## 日志

日志配置：

```toml
[logging]
level = "INFO"
use_colors = true
```

建议日常使用 `INFO`。排查节点细节时可以改成 `DEBUG`。

注册成功后，程序会打印账号摘要，包括邮箱、手机号、短信验证码、邮箱验证码、用户名、年龄和密码。

## 常见问题

### Chrome 已打开但 pydoll 连接失败

确认本机 Chrome 可正常启动，并且没有被系统权限、调试器或安全软件拦截。项目启动时会通过 pydoll 创建 Chrome，并修正 CDP websocket loopback 地址。

### 本地 OAuth 回调服务启动失败

程序会启动 `http://localhost:1455`。如果端口被占用，需要先释放该端口，否则 Codex OAuth 回调页可能无法正常加载。

### 收不到邮箱验证码

检查：

- OutlookMail 登录配置是否正确。
- `pool_group_id` 是否有可用邮箱。
- 临时邮箱 `provider/channel_id/domain` 是否正确。
- 邮件 sender 是否包含 `openai.com`。
- 邮件 subject 是否包含 `ChatGPT` 或 `OpenAI`。
- `verification_code_wait_timeout` 是否过短。

### 手机号不可用或收不到短信

检查：

- 短信服务 API key、国家编号和价格区间是否正确。
- `phone_number_retry_attempts` 是否足够。
- `sms_verification_retry_attempts` 是否足够。
- 号码是否只支持 WhatsApp。当前流程需要 SMS。

## 开发扩展

新增服务时优先实现现有抽象：

- 邮箱服务：继承 `email.email_service.EmailService`
- 短信服务：继承 `sms.sms_service.SmsService`
- 账号导出服务：继承 `account_export.account_export_service.AccountExportService`
- 注册节点：继承 `register.register_flow.RegisterNode`

新增注册节点后，在 `main.py` 的 `build_register_flow()` 中加入节点实例和状态流转规则即可。
