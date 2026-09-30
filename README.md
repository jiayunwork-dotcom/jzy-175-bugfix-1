# 社区服务站选址后端

FastAPI + SQLite 的后端服务：给定平面上的居民点、候选站址与统一服务半径，求
**最少开站数的精确最优解**，支持必开站点、不可行诊断、版本管理、后台作业、超时/
取消、半径扫描与增量重解。对外只提供 HTTP 接口，无页面。

> 题目本质是 NP 难的**集合覆盖**。本系统不把贪心结果冒充成最优：只有搜索树被
> 完整证尽（或被合法下界关闭）时才返回 `proven_optimal=true`。

---

## 1. 目录与模块划分

| 模块 | 职责 |
| --- | --- |
| `app/coverage.py` | 欧氏距离与“居民点 → 可达候选站”覆盖关系构建 |
| `app/solver.py` | 精确求解：分支定界、合法下界、贪心初始解、超时/取消协作 |
| `app/store.py` | SQLite 持久化：项目、不可变版本、作业、覆盖关系缓存 |
| `app/jobs.py` | 后台作业调度（守护线程池）、取消、重启中断标记、去重 |
| `app/incremental.py` | 增删居民点后的增量重解（复用覆盖 + 旧解热启动 + 完整证优） |
| `app/services.py` | 字段校验、覆盖组装、单解/扫描引擎编排 |
| `app/main.py` | FastAPI HTTP 层 |
| `tests/` | 自动化测试（含可手工验算的诊所回归基准、随机对拍） |

---

## 2. 运行

```bash
# 构建镜像（基于 python:3.12-slim）
docker build -t siting:latest .

# 单容器提供全部接口，数据目录挂载到宿主机
docker run -p 8000:8000 -v "$PWD/data:/data" siting:latest
# 或：docker compose up
```

环境变量：

* `SITING_DATA_DIR`（默认 `/data`）：SQLite 所在目录，建议挂载。
* `SITING_DB_PATH`：可显式指定数据库文件路径。
* `SITING_WORKERS`（默认 `4`）：并行求解的工作线程数。

交互式文档：容器启动后访问 `http://localhost:8000/docs`。

