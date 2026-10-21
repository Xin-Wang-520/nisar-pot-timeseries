# Run 1.5: keep DHDH products / 保留 DHDH 数据

运行顺序：**Run 1（DEM）→ Run 1.5（筛选数据）→ 原 Run 2（AHH 30000）→ Run 3……**

仅扫描 `data/` 顶层，按标准 NISAR RSLC 文件名的四字母极化代码筛选。
默认保留 `DHDH`；其他配置及其 H5、XML、KML、PNG、QA PDF、QA H5、CSV、YAML
等同一产品的配套文件，一起移入指定的归档子目录（默认 `data/not_DHDH/`）。
不改 H5 内容、不读取影像像素，不扫描或恢复已有子目录中的文件。
下方命令按用户指定，将新归档文件加入已有 `data/before_20260629/`，其中原有文件不修改、
同名目标不覆盖。下载脚本等非产品文件也保留。

```bash
conda activate gmtsar_nisar_master
cd /data2/xinw/landsides/nepal_landsides/NISAR/T48D

# 不带 1：只预览，不创建目录、不移动文件
python3 run1.5_nisar_select_dhdh.py \
  --data "$PWD/data" --archive-name before_20260629

# 带 1：正式移动；发现同名目标文件会停止，避免覆盖
python3 run1.5_nisar_select_dhdh.py 1 \
  --data "$PWD/data" --archive-name before_20260629

# 检查保留数据的实际参数
python3 run0_nisar_check_stack.py --data "$PWD/data" --start-date 20260629

# 按原流程转换，不需要之前的混合模式兼容步骤
bash run2_nisar_preprocess.sh 1 \
  --data "$PWD/data" --out "$PWD" --scale 30000 --jobs 5
```

本例将保留 4 期 `4005 / DHDH`：

```text
20260629
20260723
20260816
20260909
```

归档 3 期 `2005 / QPDH`：20260711、20260828、20260921。
这是将文件移动到子目录，不是删除影像；归档中有 `run1_5_moves_*.tsv` 移动记录。
每次执行前检查全部目标文件是否冲突，支持筛选完成后重复运行。
若执行中途发生 I/O 错误，保留已完成的移动和记录，并报出日志路径。
若 `data/` 顶层只剩子目录、没有主 RSLC H5，脚本报错停止，不会自动到归档里找数据。

震前速度场：截至 20260816，只有 20260629、20260723、20260816 三期，两个间隔均为 24 天。
如果振幅变化也限定为本次保留的 DHDH，8 月 26 日事件最近的同配置前后影像为
20260816 → 20260909。20260828 已在归档目录，不参与顶层数据的常规处理。

筛选极化代码不等于验证频率、采样和带宽；请保留 Run 0/Run 2 的检查。
这批已检查的 T48D DHDH 均为 4005，距离向采样约 3.12 m。

## English

Run 1.5 is a standard-library-only file organizer. It keeps DHDH RSLC products
in the top level of `data/` and moves other polarization configurations and
all matching sidecars into the selected archive (`data/not_DHDH/` by default).
The commands above explicitly select the user's existing `before_20260629/`
directory as the destination. Existing files there are preserved and are not
searched; H5 contents remain unchanged.

Omit positional `1` for a read-only preview. Add `1` to move files. All name
collisions are checked before moving, and destinations are never intentionally
overwritten. A TSV move journal is saved in the archive. Subsequent runs leave
the completed selection unchanged. An interrupted move can be inspected using
its journal; no automatic deletion or restoration is attempted.

Continue with the original Run 2 (fixed AHH, scale 30000) after checking the
retained stack. For this T48D subset, the four dates are 20260629, 20260723,
20260816 and 20260909. The pre-event velocity series ends on 20260816 and has
three acquisition dates. The nearest retained DHDH event pair is
20260816–20260909. Polarization selection alone does not certify equal sampling
or bandwidth; retain the stack and preprocessing checks.
