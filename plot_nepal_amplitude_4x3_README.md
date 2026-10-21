# 四组振幅成果的四行三列绘图

读取 `collect_nepal_sar_results.py` 整理的成果包。源 GRD 只读；不改变其范围、数值、分辨率或缺测，不生成裁剪后的 GRD。仅在绘图内存中选择视窗，采用最近邻显示。

默认视窗约为截图黄色框：西 85.512、东 85.536、南 28.278、北 28.299 度。它是读图估计值，可通过 `--region W E S N` 调整。

四行依次为陆探降轨（20260813–20260829）、NISAR T98A 升轨（20260819–20260831）、陆探升轨（20260702–20260827）、NISAR T48D 降轨（20260816–20260909）。三列为事件前相对功率 dB、事件后相对功率 dB、后期/前期功率变化 dB，与原振幅产品的前后变化对照图一致。

每行前后图使用该区域合并数据的 2–98 百分位作为共同显示色标，四行第三列共用 ±10 dB，每张变化图下方各有一条水平色标。超限值只在图上饱和显示。缺测保留为灰色，不补洞。不同传感器行之间的绝对功率数值不作定量等同。

需要 GMT、NumPy、Matplotlib；在 GMTSAR 环境运行。NISAR 默认使用已有 `initial` 版本，即不加额外 60 m 滤波。它仍是 Run13 已生成的初始功率产品；原始 SLC 并不参与本次绘图。若需要已存的 60 m 版本，可选 `--nisar-level gaussian_60m`。

```bash
cd /data2/xinw/landsides/nepal_landsides/Nepal_20260826_SAR_Results

# 预览路径与网格头
python3 06_Reproduce/plot_nepal_amplitude_4x3.py --bundle "$PWD"

# 正式绘图
python3 06_Reproduce/plot_nepal_amplitude_4x3.py 1 \
  --bundle "$PWD" \
  --region 85.512 85.536 28.278 28.299 \
  --nisar-level initial --change-limit-db 10 --dpi 300
```

结果在 `02_Amplitude/Comparison_4x3/`：PNG、PDF、输入及参数 JSON，以及对应文字说明。initial 和 gaussian_60m 使用不同文件名。再次画同一版本会更新对应图片及说明，不改动源网格。