本地开发（Python 3.12）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
pytest
```

---

## 3. HTTP 接口摘要

项目与版本：

* `POST /api/projects` `{name}`
* `GET  /api/projects/{project_id}`，`GET /api/projects`
* `POST /api/projects/{project_id}/versions`
  `{residents:[{id,x,y}], candidates:[{id,x,y}], label?, change_note?}`
* `GET  /api/versions/{version_id}`
* `GET  /api/projects/{project_id}/versions`（按版本号排序，便于对比）
* `POST /api/versions/{version_id}/derive`
  `{add_residents?, remove_resident_ids?, candidates?, change_note?}`
  生成新版本并记录 `parent_version_id`；旧版本与其方案原样保留。

作业：

* `POST /api/versions/{version_id}/solve`
  `{radius, forced_site_ids?, timeout?}` → `202 {job_id, submit_outcome}`
* `POST /api/versions/{version_id}/solve-incremental`（同上 body；用于派生出的版本）
* `POST /api/versions/{version_id}/sweep`
  `{radii:[...], forced_site_ids?, timeout?}`（半径可乱序，内部升序执行）
* `GET  /api/jobs/{job_id}`（状态、进度、结果）
* `GET  /api/versions/{version_id}/jobs`
* `POST /api/jobs/{job_id}/cancel`

`submit_outcome` 取值：`created`（新记录）/ `active`（已有运行中作业）/
`reused`（已完成结果直接复用）/ `rerun`（之前超时/取消/中断，**在同一条记录**上
重跑）。

作业 `status`：`queued | running | completed | timeout | cancelled | interrupted | failed`。

求解结果（`GET /api/jobs/{id}` 的 `result`）：

* 可行：`{feasible:true, site_ids, best_size, lower_bound, gap,
  proven_optimal, stop_reason, nodes_explored, max_depth}`
* 不可行：`{feasible:false, uncovered_resident_ids:[...], proven_optimal:true}`
  ——所有候选全开仍够不着时返回成功 HTTP 状态，但业务结果是失败并列出够不着的点。
* 进度（`progress`）：`nodes_explored`、`search_depth`、`lower_bound`、
  `best_size`、`best_site_ids`。

校验错误统一为 `400`，形如：

```json
{"error":{"code":"validation_error","errors":[{"field":"radius","message":"..."}]}}
```

覆盖的字段级错误：半径非正 → `radius`；居民点/候选址为空或坐标非法 →
`residents[i].x` 等；点名必开站不存在 → `forced_site_ids`（并列出未知编号）。

---

## 4. 精确性如何保证（最关键的部分）

求解器 `exact_set_cover` 做的是**集合覆盖的分支定界**：

1. **覆盖关系**：居民点 `p` 可达当且仅当存在开站 `s` 使欧氏距离
   `dist(p,s) <= radius`（边界包含）。
2. **支配剪枝**：若站 `a` 在去掉必开站后的剩余居民上覆盖的集合是站 `b` 的子集，
   且 `a` 非必开，则永不需要在 `b` 之外再开 `a`（安全，保最优）。
3. **下界**：贪心挑选“可达站集合两两不交”的居民点，每个都需要一座不同的站，
   故其数量是合法下界。任何时刻都有 `lower_bound <= 最优站数`。
4. **分支**：对最难（剩余可达站最少）的居民点，选取一个候选站做严格的二元分支：
   “开它”与“不开它”两个子树都搜索，因此**不会漏掉最优解**。搜索到的方案在返回
   前还会再独立校验一次全覆盖，杜绝“漏覆盖却报成功”。
5. **上界/热启动**：集合覆盖贪心给出第一个可行解（只用于剪枝，不决定最优标志）。
6. **何时才算最优**：搜索树完整证尽，或当前最优解大小已等于合法下界。超时/被取消
   一律 `proven_optimal=false`，同时交回手上最好可行解、下界与
   `gap = best_size - lower_bound`，`stop_reason` 标成 `timeout/cancelled`。

测试用 600+ 个随机小例与**暴力枚举**逐一对拍站数、可行性与不可行点列表。

### 三条最想防住的错，各自的防线

* **贪心被标成最优**：`proven_optimal` 只由“树证尽/下界闭合”决定，贪心仅作上界；
  超时显式为 `timeout`。
* **漏覆盖却报成功**：分支正确性（含 exclude 分支）+ 返回前对每个居民点复核覆盖；
  不可行时列出 `uncovered_resident_ids`，不会报 `feasible:true`。
* **增量与全量对不上**：增量只提供上界（热启动），仍跑完整证明；并有多 seed
  随机增删序列与冷解对拍（`tests/test_incremental.py`）。

---

## 5. 增量重解策略（含理由与回退条件）

高频操作是“在某版本上增删几个居民点再求一次”。采用的策略是：

> **复用覆盖关系 + 用上一版可行解热启动分支定界，但仍跑完整的最优性证明。**

具体地（见 `app/incremental.py`）：

* **覆盖关系复用**：候选址与半径不变时，保留下来的居民点其可达站集合逐字节相同，
  直接从父版本缓存复制；只对新增居民点做几何计算。派生版本的覆盖关系仍按新版本
  居民点顺序规整后落库缓存。
* **旧解复用**：父版本在同半径、同必开集合下的最优（或最好可行）方案，对所有保留
  居民点仍可行；新增居民点若覆盖不到则用贪心少量修补。把它作为分支定界的初始
  incumbent，能立刻得到一个很紧的上界，大幅减少搜索。
* **仍然证明最优**：热启动只影响上界，搜索树、下界与“证尽才算最优”的判据和冷解
  完全相同。所以增量给出的站数与对同一份数据从头求解严格一致；超时/取消时也和冷
  解一样诚实。

**何时退回全量（冷）重解：**

* 新版本的候选址集合发生变化（增删/替换候选址，支配结构随之改变）；
* 求解半径与父版本作业的半径不同（覆盖关系必须重建）；
* 父版本没有可用于该半径/必开集合的可行方案可热启动。

结果里带有 `incremental.{reused_coverage, warm_started_from_parent,
fell_back_to_cold, parent_version_id}`，便于核对到底走了哪条路。注意：即使没有
可热启动的父方案，搜索本身仍是完整的精确求解，只是少了热启动这层加速。

---

## 6. 作业、超时、取消与重启

* 求解在守护线程池后台运行，提交立即返回作业编号，多个作业并行互不干扰。
* 取消用协作式 `threading.Event`，搜索在分支节点间检查；排队中的作业被取消则直接
  置为 `cancelled` 并出队。
* 作业记录用条件更新 `queued -> running` 原子抢占，重复入队不会执行两次。
* 去重键为 `(版本, 半径, 排序后的必开集合, 类型)`；同版本重复求解不产生重复记录，
  完成的结果直接复用，超时/取消/中断的在原记录上重跑。
* **服务重启**：启动时把上次遗留的 `queued/running` 作业统一标记为 `interrupted`，
  不会一直挂着“运行中”；已完成作业的结果仍在 SQLite 中可取回。

---

## 7. 半径扫描的单调性

扫描内部按半径升序执行，并用上一个（较小半径）的最优解为下一步热启动（半径变大，
旧解仍可行）。因此站数阶梯关于半径单调不增——这既是业务直觉，也被
`tests/test_sweep.py` 断言。

---

## 8. 诊所回归基准（可手工验算）

`tests/conftest.py` 内置一个小例子：**11 个居民点 + 5 个候选诊所**。三个相距约
40 单位的居民组团（每团 3–4 人），每团内部一座诊所 `c1/c2/c3`，另有两座谁也够
不着的干扰站 `d1/d2`，半径 5。

* 每个组团的居民只可能被本团诊所照顾，三个组团两两不交 ⇒ 必开 `c1,c2,c3`，
  **最优恰好 3**，且可被分支定界证明（`lower_bound=best_size=3, gap=0`）。
* 删掉 `c2` 后，中间组团 `r5–r8` 全员够不着 ⇒ 不可行并列出这 4 个点。
* 强开干扰站 `d1`：解里必须包含它，站数不少于 3。
* 半径加大（5→10→50）：站数不增，半径 50 时一站全覆盖。

---

## 9. 已写入测试的关系

* 只调大半径，最优站数不会变多（扫描阶梯 & 单解）。
* 强开一座原本不在最优解里的站，站数不会少于原最优，且该站必在解中。
* 拿掉“所有最优解都离不开”的站 ⇒ 站数上升或变无解并列出漏点（诊所 `c2`）。
* 同一居民点坐标重复出现（不同 id）不影响站数。
* 超时作业：`lower_bound <= 真实最优 <= 当前最好`，且方案真实全覆盖。
* 增量与冷解在随机增删序列下站数一致、全覆盖一致（多 seed 对拍）。

测试入口：

```bash
pytest                 # 全量（其中含一个约需十几秒的精确网格例用于超时/取消）
pytest tests/test_clinic.py tests/test_validation.py tests/test_versions.py tests/test_sweep.py
```
