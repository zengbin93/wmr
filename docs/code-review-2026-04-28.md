# wmr 全盘代码审查报告(2026-04-28)

> 范围:`wmr/` 源码 + `tests/` + `examples/` + `pyproject.toml` + `.github/workflows/`
>
> 工具结论:`ruff check . / ruff format --check . / basedpyright` 全部 0 报错
> 0 警告;`pytest -m "unit or integration and not online"` 85 用例全绿,
> LocalManager 行+分支覆盖率 99%、utils 97%、base 100%(online 17% — 由
> 独立的 online-test job 覆盖)。
>
> 本报告按"**1) 冗余/重复逻辑** → **2) 工程优化建议** → **3) 细节问题与
> 隐患**"三段式给出,每条都标 **位置 + 影响 + 建议**,可作为后续 PR 的
> 待办清单。优先级标记:🔴 必修、🟡 建议、🟢 可选。

---

## 一、冗余 / 重复逻辑

### 1.1 🔴 `publish_weights` 与 `publish_returns` 在两个后端 4 处镜像重复

- **位置**:[wmr/local.py:314-353](wmr/local.py#L314-L353) /
  [wmr/local.py:397-435](wmr/local.py#L397-L435) /
  [wmr/online.py:369-406](wmr/online.py#L369-L406) /
  [wmr/online.py:451-488](wmr/online.py#L451-L488)
- **问题**:四个方法的 4 段流水线("标准化输入 → 过滤 latest_dt →
  排序去重 → 分批写入")结构完全同构,差异仅在:
  1. 数据列名(`weight` / `returns`)
  2. 过滤运算符(`>` 仅追加 / `>=` 允许覆盖)
  3. 是否在 batch 间打 heartbeat
  4. 写入语句(driver 占位符不同)
- **影响**:任何流水线层面的修复(例如下文 §3.4 的 latest_dt 直查优化)
  必须改 4 份;漏改一处会破坏双后端 parity。
- **建议**:抽取到 `BaseManager` 上的模板方法 + 子类钩子。例如
  ```python
  def _publish_dataframe(
      self, strategy, df, *, value_col, mode: Literal["append","upsert"],
      batch_size, heartbeat_each_batch
  ): ...
  ```
  子类只需实现 `_query_latest_dt(strategy, table)` 与
  `_insert_batch(table, batch)` 两个钩子,流水线下沉到基类。已有
  `tests/parity/test_parity.py` 可直接保护这次重构。

### 1.2 🟡 `get_strategy_weights` / `get_strategy_returns` WHERE 子句重复 4 份

- **位置**:[wmr/local.py:355-384](wmr/local.py#L355-L384) /
  [wmr/local.py:437-468](wmr/local.py#L437-L468) /
  [wmr/online.py:408-436](wmr/online.py#L408-L436) /
  [wmr/online.py:490-520](wmr/online.py#L490-L520)
- **问题**:`(sdt, edt, symbols)` 三段过滤逻辑在 4 处独立维护,
  `_format_for_db` / `_to_naive` 调用顺序同构。差异只是占位符与时间
  截断(returns 把 sdt / edt 截到 00:00:00 / 23:59:59)。
- **建议**:在每个后端内提取 `_build_query_filters(...) -> (sql_part, params)`
  内部辅助;长期可在 BaseManager 层做 dataclass `QueryRange`,统一
  日期截断与 symbols 归一化。

### 1.3 🟡 `set_meta` 在 LocalManager 内自实现 DELETE+INSERT,而 `_insert_or_replace` 工具未被复用

- **位置**:[wmr/local.py:262-283](wmr/local.py#L262-L283) vs
  [wmr/local.py:580-598](wmr/local.py#L580-L598)
- **问题**:`set_meta` 手写 `DELETE FROM metas WHERE strategy = ?` 然后
  `INSERT`;`_insert_or_replace` 已经是同语义工具,但只服务 weights /
  returns 批量场景。
- **建议**:让 `set_meta` 走 `_insert_or_replace("metas", df,
  key_cols=["strategy"])`,让 `add_tag` 走相同入口;这样 upsert 语义
  在 LocalManager 内部只有一个实现。

### 1.4 🟡 `online.summary` / `online.clear_strategy` 多次单独 `query_df` 取 count

- **位置**:[wmr/online.py:597-619](wmr/online.py#L597-L619) /
  [wmr/online.py:641-650](wmr/online.py#L641-L650)
- **问题**:summary 5 次往返 ClickHouse,clear_strategy 3 次。每次都是
  `SELECT count() FROM xxx FINAL`,可以合并为一条
  ```sql
  SELECT
      (SELECT count() FROM metas FINAL) AS metas,
      (SELECT count() FROM weights FINAL) AS weights,
      ...
  ```
- **影响**:summary 路径在监控 / 看板里高频调用;5 次 round-trip
  在跨机房链路下叠加几十毫秒延迟。
- **建议**:合成单条 SQL,iloc[0] 一次拿全部计数。

### 1.5 🟢 `test_online_basic.py` 与 `test_online_full.py` 场景部分重叠

- **位置**:`set_meta_overwrite_false_skips` / `update_strategy_status_invalid`
  / `publish_weights_appends` 等在两个文件里都有。
- **建议**:把 `test_online_basic.py` 全部并入 `test_online_full.py`,
  按"metas / weights / returns / tags / 运维"五段组织,避免读者不
  确定该看哪一份。

### 1.6 🟢 `wmr/local.py:_to_naive` / `_series_to_naive` 应放进 `utils.py`

- **位置**:[wmr/local.py:49-67](wmr/local.py#L49-L67)
- **问题**:这两个函数是对 `_ensure_timestamp` 的 naive 化封装,概念
  与 utils 中其他时间工具同级。它们只在 LocalManager 用——但放在 utils
  并不会泄漏 DuckDB 依赖,反而使时间工具集中。
- **建议**:迁移到 `wmr/utils.py`,LocalManager 直接 import。

---

## 二、工程优化建议

### 2.1 🔴 视图内部 SQL 缺 `FINAL`,`test_online_final_keyword` 漏检

- **位置**:[wmr/online.py:229-267](wmr/online.py#L229-L267) +
  [tests/unit/test_online_final_keyword.py:62-69](tests/unit/test_online_final_keyword.py#L62-L69)
- **现象**:`cs_latest_weights` / `ts_latest_weights` / `latest_weights`
  三个视图的 `CREATE VIEW` 主体内部对 `weights` / `metas` 的引用都
  **没有** `FINAL`;静态扫描器把整个含 `CREATE` 的字面量直接 `continue`
  跳过,所以也没报。

  ```sql
  CREATE VIEW IF NOT EXISTS {db}.cs_latest_weights AS
  WITH latest_dates AS (
      SELECT strategy, MAX(dt) AS latest_dt
      FROM {db}.weights GROUP BY strategy   -- ← 缺 FINAL
  )
  ...
  JOIN {db}.metas m ON w.strategy = m.strategy   -- ← 缺 FINAL
  WHERE m.weight_type = 'cs'
  ```

- **影响**:外层 `SELECT * FROM latest_weights FINAL` 中的 `FINAL` 只
  作用于 `latest_weights` 这个视图自身,**不会传播到视图内部子查询**。
  当 `metas` 表对同一 strategy 留有多份未合并的 part(set_meta 后短
  时间内或 ALTER UPDATE 后),JOIN 会产生笛卡尔积、weights 行被
  重复返回——这正是 `FINAL` 设计要避免的场景。
- **建议**:
  1. 视图内的 `FROM {db}.weights` / `FROM {db}.metas` 全部追加 `FINAL`;
  2. 修改 `tests/unit/test_online_final_keyword.py`,把 `CREATE VIEW`
     的 SELECT 主体也纳入扫描(可在 `_collect_sql_literals` 里识别
     `CREATE VIEW ... AS <select>` 拆分)。

### 2.2 🟡 `publish_weights` 用视图查 latest_dt,代价高于直查表

- **位置**:[wmr/local.py:323](wmr/local.py#L323) /
  [wmr/online.py:378](wmr/online.py#L378)
- **现象**:发布前过滤"仅追加" 时调用 `get_latest_weights(strategy)`,
  而该视图是 `ts_latest_weights UNION ALL cs_latest_weights`,内部
  `JOIN metas`、`GROUP BY symbol`、`WHERE weight_type=...`,代价不小。
  对比同方法在 `publish_returns` 中是直接
  ```sql
  SELECT symbol, MAX(dt) FROM returns WHERE strategy = ? GROUP BY symbol
  ```
  方式更轻、与 returns 表保持对称,反倒在 weights 上绕远路。
- **影响**:publish_weights 在 100 万行批量发布时,前置过滤的视图
  查询会显著占用 ClickHouse 调度资源(且每次 publish 都重复)。
- **建议**:替换为对 `weights` 表的直查
  `SELECT symbol, MAX(dt) FROM weights FINAL WHERE strategy = ? GROUP BY symbol`,
  与 publish_returns 对齐;`get_latest_weights` 仍服务用户查询。

### 2.3 🟡 `OnlineManager.heartbeat` 单次 publish 触发 4–N 次 mutation

- **位置**:[wmr/online.py:369-406](wmr/online.py#L369-L406) +
  [wmr/online.py:576-589](wmr/online.py#L576-L589)
- **现象**:`publish_weights` 中每个 batch 都调用 `heartbeat` 一次,
  额外两次 pre/post → N+2 次 `ALTER TABLE metas UPDATE heartbeat_time`。
  ClickHouse mutation 是异步重操作,会写新 part 并触发后台合并,高
  频心跳放大背景压力。
- **建议**:
  1. 流程优化:`publish_weights` 只在 begin / end 各调一次 heartbeat,
     batch 之间通过自增 batch_id 上报进度即可(可写到 stdout / 日志)。
  2. heartbeat 容错:目前 [online.py:582-589](wmr/online.py#L582-L589)
     `try/except + raise`,失败会中断 publish——心跳本质是观测信号,
     建议捕获异常仅打 error,不向上抛。
  3. 长期方案:把 heartbeat_time 拆到独立 `KeeperMap` / `EmbeddedRocksDB`
     表(O(1) 写),而不是对 ReplacingMergeTree 做 ALTER UPDATE。


### 2.5 🟡 CI 流水线 `typecheck needs lint`,本可并行

- **位置**:[.github/workflows/ci.yml:42](.github/workflows/ci.yml#L42)
- **问题**:`typecheck` 通过 `needs: lint` 串行依赖,但 ruff 与
  basedpyright 是无依赖关系的两套工具——并行能省一次 `setup-uv` +
  `uv sync` 的 60–90 秒。
- **建议**:`typecheck` 改为 `needs: []`(或干脆移除 `needs`),
  test / online-test / examples 同理(目前都 needs lint)。lint 失败
  时 GitHub Actions 也会照常显示其它 job 的失败,这恰好暴露所有问题
  而不是只看到 lint 一项。

### 2.6 🟡 缺 A1 签名对齐测试

- **位置**:`docs/code-quality.md` §4.6 / §6 明确要求 unit
  `test_signature_alignment` 用 `inspect.signature` 比对
  `BaseManager` 与 `czsc.traders.cwc`,但 tests/ 中实际不存在该用例。
- **影响**:任何对 cwc.py 上游签名的漂移都不会被 CI 拦截,违背"接口
  对齐 cwc"的项目核心约束。
- **建议**:加 `tests/unit/test_signature_alignment.py`,枚举
  `BaseManager.set_meta / publish_weights / publish_returns / add_tag /
  add_tags / list_tags / remove_tag / heartbeat / clear_strategy` 的
  参数顺序与默认值(去掉 `db` / `database` 后),与从 czsc 导入的
  函数对齐;对侧不可用时 `pytest.skip`。

### 2.7 🟡 `human.md` 中承诺的 verbose 模式未实现

- **位置**:仓库根 `human.md`(README 之外的隐性需求文档)
- **现状**:文件明确写"提供统一的 verbose 模式,默认 False;开启后
  loguru 充分展示执行细节",但代码中没有该开关。当前
  `LocalManager` / `OnlineManager` 的 `_logger` 字段已经接受外部
  logger,但日志 level 是常量 INFO,publish 流水线无论是否需要都打
  6+ 条日志。
- **建议**:
  1. 在两个 Manager 构造函数加 `verbose: bool = False` 参数;
  2. 非 verbose 时,流水线把"标准化 / 过滤 / 排序"细节降级到 DEBUG;
  3. verbose 时,额外输出输入行数、过滤前后行数差、单批耗时。
  4. 同步 `human.md` 与 README,标注完成状态。

### 2.8 🟢 `add_tags` 接口的 creator 字段被无视

- **位置**:[wmr/local.py:480-496](wmr/local.py#L480-L496) /
  [wmr/online.py:528-549](wmr/online.py#L528-L549)
- **现象**:`add_tag(creator=...)` 接收 creator,`add_tags` 强制写
  `"system"`,无视任何业务方传入。这是为了对齐 cwc.py 的入参签名
  (`Iterable[tuple[str,str]]`),但 docstring 没说明。
- **建议**:在两处 docstring 显式注明"批量写入时 creator 固定为
  `system`,如需指定 creator 请逐条调用 `add_tag`",避免使用方踩坑。
  长期可把 items 类型放宽为 `Iterable[tuple[str, str] | tuple[str, str, str]]`。

### 2.9 🟢 `add_tags` 返回值是"输入条数"而非"实际新增"

- **位置**:[wmr/local.py:482-496](wmr/local.py#L482-L496) /
  [wmr/online.py:528-549](wmr/online.py#L528-L549)
- **问题**:文档 "Returns: 写入条数" 容易被误读为"新增条数";输入
  100 条已存在的 (strategy, tag),返回 100 而不是 0。
- **建议**:docstring 措辞改成 "返回处理的输入条数(不区分新增 / 覆盖)"。

### 2.10 🟢 测试断言里大量出现"line N->M" 注释

- **位置**:[tests/integration/test_local_extra_branches.py](tests/integration/test_local_extra_branches.py)
  开头大段以 `line 60` / `line 99->101` 标注。
- **问题**:这些行号会随源码漂移失效(已经能看出几个不再对得上),
  日后 review 时很难校对。
- **建议**:用功能描述替换行号,例如"_to_naive 处理 NaT 路径"
  "`:memory:` 跳过 mkdir 路径"。

---

## 三、细节问题与隐患

### 3.1 🔴 `online.heartbeat` 失败会阻断 `publish_weights`

- **位置**:[wmr/online.py:582-589](wmr/online.py#L582-L589)
- **风险**:心跳是观测信号,不应阻断业务写入。当前实现在 ALTER
  UPDATE 失败(网络抖动 / 临时只读)时直接 raise,导致整批写入回滚
  风险(实际是 ClickHouse 已写入但调用方误以为失败)。
- **建议**:改为
  ```python
  try:
      self.client.command(...)
  except Exception as e:
      self._logger.error(f"心跳失败(已忽略): {e}")
  ```
  与 LocalManager 的非阻断行为对齐。

### 3.2 🔴 `mask_dsn_password` 在密码含 `@` / `:` 时可能脱敏失败

- **位置**:[wmr/utils.py:118-125](wmr/utils.py#L118-L125)
- **风险**:`urlparse("clickhouse://u:p@ss@host:9000/db")` 的 password
  字段在 stdlib 中实际是 `p`,而 `@ss` 被并入 host 段——脱敏字符串
  替换 `f":{parsed.password}@"` 命中第一个 `:p@`,但完整密码没全部
  抹掉,可能让运维误以为已脱敏。生产 DSN 出现 `@` / `:` 的情况
  虽不常见但合规要求里很关键。
- **建议**:
  1. 解析后用 `urlunparse(parsed._replace(netloc=netloc))`,但 netloc
     替换前对密码做 percent-decode 后再统配;
  2. 直接重建 netloc:`netloc = f"{user}:***@{host}:{port}"`,不再
     依赖 string replace。
  3. 加一个测试用例:密码含特殊字符 → repr 输出绝对不含原始密码任何
     片段。

### 3.3 🟡 `online.publish_*` 的 `dt` 没有转 `_format_for_db` 后再写入

- **位置**:[wmr/online.py:391-405](wmr/online.py#L391-L405)
- **现象**:LocalManager 的 publish 路径在 batch 写入前把 dt 转为
  naive Timestamp(对应 DuckDB TIMESTAMP);OnlineManager 的 publish
  路径直接把带 tz 的 Timestamp 交给 `client.insert_df`,clickhouse-connect
  内部会把 datetime64[ns, Asia/Shanghai] 序列化为 ClickHouse
  `DateTime('Asia/Shanghai')`,**当前能跑通,但 pandas 3.0 的
  datetime64[us, tz] 在某些 driver 版本下序列化结果不可预期**。
- **建议**:在写入前显式 `df["dt"] = df["dt"].dt.tz_convert(self._tz)`
  并在文档中标注"需要 clickhouse-connect ≥ 0.7";加一个 test 用
  pandas 3.0 + clickhouse-connect 当前版本验证写入往返一致。

### 3.4 🟡 `get_meta` 不存在策略时打 warning,导致级联 warning 噪声

- **位置**:[wmr/local.py:222-224](wmr/local.py#L222-L224) /
  [wmr/online.py:277-279](wmr/online.py#L277-L279)
- **现象**:`set_meta` / `update_strategy_status` / `clear_strategy` /
  `heartbeat` 都先调用 `get_meta` 探测存在性,不存在时先打一条 warning,
  这些方法本身又再打一条 warning——同一事件 2 条 warning,日志噪音大。
  尤其 `set_meta(overwrite=False)` 的"先查再写"是正常路径,不应该
  warning。
- **建议**:把 `get_meta` 的 "策略不存在" warning 降级为 DEBUG,
  让外层方法决定是否提示用户。`get_meta` 是工具方法,不应直接产生
  用户面向日志。

### 3.5 🟡 `VALID_STATUSES` / `VALID_WEIGHT_TYPES` 用 list 不用 frozenset / Enum

- **位置**:[wmr/base.py:30-34](wmr/base.py#L30-L34)
- **现象**:成员检测在 list 上是 O(n)。两元素无性能问题,但语义层面
  应表达"枚举",否则容易被新人 `VALID_STATUSES.append("xx")` 玷污。
  另外 `VALID_WEIGHT_TYPES` 在源码里实际无任何引用。
- **建议**:
  1. 用 `Final[frozenset[str]]` 或 `enum.StrEnum`;
  2. 检查 `VALID_WEIGHT_TYPES` 是否还需要——若 `set_meta` 不做校验
     就保留 list / docstring 即可,不必 export。

### 3.6 🟡 `online.get_strategies_by_status` 用 `if status:` 与 LocalManager 用 `if status is None` 不一致

- **位置**:[wmr/online.py:355-356](wmr/online.py#L355-L356) vs
  [wmr/local.py:300-302](wmr/local.py#L300-L302)
- **现象**:OnlineManager 把空字符串当成 "不过滤";LocalManager 严格
  用 `is None`。在 `status=""` 输入下两后端行为不一致——这违反 parity。
- **建议**:统一为 `if status is None:`(空字符串视作非法值,直接走
  WHERE,虽不会命中但不会"静默放弃过滤")。

### 3.7 🟡 `OnlineManager._dsn` 字段保存原始 DSN(含明文密码)

- **位置**:[wmr/online.py:129](wmr/online.py#L129)
- **风险**:`mgr._dsn` 私有属性虽然不会出现在 `__repr__`,但持续保留
  在内存中、被 `pickle` / `__dict__` 暴露,对长生命周期 manager 是
  泄漏面。
- **建议**:仅保留 `_dsn_parts`(已脱出 host/port/user/password 字段)
  和一个脱敏版 `_dsn_masked`;`_dsn` 改为
  `_dsn_masked = mask_dsn_password(dsn)`。如某些代码路径需要原始 DSN,
  可在 `connect()` 内重建。

### 3.8 🟡 `SELECT *` 在多处使用,与 schema 演进有耦合风险

- **位置**:`metas` / `weights` / `returns` / `tags` / 视图相关查询大量
  `SELECT *`(local.py 与 online.py 各 ~10 处)。
- **风险**:任何加列都会改变返回 DataFrame 的 columns;依赖列序的
  代码(尤其 parity 测试中 `df.iloc[0]["c"]` 用了别名是对的,但
  `_localize_dataframe_columns(df, ["dt","update_time"], ...)` 之类
  假设特定列存在)会受影响。
- **建议**:按表显式列出 SELECT 的列。可以在模块顶部定义
  `_METAS_COLS = ("strategy", "base_freq", ...)`,SELECT 与 INSERT
  共用,顺带消除"列顺序必须与表定义一致"的隐式约束(见 `_insert_or_replace`
  docstring 的告警)。

### 3.9 🟡 `clear_strategy` 双后端 DELETE 顺序与一致性

- **位置**:[wmr/local.py:559-562](wmr/local.py#L559-L562) /
  [wmr/online.py:634-638](wmr/online.py#L634-L638)
- **现象**:LocalManager 没有事务包裹;OnlineManager 是 4 个 lightweight
  delete 串行(ClickHouse 不支持跨表事务)。删除中途异常会留下
  "metas 没了但 weights 还在"的孤儿数据。
- **建议**:
  1. LocalManager 用 `c.begin() / c.commit() / c.rollback()`,DuckDB
     支持本地事务;
  2. OnlineManager 文档显式说明"非原子",建议先停止策略(set status
     = '废弃')再 clear;
  3. clear_strategy 完成后调用 `summary()` 输出剩余条数,作为运维
     验收。

### 3.10 🟢 `_localize_dataframe_columns` 的"原地修改"反模式

- **位置**:[wmr/utils.py:84-100](wmr/utils.py#L84-L100)
- **现象**:函数对传入 df 做列赋值并返回同一对象。当前所有调用方传
  入的都是新 DataFrame,无 `SettingWithCopyWarning` 风险,但函数命名
  与签名(返回 `DataFrame`)既像 in-place 又像新对象,容易让读者犹豫。
- **建议**:改名 `_apply_tz_inplace` 或者把返回值改为 `None`,
  明确表达 in-place 语义。

### 3.11 🟢 `coverage.xml` / `.coverage` 在仓库根目录但已被 gitignore

- **位置**:仓库根
- **现状**:文件存在但 git 忽略——本地跑 pytest 的产物,不会被提交。
  但 GitHub clone 后会因为缺这两个文件让 codecov 本地校验报错。
- **建议**:
  1. 在 `Makefile` / `justfile` 里加 `clean: rm -f .coverage coverage.xml`;
  2. README 测试章节加一行说明:"测试会在仓库根生成 .coverage /
     coverage.xml,被 gitignore 忽略"。

### 3.12 🟢 `test_local_metas.test_set_meta_overwrite_true_updates_but_keeps_create_time` 用 `time.sleep(1.1)`

- **位置**:[tests/integration/test_local_metas.py:73-75](tests/integration/test_local_metas.py#L73-L75)
- **现象**:测试需要让 update_time 跨秒。`sleep(1.1)` 会让该用例本身
  超过 1.1s。同样的 sleep 在 weights / online 测试里出现 4 次,合计
  约 6 秒纯等待。
- **建议**:把 metas 的 update_time / heartbeat_time 切到亚秒精度
  (DuckDB 用 `TIMESTAMP_MS`,ClickHouse 用 `DateTime64(3, 'Asia/Shanghai')`),
  测试改成 `sleep(0.05)` 或纯断言"after >= before"——既贴近"心跳"
  的实时语义,也大幅缩短测试时间。


### 3.15 🟢 `loguru` 直接当 logger 使用,但允许注入

- **位置**:两个 Manager 的 `__init__(logger=loguru.logger)`
- **现象**:默认 `loguru.logger` 是全局 sink。注入测试用 logger 时
  必须传入(目前测试中都未注入,所以日志噪音直接走 loguru 默认 sink)。
- **建议**:在 conftest.py 里做一个 `pytest_configure` hook,把
  loguru 的默认 sink 重定向到 `caplog`,既能断言日志,也能压制 stdout
  噪音。详见 `loguru` 文档 "compatibility with std logging"。

---

## 四、整体评分

| 维度       | 评分    | 备注 |
|------------|---------|------|
| 代码风格    | ⭐⭐⭐⭐⭐ | ruff / format 全绿,docstring 风格统一 |
| 类型注解    | ⭐⭐⭐⭐⭐ | basedpyright standard 0 错误,`Any` 用得克制 |
| 测试质量    | ⭐⭐⭐⭐☆ | LocalManager 99% 行+分支,但 A1 签名对齐缺失,扫描器漏检视图 |
| 代码简洁性  | ⭐⭐⭐☆☆ | 4 段流水线镜像、SELECT * 分散是主要拖分项 |
| 工程实践    | ⭐⭐⭐⭐☆ | CI / 镜像源 / 双后端 fixture 设计干净;`__init__` 与 verbose 缺口 |

---

## 五、推荐 PR 拆分顺序

按"修复正确性 → 减重 → 锦上添花"顺序提交,每步都不破坏现有验收:

1. **PR-1(🔴)** §2.1 视图 FINAL + 扫描器修补 + §3.1 heartbeat 容错 +
   §3.2 mask_dsn_password 加固 + §2.4 `__init__` 兜底
2. **PR-2(🔴)** §1.1 publish 流水线模板方法重构 + §3.6 status 过滤
   parity + §2.6 A1 签名对齐测试
3. **PR-3(🟡)** §2.3 heartbeat 频率优化 + §2.2 latest_dt 直查 + §1.4
   summary / clear 合成查询 + §3.4 get_meta warning 降级
4. **PR-4(🟢)** §2.7 verbose 模式 + §3.12 亚秒精度 + §3.10 `_localize`
   重命名 + §1.5 测试合并 + §1.6 _to_naive 迁移 + §3.14 online_quickstart

每个 PR 都附 changelog 一行(对照 docs/code-quality.md §5.3 要求),
保持现有 90% 覆盖门槛与 ruff/format/basedpyright 的硬指标不退化。
