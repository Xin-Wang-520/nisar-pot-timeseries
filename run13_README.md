# Run 13：尼泊尔事件前后振幅变化

直接使用 Run 4.5 或 Run 4.75 已经配准的 SLC，默认比较 `20260819`（前）和 `20260831`（后）。不需要重新配准，也不读取 `freq_xcorr.dat` 或 Run 9 时间序列。

## 输入与运行

将 `run13_nisar_amplitude_change.py` 放到服务器的 `T98A` 项目根目录，在原 GMTSAR 环境中运行。需要 Python 3、NumPy、GMT、Ghostscript 和 GMTSAR 的 `conv`、`gmtsar_sharedir.csh`、`make_gaussian_filter`、`proj_ra2ll_ascii.csh`。

使用之前的冰川局部研究区：

```bash
cd /data2/xinw/landsides/nepal_landsides/NISAR/T98A
local_project="$PWD/local_pot_roi/landslide_small_all"

# 先检查两期配准数据和主影像几何，不进行计算
python3 run13_nisar_amplitude_change.py \
  --source "$local_project" --pair 20260819 20260831

# 正式计算、地理编码并绘图
python3 run13_nisar_amplitude_change.py 1 \
  --source "$local_project" --pair 20260819 20260831 \
  --smooth-m 60 --spacing-m 15 --change-limit-db 10
```

这里读取 `SLC_coreg_cut/NSR_日期.PRM`、`NSR_日期.SLC`、`coreg_logs/NSR_日期/{registration.txt,valid_window.txt}`、`local_crop_master.txt` 和 `topo/{geometry.txt,trans.dat}`。两期影像及主影像的 PRM 必须齐全，并且属于同一个配准主影像。若某日期尚未包含在局部项目中，应先补做该日期的局部裁剪。

省略 `--source` 时，优先寻找当前目录下的 `local_pot_roi/landslide_small_all`。若想处理 Run 4.5 的完整配准区域，显式使用 `--source "$PWD"`，脚本读取 `SLC_coreg_full/`。

## 两种 SLC 分别处理

服务器上保留完整项目元数据时，用以下命令分别处理完整配准区域和局部区域。两套输出存入不同文件夹。

```bash
python3 run13_nisar_amplitude_change.py 1 \
  --source "$PWD" --pair 20260819 20260831 \
  --smooth-m 60 --spacing-m 15 \
  --output "$PWD/amplitude_results/20260819_20260831/full_geocoded"

python3 run13_nisar_amplitude_change.py 1 \
  --source "$PWD/local_pot_roi/landslide_small_all" \
  --pair 20260819 20260831 --smooth-m 60 --spacing-m 15 \
  --output "$PWD/amplitude_results/20260819_20260831/cut_geocoded"
```

如果像本地下载的数据一样，仅有两个目录内的 PRM/SLC/LED，使用显式 `--slc-dir` 加 `--radar-only`，不要求 `trans.dat`、配准日志或主影像 PRM：

```bash
for kind in SLC_coreg_full SLC_coreg_cut; do
  python3 run13_nisar_amplitude_change.py 1 \
    --slc-dir "$PWD/$kind" --radar-only \
    --pair 20260819 20260831 --smooth-m 60
done
```

仅将明确已经配准到同一网格的影像用于这个模式。脚本检查尺寸、采样率、零残余配准参数和字节数，屏蔽两期任一影像为零的像素，但下载文件没有配准日志时，不能复核原始配准质量或使用日志中的保守有效窗口。物理高斯滤波默认采用前期 PRM 的几何参数；有共同主影像 PRM 时，可指定 `--grid-prm /path/to/master.PRM`。

雷达模式默认输出到 `amplitude_results/20260819_20260831/SLC_coreg_full/initial_plus_g60_radar/` 和对应的 `SLC_coreg_cut/` 目录。每套包含 `initial/`、`gaussian_60m/`；雷达图位于各自的 `png_radar/`，含两期振幅图、两期 dB 功率图、振幅差图、dB 变化图和一行三列对照图，均有 PNG/PDF。雷达坐标为各自输入影像的零起始像素位置，不能在缺少裁剪信息时直接当作原主影像像素或经纬度。

## 计算过程

1. 检查配准主影像、两期尺寸和共同有效区域，排除补零像素。
2. 用同一初始高斯核分别处理两期复数 SLC，得到功率 (P\propto I^2+Q^2\)，保留 `initial` 结果。初始核按 GMTSAR `filter.csh` 的 PRF/距离采样率规则选择。
3. 另外生成并施加 60 m 高斯滤波，保留 `gaussian_60m` 结果。`--smooth-m 0` 可关闭这一额外步骤；这里的米数是 GMTSAR 滤波波长参数。
4. 对两期功率计算振幅 (A=\sqrt{P}\)、振幅差 `A_post-A_pre`、功率差 `P_post-P_pre`、功率比 `P_post/P_pre` 和 dB 变化 `10*log10(P_post/P_pre)`。滤波后的振幅是功率平方根，不是直接对振幅取均值。
5. 将零起始的局部雷达像素坐标恢复到原主影像坐标，再用原来的 `trans.dat` 地理编码。两期采用同一批有效测点和同一地理网格，以 `blockmean` 聚合功率，再计算地理网格上的差值及比值；空网格不插值填补。
6. 输出 PNG/PDF 对照图和振幅差图。两期功率图共用色标，dB 变化图默认显示 ±10 dB；仅显示范围受限，GRD 数值不裁剪。
7. 地理编码模式下自动生成 Google Earth KMZ。各图层使用无边框的经纬度影像，缺测区域透明，色标与对应地图一致。

