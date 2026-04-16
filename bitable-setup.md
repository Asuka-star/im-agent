# 多维表格接入说明

这份文档对应项目里的“整理待办并同步表格”能力。

## 1. 在飞书里准备一个多维表格

新建一个多维表格，至少建出这些字段：

- `任务`
- `负责人`
- `截止时间`
- `优先级`
- `状态`
- `备注`
- `会话ID`

字段类型不用一开始追求复杂，文本字段就够做 MVP 演示。

## 2. 拿到 `app_token` 和 `table_id`

打开这个多维表格后，浏览器地址里通常能看到类似结构：

```text
https://.../base/<app_token>?table=<table_id>
```

你需要的就是：

- `FEISHU_BITABLE_APP_TOKEN=<app_token>`
- `FEISHU_BITABLE_TABLE_ID=<table_id>`

## 3. 配置 `.env`

把这些配置填进去：

```env
FEISHU_BITABLE_ENABLED=true
FEISHU_BITABLE_APP_TOKEN=你的_app_token
FEISHU_BITABLE_TABLE_ID=你的_table_id

FEISHU_BITABLE_TITLE_FIELD=任务
FEISHU_BITABLE_OWNER_FIELD=负责人
FEISHU_BITABLE_DUE_DATE_FIELD=截止时间
FEISHU_BITABLE_PRIORITY_FIELD=优先级
FEISHU_BITABLE_STATUS_FIELD=状态
FEISHU_BITABLE_NOTES_FIELD=备注
FEISHU_BITABLE_SESSION_FIELD=会话ID
```

如果你的字段名不一样，把右侧改成你实际的字段名就行。

## 4. 确认飞书应用权限

你的自建应用需要能访问多维表格相关接口。

至少要确保：

- 应用已经安装到当前租户
- 应用有访问多维表格记录的权限

官方接口文档：

- 新增记录：
  - https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/create

## 5. 重新部署

```powershell
.\scripts\redeploy.ps1
```

## 6. 群里验证

先正常讨论几句，再发送：

- `@机器人 帮我把刚才讨论同步到表格`
- `@机器人 顺手整理一下任务并写到多维表格`

如果成功，群里会告诉你：

- 提取到了哪些任务
- 成功写入了多少条到多维表格

## 7. 失败时看哪里

查看容器日志：

```bash
docker logs -f feishu-im-agent-mvp
```

常见问题：

- `多维表格未配置`
  - 说明 `.env` 里没开或没填 `app_token/table_id`
- `Feishu API error`
  - 多半是权限、字段名或表 ID 不对
- 写入成功 0 条
  - 说明这一轮讨论没有抽取出明确任务
