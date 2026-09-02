# Piax2API

将 [piax.org](https://www.piax.org) 的聊天、图像和账号能力封装为 OpenAI 兼容 API。项目保留参考项目中的 function calling 格式转换逻辑，同时不使用代理池或活跃账号池。

## 功能

- OpenAI Chat Completions：`/v1/chat/completions`
- Anthropic Messages 兼容接口：`/v1/messages`
- 模型列表：`/v1/models`
- 图像生成：`/v1/images/generations`
- Token 管理和 dashboard：`/admin/dashboard`
- Piax 邮箱验证码注册/登录
- Worker 临时邮箱和 Outlook OAuth2/IMAP 两种邮箱模式
- 启动时签到、余额查询和定时保活
- 模型别名：客户端使用 `piax/<model-path>`，服务端自动移除前缀并选择对应 Piax agent

## 安装

需要 Python 3.11+。

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

复制并编辑环境变量：

```bash
copy .env.example .env
```

至少配置：

```env
API_KEY=example-key
TOKEN_FILE=data/tokens.json
PIAX_API_URL=https://piax-api.piax.org
```

## 启动

```bash
python main.py
```

默认监听 `http://127.0.0.1:8001`。启动阶段会加载 Token，并执行一次 Piax 每日签到和余额查询；之后按 `KEEP_ALIVE_MINUTES` 周期重复执行。

## API 鉴权

当 `.env` 设置了 `API_KEY` 时，请求需要携带：

```http
Authorization: Bearer example-key
```

将 `API_KEY` 留空可关闭鉴权（仅建议本地开发使用）。

## Chat Completions

```bash
curl http://127.0.0.1:8001/v1/chat/completions ^
  -H "Authorization: Bearer example-key" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"piax/gemini-2-5-pro\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}],\"stream\":false}"
```

Piax 上游使用 `chatStream` 接口。每个客户端请求生成新的 `conversationId`，完整消息历史会合并为一次 Piax query。function calling 仍由原有的工具提示、标记解析和 OpenAI 响应转换逻辑处理。

## 图像生成

```http
POST /v1/images/generations
Content-Type: application/json

{
  "model": "piax/gpt-image-2",
  "prompt": "a red panda reading a book",
  "n": 1,
  "size": "1024x1024",
  "response_format": "url"
}
```

服务端调用 Piax 的 `gptImageTaskSubmit` 接口，并返回 OpenAI 风格的 `created` 和 `data` 字段。

## 模型名称

模型列表接口返回带 `piax/` 前缀的模型。前缀用于区分多个上游，例如：

```text
piax/gpt5-6-terra
piax/gemini-2-5-pro
piax/claude-opus-5
piax/deepseek-v4-pro
```

客户端传入模型时可以使用带前缀的名称；上游请求会自动去掉 `piax/`。

## 注册账号

### Worker 临时邮箱

默认模式：

```env
EMAIL_MODE=worker
WORKER_DOMAIN=.....
EMAIL_DOMAIN=.....
ADMIN_PASSWORD=...
```

注册接口：

```http
POST /admin/register/start
```

Piax 注册流程是发送验证码后直接调用 `emailLogin`，不使用密码注册接口。

### Outlook 邮箱

设置：

```env
EMAIL_MODE=outlook
OUTLOOK_ACCOUNT_FILE=outlook_accounts.txt
OUTLOOK_TOKEN_URL=https://login.microsoftonline.com/common/oauth2/v2.0/token
OUTLOOK_IMAP_HOSTS=outlook.office365.com;imap-mail.outlook.com
OUTLOOK_IMAP_PORT=993
```

`outlook_accounts.txt` 每行格式：

```text
email----password----client_id----refresh_token
```

Outlook 模式使用 refresh token 获取 OAuth access token，再通过 IMAP XOAUTH2 读取 Piax 验证码。

## Dashboard

打开：

```text
http://127.0.0.1:8001/admin/dashboard
```

Dashboard 可查看 Token 状态、添加/删除/启用/禁用 Token、刷新签到余额和启动注册任务。

## 数据文件

- `data/tokens.json`：Piax Token 及账号信息
- `outlook_accounts.txt`：Outlook OAuth 账号（不要提交到版本库）
- `.env`：本地配置和密钥（不要提交到版本库）

## 注意事项

- 不要将 JWT、refresh token、临时邮箱管理密码或 API_KEY 提交到公开仓库。
- Piax 上游模型、agentId 和任务字段可能变化；如官网更新，需要同步模型映射和请求格式。