## 输出位置

```text
local_pot_roi/landslide_small_all/amplitude_results/
  20260819_20260831/initial_plus_g60_spacing15_step1/
    initial/
    gaussian_60m/
    processing_summary.json
    README.txt
```

每套结果包含 `*_ra.grd`（原主影像雷达坐标）、`*_ll.grd`（经纬度网格）和 `png/` 下的 PNG/PDF 图。主要网格有：

- `amplitude_pre_ll.grd`、`amplitude_post_ll.grd`：两期振幅。
- `amplitude_difference_post_minus_pre_ll.grd`：后期振幅减前期振幅。
- `power_difference_post_minus_pre_ll.grd`：后期功率减前期功率。
- `power_ratio_post_over_pre_ll.grd`：后期/前期功率比。
- `change_post_over_pre_db_ll.grd`：后期/前期 dB 变化。

`png/comparison_pre_post_change_1x3.png` 为前期功率、后期功率、dB 变化的一行三列对照图；`png/amplitude_difference_post_minus_pre_ll.png` 为线性振幅差图。

## Google Earth KMZ

正常模式 `1` 完成地理编码后，会自动在各滤波目录下生成 `google_earth/`。已有地理编码结果可单独补导出，无需重新读取 SLC 或计算振幅：

```bash
cd /data2/xinw/landsides/nepal_landsides/NISAR/T98A

python3 run13_nisar_amplitude_change.py 2 \
  --results \
  "$PWD/amplitude_results/20260819_20260831/full_geocoded" \
  "$PWD/amplitude_results/20260819_20260831/cut_geocoded"
```

每个 `initial/google_earth/` 和 `gaussian_60m/google_earth/` 中包含：

- `all_layers.kmz`：六个图层合并打包，默认显示 dB 变化，打开后可在 Google Earth 左侧展开并切换图层。
- `amplitude_pre_ll.kmz`、`amplitude_post_ll.kmz`：两期振幅。
- `power_pre_db_ll.kmz`、`power_post_db_ll.kmz`：两期 dB 功率。
- `amplitude_difference_post_minus_pre_ll.kmz`：后期减前期振幅。
- `change_post_over_pre_db_ll.kmz`：后期/前期 dB 变化。

同时保留对应 PNG/KML。KMZ 内使用相对路径包含影像，复制单个 KMZ 即可独立打开。模式 `2` 接受一个或多个完整结果目录，也可直接传入某个 `initial/` 或 `gaussian_60m/` 目录。再次导出会更新该目录中的生成文件，不改动 GRD。仅有 `*_ra.grd` 的雷达坐标结果需要先完成地理编码，才能生成正确的 Google Earth 图层。

## 显示平滑与地理编码检查

PNG/PDF 和 KMZ 均使用最近邻显示（GMT `-nn`），避免双三次插值增加显示平滑。已算好的经纬度网格可在模式 `2` 中加 `--redraw`，重画 PNG/PDF 并更新 KMZ，不重算功率、不改 GRD。若同时保留雷达 GRD，也会重画 `png_radar/`：

```bash
python3 run13_nisar_amplitude_change.py 2 --redraw \
  --results "$PWD/amplitude_results/20260819_20260831/cut_geocoded"
```

完整区域和局部区域可同时更新：

```bash
python3 run13_nisar_amplitude_change.py 2 --redraw \
  --results \
  "$PWD/amplitude_results/20260819_20260831/full_geocoded" \
  "$PWD/amplitude_results/20260819_20260831/cut_geocoded"
```

同一命令也能重画仅有雷达 GRD 的本地结果目录；此时更新 `png_radar/`，并明确提示跳过 KMZ。重新导出 KMZ 需要原始经纬度 GRD，无法从已着色的 KMZ 图片完整恢复原数值。

`gaussian_60m` 数据本身仍含额外高斯平滑；可与 `initial/png/` 的同名图比较。地理编码还会把同一个约 15 m 网格内的功率做均值聚合。最近邻显示只消除绘图插值带来的额外平滑，不能恢复被滤波或聚合消除的细节。

振幅差图的对称色标取绝对差值的第 98 百分位。超出色标范围的有限值使用两端蓝/红饱和显示（GMT `makecpt -D`），GRD 不裁剪；缺测值仍为灰色（KMZ 中透明）。旧版默认黑/白前背景色会将负极值画成黑块、正极值画成白块，不能把这些块直接解释为缺测。使用 `--redraw` 可更新旧图和 KMZ。

若仅地理图出现明显扇形拉丝或灰色缝隙，应检查 `trans.dat`、雷达到地理坐标查找表，以及地理网格是否缺测；山区的雷达几何畸变也可能参与其中。先对照同一滤波版本的 `png_radar/` 和 `png/`，不能单凭截图将扇形纹理判定为真实变化或简单归因于 60 m 滤波。

振幅差和 dB 变化的正值表示后期变亮/增强，负值表示后期变暗/减弱，不能解释为 POT 的运动方向。结果是相对功率/振幅，未做 sigma0/gamma0 辐射定标或地形辐射归一化。默认 15 m 是地理网格采样间隔，不表示获得了 15 m 独立空间分辨率。

既有 SLC、PRM、POT 结果只读。已有输出不会覆盖；如需重算，指定另一个 `--output` 路径。计算失败时会保留临时目录和日志，便于排查。
