# Trace 步序对提取

`trace_to_pairs.py` 是 PLC 工作流的运行数据组件。将它和 `omron_trace.py` 放在工作流根目录，复用工程的 `trace_jump_analyzer.py`、工位工具、动作证据读取和变量过滤规则。

```bash
python trace_to_pairs.py --trace "采集/OmronTrace" --run "outputs/本次运行" --pattern "m_nNextAutoStep[*]"
python trace_to_pairs.py --trace "采集/Scope.trace" --run "outputs/本次运行"
```

`--trace` 接受 TwinCAT Scope `.trace`、一张 Sysmac CSV/TSV/TXT 表，或同次采集的分卷目录。目录读取直接子文件，按表内 `Index` 顺序合并。表头需要 `Index`、`RawTime` 和带 IEC 整型后缀的变量列，例如 `m_nNextAutoStep[37]:INT`。提取时保留完整变量名和下标。`--pattern` 支持工作流的 glob 和 `re:` 全匹配。默认处理所有整型通道，可用模式限定步序变量。

重复且字节相同的分卷只计一次。配置或整型变量列不一致、有冲突的索引重叠、或采集起始时间不一致时，程序会报告涉及的文件。缺号与空值会切断对应的观测区间。分卷边界的索引连续时，边界两侧也参与跳变提取。

精确 `RawTime` 以纳秒解析，再转换为相对毫秒。分卷时间因科学计数法被压缩或重复时，程序从有精确时间的样本估计采样周期，按索引插值或外推。若整组都缺乏足够精度的时间戳，传入已经核实的 `--sample-interval-ms`。`SamplingInterval` 配置字段的数值可能以 PLC 任务周期为单位。时长统计以可观测的起始步停留时间为准，观测中途开始时从首个有效样本计时。

## 工作流接口

- `generate(trace_path, run, source=None, proc_name=None, ..., pattern=None, sample_interval_ms=None)` 接收文件或目录，直接在 `run` 写入 `trace_collection_pairs.csv`、`trace_pairs_review.csv`、`trace_pairs_summary.json` 及跳变、恒值段、变量、周期审计。动作变量和步注释仍从本次 run 的源码证据补充。
- `trace_collection_pairs.csv` 保留起止标签、动作证据、次数和中位数、最短、最长时长列，新增 `估算时长次数`。汇总中的 `trace_input` 记录分卷顺序、索引范围、时间来源和缺号。
- `load_trace_duration_map(run)` 返回由完整起止标签组成的键和中位数时长。`load_trace_timing(run)` 还返回包含估算时长的标签对，供 Excel 标记。
- 工作流应把同一个 `--trace` 文件或目录传入 `generate()`，同时传递本次变量模式。可用 `omron_trace.trace_input_digest()` 给目录内的 Trace 文件名和文件内容生成指纹。单文件指纹沿用文件内容 SHA-256。
