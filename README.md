# 多源数据采集编排平台

面向数据源接入、采集编排、内容解析、反爬对抗与数据校验的全链路数据采集平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集源管理 | `crawl_source` | 采集源 | 源标识、源名称、协议类型 |
| 种子管理 | `seed_config` | 采集种子 | 种子编号、目标URL模式、列表页XPath |
| 解析规则 | `parse_rule` | 解析规则 | 规则编号、目标字段名、选择器类型 |
| 频次控制 | `rate_limit` | 限流策略 | 策略编号、目标源、请求间隔 |
| 代理池调度 | `proxy_pool` | 代理节点 | 节点编号、代理地址、端口号 |
| 会话管理 | `session_mgr` | 采集会话 | 会话编号、关联源、登录状态 |
| 重试策略 | `retry_policy` | 重试策略 | 策略编号、目标源、最大重试次数 |
| 字段抽取 | `field_extract` | 抽取模板 | 模板编号、关联种子、字段名 |
| 反爬对抗 | `anti_spider` | 对抗策略 | 策略编号、目标源、反爬类型 |
| 渲染编排 | `page_render` | 渲染配置 | 配置编号、目标源、渲染引擎 |
| 数据校验 | `data_validate` | 校验规则 | 规则编号、目标字段、校验函数 |
| 增量游标 | `incre_cursor` | 增量标记 | 标记编号、关联种子、增量字段 |
| 去重过滤 | `dedup_filter` | 去重规则 | 规则编号、目标源、去重键列 |
| 格式转换 | `format_convert` | 转换配置 | 配置编号、源格式、目标格式 |
| 采集编排 | `crawl_schedule` | 采集计划 | 计划编号、关联种子、Cron表达式 |
| 结果落地 | `result_store` | 落地配置 | 配置编号、输出格式、存储路径 |
| 采集监控 | `crawl_monitor` | 监控指标 | 指标编号、关联源、成功率 |
| 内容抽取 | `content_extract` | 提取任务 | 任务编号、来源页面、内容类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 解析规则与字段抽取的模板批次

解析规则与字段抽取不再各自维护一套选择器判断：

- 选择器校验与匹配统一走 `backend/app/services/selector_validation.py`，
  历史规则的选择器语义与提示文案保持不变。
- 解析规则通过 `POST /api/parse_rule/{规则族}/template-batch` 按「解析版本 + 页面样本」
  整批产出字段抽取模板，写入跨模块落库表 `field_template_batch`，并把解析版本元数据
  写入 `parse_version_meta`；二者在同一事务提交，写入中断会回滚并保留上一份完整批次。
- 同一解析版本并发重算只会有一份批次处于「可消费」状态，其余标记为「已替换」。
- 字段抽取只消费解析规则发布的批次（`POST /api/field_extract/batch/{批次ID}/consume`），
  不再解释选择器，并按原规则版本登记输出类型。
- 生成模式：
  - `历史复现`：旧默认值、字段顺序原样保留，逐字段记录历史版本依据；
  - `统一生成`：字段集合、输出类型与空值规则与历史等价，逐字段写入历史版本映射。
  `GET /api/parse_rule/{规则族}/template-batch?rule_version=...` 可查看批次依据说明。

领域：数据工程
子方向：数据采集、爬虫与RPA
