---
name: powerline-vegetation-risk
description: >
  Identify vegetation encroachment, rapid growth, and tree fall risks along
  powerline corridors. Generate prioritized inspection points.
  Use when screening vegetation near powerlines, assessing tree fall hazards,
  prioritizing line inspection routes, or generating evidence for maintenance.
---

# Powerline Vegetation Risk

Identifies high vegetation, rapid growth, and potential tree fall risks along
powerline corridors. Outputs prioritized inspection points with evidence slices.

## Trigger

Use when the user wants to:
- Screen vegetation encroachment risk along powerline corridors
- Assess tree fall hazards toward powerlines
- Prioritize inspection routes based on vegetation risk
- Generate evidence slices for maintenance planning
- Evaluate clearance between vegetation and conductors
- Identify fast-growing vegetation near lines

## CLI Usage

```bash
# Basic analysis with default parameters (110kv, synthetic data)
python scripts/powerline_vegetation_risk.py --output-dir ./pvr-output

# 220kv line with custom conductor height
python scripts/powerline_vegetation_risk.py \
  --voltage 220kv \
  --conductor-height 14.0 \
  --output-dir ./pvr-output

# Without conductor height data (proximity risk only)
python scripts/powerline_vegetation_risk.py \
  --voltage 110kv \
  --no-conductor-data \
  --output-dir ./pvr-output

# Custom corridor width and fall factor
python scripts/powerline_vegetation_risk.py \
  --voltage 500kv \
  --corridor-width 60.0 \
  --fall-factor 1.2 \
  --output-dir ./pvr-output

# With custom risk scoring rules
python scripts/powerline_vegetation_risk.py \
  --voltage 110kv \
  --risk-rules references/risk_scoring.json \
  --output-dir ./pvr-output
```

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `--voltage` | 110kv | Voltage level: 10kv, 35kv, 110kv, 220kv, 500kv, 1000kv |
| `--conductor-height` | from schema | Conductor height above ground (meters) |
| `--no-conductor-data` | False | Flag: no conductor height data available |
| `--corridor-width` | from schema | Custom corridor half-width (meters) |
| `--fall-factor` | 1.0 | Tree fall reach factor (1.0 = tree height = reach) |
| `--risk-rules` | built-in | Custom risk scoring rules JSON |
| `--powerlines` | None | Powerlines GeoJSON file (LineString) |
| `--trees` | None | Trees GeoJSON file (Point with height/growth_rate) |
| `--output-dir` | ./pvr-output | Output directory |

## Output

| File | Description |
|---|---|
| `risk_points.geojson` | Point features for each vegetation risk location |
| `risk_segments.geojson` | Line features for high-risk line segments |
| `clearance.tif` | Raster map of vertical clearance (meters) |
| `inspection_priority.csv` | Ranked inspection segments with recommendations |
| `request.json` | Analysis request metadata |
| `output-manifest.json` | Output file inventory and risk summary |
| `qa.json` | Quality assurance checks |

## Risk Level Codes

| Code | Name | Description | Action |
|---|---|---|---|
| 4 | critical | Immediate risk | Dispatch crew within 24 hours |
| 3 | high | Priority risk | Schedule inspection within 7 days |
| 2 | medium | Planned risk | Include in next scheduled patrol |
| 1 | low | Monitor | Note for next routine inspection |
| 0 | minimal | No action | No action needed |

## Risk Scoring Factors

Each tree/vegetation point is scored on 5 factors (total 0-50):

| Factor | Max | Description |
|---|---|---|
| Height proximity | 10 | Vegetation height relative to conductor |
| Growth rate | 10 | Annual growth rate (m/year) |
| Fall risk | 10 | Tree fall reach vs. distance to line |
| Slope exposure | 8 | Terrain steepness toward line |
| Wind exposure | 6 | Storm/wind frequency factor |

## Key Algorithms

### Clearance Analysis
Computes vertical clearance = conductor_height - vegetation_height.
Without conductor data, outputs proximity risk only (no true clearance).

### Fall Risk
A tree can fall toward the line if tree_height * fall_factor >= distance_to_line.
This considers horizontal distance, not just corridor intersection.

### Corridor Generation
Buffers the powerline by voltage-dependent half-width (e.g., 25m for 110kv,
50m for 500kv). Custom width overrides default.

### Segment Analysis
Each line segment between towers is analyzed independently. Trees within
reach distance are scored and the segment gets the maximum risk level.

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Argument error |
| 3 | Dependency missing |
| 6 | Data validation failure |
| 7 | Processing failure |

## Limitations

- Without conductor height data, clearance values are estimates only
- Optical imagery cannot reliably provide individual tree heights
- Engineering safety conclusions require field verification
- Sensitive line data is processed locally (not uploaded)
- Fall factor is a simplification; actual fall direction depends on wind and tree species

## References

- GB/T 50545-2010: Code for design of 110kV~750kV overhead transmission line
- DL/T 741-2010: Operating code for overhead transmission line
- Common utility vegetation management practices (IEEE)


## 数据下载

本 skill 可自动从 Microsoft Planetary Computer 下载数据 (无需 API key):

```bash
python powerline_vegetation_risk.py --bbox 116,39,117,40 --date-range 2024-06-01,2024-06-30 --output-dir <tmp>
```

- `--bbox W,S,E,N`: WGS-84 边界框 (西, 南, 东, 北)
- `--date-range START,END`: 日期范围 (YYYY-MM-DD,YYYY-MM-DD)
- `--aoi-file <path.geojson>`: 替代 --bbox 的 GeoJSON 多边形
- `--cache-dir <path>`: 缓存目录 (默认 ~/.geoskill_cache)

当用户只给 `--bbox + --date-range` (没有 `--image`) 时，skill 自动下载数据。
当用户给 `--image` 时，走原文件路径 (向后兼容)。
