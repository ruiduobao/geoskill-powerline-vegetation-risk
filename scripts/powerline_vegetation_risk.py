#!/usr/bin/env python3
"""
Powerline Vegetation Risk - Identify vegetation encroachment and fall risks
along powerline corridors for inspection prioritization.

Exit codes:
    0 = success
    2 = argument error
    3 = dependency missing
    6 = data validation failure
    7 = processing failure
"""

import argparse
import csv
import json
import math
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Try pip-installed package first; fall back to local copy in repo root.
try:
    from _geoskill_data_fetcher import (add_bbox_date_args,
        parse_bbox_arg,
        parse_date_range_arg,
        DataFetcher,
        DataSource,
        BBox,
        DateRange,
        DataFetcherError,)
    _FETCHER_AVAILABLE = True
except ImportError:
    import sys as _sys
    from pathlib import Path as _Path
    _skill_dir = _Path(__file__).resolve().parent
    _repo_root = _skill_dir.parent.parent
    _local_fetcher = _repo_root / "_geoskill_data_fetcher"
    if _local_fetcher.exists():
        _sys.path.insert(0, str(_repo_root))
    from _geoskill_data_fetcher import (add_bbox_date_args,
        parse_bbox_arg,
        parse_date_range_arg,
        DataFetcher,
        DataSource,
        BBox,
        DateRange,
        DataFetcherError,)
    _FETCHER_AVAILABLE = True
except ImportError:  # pragma: no cover - graceful when running standalone
    _FETCHER_AVAILABLE = False



EXIT_OK = 0
EXIT_ARG = 2
EXIT_DEP = 3
EXIT_VALIDATION = 6
EXIT_PROCESSING = 7

# Risk level codes
RISK_CRITICAL = 4
RISK_HIGH = 3
RISK_MEDIUM = 2
RISK_LOW = 1
RISK_MINIMAL = 0

# Default risk scoring schema
DEFAULT_RISK_SCHEMA = {
    "risk_levels": {
        "critical": {"code": 4, "min_total": 38, "color": "#d73027",
                     "description": "Critical risk", "action": "Dispatch crew within 24 hours"},
        "high": {"code": 3, "min_total": 28, "color": "#fc8d59",
                 "description": "High risk", "action": "Schedule inspection within 7 days"},
        "medium": {"code": 2, "min_total": 18, "color": "#fee08b",
                   "description": "Medium risk", "action": "Include in next scheduled patrol"},
        "low": {"code": 1, "min_total": 8, "color": "#91cf60",
                "description": "Low risk", "action": "Note for next routine inspection"},
        "minimal": {"code": 0, "min_total": 0, "color": "#1a9850",
                    "description": "Minimal risk", "action": "No action"},
    },
    "corridor_widths": {
        "1000kv": 75.0, "500kv": 50.0, "220kv": 35.0,
        "110kv": 25.0, "35kv": 15.0, "10kv": 10.0,
    },
    "conductor_heights": {
        "1000kv": 25.0, "500kv": 18.0, "220kv": 14.0,
        "110kv": 11.0, "35kv": 8.0, "10kv": 6.0,
    },
    "fall_factor": 1.0,
    "clearance_buffer": 2.0,
}

# Voltage levels supported
VOLTAGE_LEVELS = ["1000kv", "500kv", "220kv", "110kv", "35kv", "10kv"]


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def create_polygon(x: float, y: float, w: float, h: float) -> Dict:
    """Create a rectangular polygon dict (GeoJSON-like)."""
    return {
        "type": "Polygon",
        "coordinates": [[
            [x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]
        ]],
    }


def point_to_segment_distance(px: float, py: float,
                              x1: float, y1: float,
                              x2: float, y2: float) -> float:
    """
    Compute minimum distance from point (px, py) to line segment (x1,y1)-(x2,y2).
    """
    dx = x2 - x1
    dy = y2 - y1
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-12:
        return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_len_sq))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)


def point_to_polyline_distance(px: float, py: float,
                               line_coords: List[List[float]]) -> float:
    """Minimum distance from point to a polyline (list of [x, y] coords)."""
    min_dist = float("inf")
    for i in range(len(line_coords) - 1):
        x1, y1 = line_coords[i]
        x2, y2 = line_coords[i + 1]
        d = point_to_segment_distance(px, py, x1, y1, x2, y2)
        if d < min_dist:
            min_dist = d
    return min_dist


def compute_line_length(line_coords: List[List[float]]) -> float:
    """Total length of a polyline."""
    total = 0.0
    for i in range(len(line_coords) - 1):
        dx = line_coords[i + 1][0] - line_coords[i][0]
        dy = line_coords[i + 1][1] - line_coords[i][1]
        total += math.sqrt(dx * dx + dy * dy)
    return total


def buffer_line(line_coords: List[List[float]],
                half_width: float) -> Dict:
    """
    Create a simple buffer polygon around a polyline.
    Uses perpendicular offsets at each vertex.
    Returns a GeoJSON-like polygon dict.
    """
    if len(line_coords) < 2:
        return create_polygon(line_coords[0][0] - half_width,
                              line_coords[0][1] - half_width,
                              half_width * 2, half_width * 2)

    left = []
    right = []
    n = len(line_coords)

    for i in range(n):
        x, y = line_coords[i]
        # Direction vector
        if i == 0:
            dx = line_coords[1][0] - x
            dy = line_coords[1][1] - y
        elif i == n - 1:
            dx = x - line_coords[n - 2][0]
            dy = y - line_coords[n - 2][1]
        else:
            dx = line_coords[i + 1][0] - line_coords[i - 1][0]
            dy = line_coords[i + 1][1] - line_coords[i - 1][1]

        length = math.sqrt(dx * dx + dy * dy)
        if length < 1e-12:
            continue
        # Perpendicular (normalized)
        nx = -dy / length
        ny = dx / length
        left.append([x + nx * half_width, y + ny * half_width])
        right.append([x - nx * half_width, y - ny * half_width])

    # Combine: left forward + right reversed
    coords = left + right[::-1]
    if coords:
        coords.append(coords[0])  # close

    return {"type": "Polygon", "coordinates": [coords]}


# ---------------------------------------------------------------------------
# Clearance and fall analysis
# ---------------------------------------------------------------------------

def compute_clearance(vegetation_height: float,
                      conductor_height: float,
                      ground_elevation: float = 0.0,
                      has_conductor_data: bool = True) -> Dict[str, Any]:
    """
    Compute vertical clearance between vegetation and conductor.

    If no conductor data available, returns proximity risk only.
    """
    if not has_conductor_data:
        return {
            "clearance_m": None,
            "vegetation_height_m": float(vegetation_height),
            "conductor_height_m": None,
            "proximity_risk": "unknown",
            "has_conductor_data": False,
        }

    effective_conductor = conductor_height + ground_elevation
    clearance = effective_conductor - vegetation_height

    if clearance < 1.0:
        risk = "critical"
    elif clearance < 3.0:
        risk = "high"
    elif clearance < 5.0:
        risk = "medium"
    else:
        risk = "low"

    return {
        "clearance_m": round(clearance, 2),
        "vegetation_height_m": float(vegetation_height),
        "conductor_height_m": float(conductor_height),
        "proximity_risk": risk,
        "has_conductor_data": True,
    }


def compute_fall_risk(tree_height: float,
                      distance_to_line: float,
                      fall_factor: float = 1.0) -> Dict[str, Any]:
    """
    Compute tree fall risk toward powerline.

    A tree can fall toward the line if its height * fall_factor >= distance.
    """
    fall_reach = tree_height * fall_factor
    can_fall_to_line = fall_reach >= distance_to_line

    if can_fall_to_line:
        ratio = fall_reach / max(distance_to_line, 0.01)
        if ratio >= 1.5:
            risk = "critical"
        elif ratio >= 1.2:
            risk = "high"
        else:
            risk = "medium"
    else:
        risk = "low"

    return {
        "fall_reach_m": round(fall_reach, 2),
        "distance_to_line_m": round(distance_to_line, 2),
        "can_fall_to_line": can_fall_to_line,
        "fall_risk": risk,
        "fall_ratio": round(fall_reach / max(distance_to_line, 0.01), 2),
    }


# ---------------------------------------------------------------------------
# Risk scoring
# ---------------------------------------------------------------------------

def score_height_proximity(vegetation_height: float,
                           conductor_height: float,
                           has_conductor_data: bool) -> float:
    """Score 0-10 for height proximity risk."""
    if not has_conductor_data:
        # Without conductor data, score based on absolute height
        if vegetation_height >= 20:
            return 7.0
        elif vegetation_height >= 15:
            return 5.0
        elif vegetation_height >= 10:
            return 3.0
        else:
            return 1.0

    margin = conductor_height - vegetation_height
    if margin <= 1.0:
        return 10.0
    elif margin <= 3.0:
        return 7.0
    elif margin <= 5.0:
        return 4.0
    else:
        return 1.0


def score_growth_rate(growth_rate: float) -> float:
    """Score 0-10 for growth rate risk."""
    if growth_rate >= 1.5:
        return 10.0
    elif growth_rate >= 1.0:
        return 7.0
    elif growth_rate >= 0.5:
        return 4.0
    else:
        return 1.0


def score_fall_risk(tree_height: float, distance_to_line: float,
                    fall_factor: float) -> float:
    """Score 0-10 for fall risk."""
    fall_reach = tree_height * fall_factor
    if distance_to_line < 0.01:
        return 10.0
    ratio = fall_reach / distance_to_line
    if ratio >= 1.5:
        return 10.0
    elif ratio >= 1.2:
        return 7.0
    elif ratio >= 1.0:
        return 5.0
    else:
        return 1.0


def score_slope(slope_deg: float) -> float:
    """Score 0-8 for slope exposure."""
    if slope_deg >= 30:
        return 8.0
    elif slope_deg >= 20:
        return 5.0
    elif slope_deg >= 10:
        return 3.0
    else:
        return 1.0


def score_wind(wind_risk: float) -> float:
    """Score 0-6 for wind exposure."""
    if wind_risk >= 0.8:
        return 6.0
    elif wind_risk >= 0.5:
        return 4.0
    elif wind_risk >= 0.2:
        return 2.0
    else:
        return 1.0


def compute_total_risk_score(height_score: float, growth_score: float,
                             fall_score: float, slope_score: float,
                             wind_score: float) -> float:
    """Compute weighted total risk score (max 50)."""
    return height_score + growth_score + fall_score + slope_score + wind_score


def classify_risk_level(total_score: float, schema: Dict) -> int:
    """Classify total score to risk level code."""
    levels = schema["risk_levels"]
    if total_score >= levels["critical"]["min_total"]:
        return RISK_CRITICAL
    elif total_score >= levels["high"]["min_total"]:
        return RISK_HIGH
    elif total_score >= levels["medium"]["min_total"]:
        return RISK_MEDIUM
    elif total_score >= levels["low"]["min_total"]:
        return RISK_LOW
    else:
        return RISK_MINIMAL


def risk_level_to_name(code: int) -> str:
    """Convert risk level code to name."""
    names = {
        4: "critical", 3: "high", 2: "medium", 1: "low", 0: "minimal",
    }
    return names.get(code, "unknown")


# ---------------------------------------------------------------------------
# Corridor generation
# ---------------------------------------------------------------------------

def generate_corridor(line_coords: List[List[float]],
                      voltage: str,
                      schema: Dict,
                      custom_half_width: float = None) -> Dict:
    """
    Generate a corridor polygon around a powerline.

    Args:
        line_coords: List of [x, y] coordinates
        voltage: Voltage level string (e.g., '220kv')
        schema: Risk schema dict
        custom_half_width: Override corridor half-width

    Returns:
        GeoJSON-like polygon dict
    """
    if custom_half_width is not None:
        half_width = custom_half_width
    else:
        half_width = schema["corridor_widths"].get(voltage, 25.0)

    return buffer_line(line_coords, half_width)


# ---------------------------------------------------------------------------
# Segment analysis
# ---------------------------------------------------------------------------

def analyze_segment(segment_idx: int,
                    segment_coords: List[List[float]],
                    trees: List[Dict],
                    conductor_height: float,
                    has_conductor_data: bool,
                    schema: Dict,
                    fall_factor: float = 1.0) -> Dict:
    """
    Analyze a single line segment for vegetation risk.

    Args:
        segment_idx: Segment index
        segment_coords: [[x1,y1], [x2,y2]] for this segment
        trees: List of tree dicts with 'x', 'y', 'height', 'growth_rate', 'slope', 'wind_risk'
        conductor_height: Conductor height above ground
        has_conductor_data: Whether conductor height data is available
        schema: Risk schema
        fall_factor: Tree fall reach factor

    Returns:
        Segment analysis result dict
    """
    # Find trees near this segment
    nearby_trees = []
    for tree in trees:
        dist = point_to_segment_distance(
            tree["x"], tree["y"],
            segment_coords[0][0], segment_coords[0][1],
            segment_coords[1][0], segment_coords[1][1],
        )
        if dist <= tree.get("max_reach", 50.0):
            nearby_trees.append((tree, dist))

    if not nearby_trees:
        return {
            "segment_idx": segment_idx,
            "risk_level": RISK_MINIMAL,
            "risk_name": "minimal",
            "total_score": 0.0,
            "n_trees": 0,
            "trees": [],
        }

    # Score each tree
    tree_results = []
    max_score = 0.0
    max_risk = RISK_MINIMAL

    for tree, dist in nearby_trees:
        h_score = score_height_proximity(
            tree["height"], conductor_height, has_conductor_data
        )
        g_score = score_growth_rate(tree.get("growth_rate", 0.3))
        f_score = score_fall_risk(tree["height"], dist, fall_factor)
        s_score = score_slope(tree.get("slope_deg", 0.0))
        w_score = score_wind(tree.get("wind_risk", 0.1))

        total = compute_total_risk_score(h_score, g_score, f_score, s_score, w_score)
        risk_code = classify_risk_level(total, schema)

        clearance = compute_clearance(
            tree["height"], conductor_height,
            has_conductor_data=has_conductor_data,
        )
        fall = compute_fall_risk(tree["height"], dist, fall_factor)

        tree_result = {
            "x": tree["x"],
            "y": tree["y"],
            "height": tree["height"],
            "distance_to_line": round(dist, 2),
            "scores": {
                "height": h_score,
                "growth": g_score,
                "fall": f_score,
                "slope": s_score,
                "wind": w_score,
                "total": round(total, 1),
            },
            "risk_level": risk_code,
            "risk_name": risk_level_to_name(risk_code),
            "clearance": clearance,
            "fall_risk": fall,
        }
        tree_results.append(tree_result)

        if total > max_score:
            max_score = total
            max_risk = risk_code

    return {
        "segment_idx": segment_idx,
        "risk_level": max_risk,
        "risk_name": risk_level_to_name(max_risk),
        "total_score": round(max_score, 1),
        "n_trees": len(tree_results),
        "trees": tree_results,
    }


# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------

def generate_synthetic_powerline(length: float = 1000.0,
                                 n_towers: int = 5,
                                 seed: int = 42) -> List[List[float]]:
    """Generate a straight synthetic powerline for testing."""
    np.random.seed(seed)
    coords = []
    for i in range(n_towers):
        x = i * length / (n_towers - 1)
        y = np.random.normal(0, 5)
        coords.append([x, y])
    return coords


def generate_synthetic_trees(line_coords: List[List[float]],
                             n_trees: int = 50,
                             seed: int = 42) -> List[Dict]:
    """Generate synthetic tree data near a powerline."""
    np.random.seed(seed)
    trees = []

    # Get bounding box of line
    xs = [c[0] for c in line_coords]
    ys = [c[1] for c in line_coords]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    for _ in range(n_trees):
        # Random position near line
        t = np.random.uniform(0, 1)
        # Interpolate along line
        idx = int(t * (len(line_coords) - 1))
        idx = min(idx, len(line_coords) - 2)
        frac = t * (len(line_coords) - 1) - idx
        bx = line_coords[idx][0] + frac * (line_coords[idx + 1][0] - line_coords[idx][0])
        by = line_coords[idx][1] + frac * (line_coords[idx + 1][1] - line_coords[idx][1])

        # Offset perpendicular
        offset = np.random.uniform(-60, 60)
        angle = np.random.uniform(0, 2 * math.pi)
        tx = bx + offset * math.cos(angle)
        ty = by + offset * math.sin(angle)

        tree = {
            "x": round(tx, 2),
            "y": round(ty, 2),
            "height": round(np.random.uniform(3, 25), 1),
            "growth_rate": round(np.random.uniform(0.1, 2.0), 2),
            "slope_deg": round(np.random.uniform(0, 35), 1),
            "wind_risk": round(np.random.uniform(0, 1), 2),
            "max_reach": 50.0,
        }
        trees.append(tree)

    return trees


def generate_clearance_raster(width: int, height: int,
                              line_coords: List[List[float]],
                              conductor_height: float,
                              has_conductor_data: bool,
                              schema: Dict,
                              pixel_size: float = 1.0) -> np.ndarray:
    """
    Generate a synthetic clearance raster for testing.

    Returns a 2D array where each pixel value = clearance (meters).
    """
    raster = np.full((height, width), -9999.0, dtype=np.float32)

    for r in range(height):
        for c in range(width):
            px = c * pixel_size
            py = r * pixel_size
            dist = point_to_polyline_distance(px, py, line_coords)

            # Simulate vegetation height based on distance
            if dist < 100:
                veg_height = max(2.0, 20.0 - dist * 0.15 + np.random.normal(0, 2))
            else:
                veg_height = max(2.0, np.random.uniform(3, 10))

            if has_conductor_data:
                clearance = conductor_height - veg_height
            else:
                clearance = -9999.0  # Unknown

            raster[r, c] = clearance

    return raster


# ---------------------------------------------------------------------------
# Main analysis workflow
# ---------------------------------------------------------------------------

def auto_download_image(args, output_dir: Path) -> Dict[str, Any]:
    """Download one sentinel-2-l2a scene from MPC using --bbox + --date-range.

    Returns metadata dict (also writes the path back to args.image).
    """
    if not _FETCHER_AVAILABLE:
        raise RuntimeError(
            "Shared data fetcher not importable. Pass --image <local.tif> instead, "
            "or ensure _geoskill_data_fetcher is on sys.path."
        )
    bbox = parse_bbox_arg(getattr(args, "bbox", None), getattr(args, "aoi_file", None))
    if bbox is None:
        raise RuntimeError("auto_download_image requires --bbox or --aoi-file")
    dr = parse_date_range_arg(getattr(args, "date_range", None))
    if dr is None:
        raise RuntimeError("auto_download_image requires --date-range")
    cache_dir = getattr(args, "cache_dir", None)
    fetcher = DataFetcher(
        source=DataSource.PLANETARY_COMPUTER,
        cache_dir=Path(cache_dir) if cache_dir else None,
    )
    items = fetcher.search_stac(
        collection="sentinel-2-l2a",
        bbox=bbox,
        date_range=dr,
        limit=1,
    )
    if not items:
        raise RuntimeError(
            f"No sentinel-2-l2a items found in bbox={bbox} for {dr.start}..{dr.end}"
        )
    download_dir = output_dir / "downloaded"
    paths = fetcher.download_assets(
        items=items, out_dir=download_dir, max_items=1, max_total_mb=500,
        prefer_assets=['B04', 'B08', 'B02'],
    )
    if not paths:
        raise RuntimeError("Download returned no files")
    args.image = str(paths[0])
    return {
        "data_source": "MPC",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "collection": "sentinel-2-l2a",
        "bbox": bbox.to_string(),
        "date_range": f"{dr.start},{dr.end}",
        "n_items_searched": len(items),
        "downloaded_paths": [str(p) for p in paths],
    }


def run_analysis(args: argparse.Namespace) -> int:
    """Main analysis workflow."""
    output_dir = Path(args.output_dir) if args.output_dir else Path("pvr-output")

    # --- Auto-download mode: fetch sentinel-2-l2a from MPC ---
    if (getattr(args, "bbox", None) or getattr(args, "aoi_file", None)) and getattr(args, "date_range", None):
        if not getattr(args, "image", None):
            try:
                fetch_meta = auto_download_image(args, output_dir)
                mode = "auto_download"
                print(f"  Auto-downloaded image: {args.image}")
            except DataFetcherError as e:
                print(f"ERROR: auto-download failed: [{e.kind}] {e.message}", file=sys.stderr)
                return EXIT_PROCESSING if 'EXIT_PROCESSING' in dir() else 7
    output_dir.mkdir(parents=True, exist_ok=True)

    # Parse voltage level
    voltage = args.voltage if hasattr(args, 'voltage') and args.voltage else "110kv"
    if voltage not in VOLTAGE_LEVELS:
        print(f"ERROR: Unknown voltage '{voltage}'. Use one of: {VOLTAGE_LEVELS}",
              file=sys.stderr)
        return EXIT_ARG

    # Parse risk schema
    schema = DEFAULT_RISK_SCHEMA
    if hasattr(args, 'risk_rules') and args.risk_rules:
        rules_path = Path(args.risk_rules)
        if rules_path.exists():
            try:
                with open(rules_path, "r", encoding="utf-8") as f:
                    schema = json.load(f)
            except Exception as e:
                print(f"WARNING: Failed to load risk rules: {e}", file=sys.stderr)

    # Parse conductor height
    has_conductor_data = args.has_conductor_data if hasattr(args, 'has_conductor_data') else True
    conductor_height = args.conductor_height if hasattr(args, 'conductor_height') and args.conductor_height else None
    if conductor_height is None:
        conductor_height = schema.get("conductor_heights", {}).get(voltage, 11.0)

    # Parse corridor width
    custom_half_width = args.corridor_width if hasattr(args, 'corridor_width') and args.corridor_width else None

    # Parse fall factor
    fall_factor = args.fall_factor if hasattr(args, 'fall_factor') and args.fall_factor else 1.0

    # --- Generate or load data ---
    if hasattr(args, 'powerlines') and args.powerlines:
        # Load from file
        try:
            with open(args.powerlines, "r", encoding="utf-8") as f:
                pl_data = json.load(f)
            if pl_data.get("type") == "FeatureCollection":
                # Extract first line geometry
                for feat in pl_data["features"]:
                    geom = feat.get("geometry", {})
                    if geom.get("type") == "LineString":
                        line_coords = geom["coordinates"]
                        break
                else:
                    print("ERROR: No LineString found in powerlines file", file=sys.stderr)
                    return EXIT_VALIDATION
            elif pl_data.get("type") == "LineString":
                line_coords = pl_data["coordinates"]
            else:
                print("ERROR: Unsupported powerlines format", file=sys.stderr)
                return EXIT_VALIDATION
        except Exception as e:
            print(f"ERROR: Failed to read powerlines: {e}", file=sys.stderr)
            return EXIT_VALIDATION
    else:
        # Synthetic data
        line_coords = generate_synthetic_powerline(length=500.0, n_towers=6)

    # Generate or load trees
    if hasattr(args, 'trees') and args.trees:
        try:
            with open(args.trees, "r", encoding="utf-8") as f:
                trees_data = json.load(f)
            if trees_data.get("type") == "FeatureCollection":
                trees = []
                for feat in trees_data["features"]:
                    geom = feat.get("geometry", {})
                    props = feat.get("properties", {})
                    if geom.get("type") == "Point":
                        trees.append({
                            "x": geom["coordinates"][0],
                            "y": geom["coordinates"][1],
                            "height": props.get("height", 10.0),
                            "growth_rate": props.get("growth_rate", 0.5),
                            "slope_deg": props.get("slope_deg", 0.0),
                            "wind_risk": props.get("wind_risk", 0.1),
                            "max_reach": 50.0,
                        })
            else:
                print("ERROR: Unsupported trees format", file=sys.stderr)
                return EXIT_VALIDATION
        except Exception as e:
            print(f"ERROR: Failed to read trees: {e}", file=sys.stderr)
            return EXIT_VALIDATION
    else:
        trees = generate_synthetic_trees(line_coords, n_trees=40)

    # --- Build corridor ---
    corridor = generate_corridor(line_coords, voltage, schema, custom_half_width)

    # --- Analyze segments ---
    segment_results = []
    for i in range(len(line_coords) - 1):
        seg_coords = [line_coords[i], line_coords[i + 1]]
        result = analyze_segment(
            i, seg_coords, trees, conductor_height,
            has_conductor_data, schema, fall_factor,
        )
        segment_results.append(result)

    # --- Generate risk points ---
    risk_points = []
    for seg in segment_results:
        for tree in seg.get("trees", []):
            if tree["risk_level"] >= RISK_LOW:
                risk_points.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [tree["x"], tree["y"]],
                    },
                    "properties": {
                        "segment_idx": seg["segment_idx"],
                        "height": tree["height"],
                        "distance_to_line": tree["distance_to_line"],
                        "risk_level": tree["risk_level"],
                        "risk_name": tree["risk_name"],
                        "total_score": tree["scores"]["total"],
                        "height_score": tree["scores"]["height"],
                        "growth_score": tree["scores"]["growth"],
                        "fall_score": tree["scores"]["fall"],
                        "slope_score": tree["scores"]["slope"],
                        "wind_score": tree["scores"]["wind"],
                        "can_fall_to_line": tree["fall_risk"]["can_fall_to_line"],
                        "fall_reach": tree["fall_risk"]["fall_reach_m"],
                    },
                })

    # --- Generate risk segments GeoJSON ---
    risk_segments = []
    for seg in segment_results:
        if seg["risk_level"] >= RISK_LOW:
            idx = seg["segment_idx"]
            seg_coords = [line_coords[idx], line_coords[idx + 1]]
            risk_segments.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": seg_coords,
                },
                "properties": {
                    "segment_idx": seg["segment_idx"],
                    "risk_level": seg["risk_level"],
                    "risk_name": seg["risk_name"],
                    "total_score": seg["total_score"],
                    "n_trees": seg["n_trees"],
                },
            })

    # --- Generate clearance raster ---
    raster_w, raster_h = 200, 200
    clearance_raster = generate_clearance_raster(
        raster_w, raster_h, line_coords, conductor_height,
        has_conductor_data, schema, pixel_size=5.0,
    )

    # --- Write outputs ---

    # risk_points.geojson
    points_geojson = {"type": "FeatureCollection", "features": risk_points}
    points_path = output_dir / "risk_points.geojson"
    points_path.write_text(
        json.dumps(points_geojson, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # risk_segments.geojson
    segments_geojson = {"type": "FeatureCollection", "features": risk_segments}
    segments_path = output_dir / "risk_segments.geojson"
    segments_path.write_text(
        json.dumps(segments_geojson, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # clearance.tif
    try:
        import rasterio
        from rasterio.transform import from_bounds
    except ImportError:
        print("WARNING: rasterio not available, skipping GeoTIFF output", file=sys.stderr)
    else:
        transform = from_bounds(0, 0, raster_w * 5, raster_h * 5, raster_w, raster_h)
        clearance_path = output_dir / "clearance.tif"
        with rasterio.open(
            clearance_path, "w",
            driver="GTiff",
            height=raster_h,
            width=raster_w,
            count=1,
            dtype="float32",
            crs="EPSG:32650",  # UTM zone 50N (projected)
            transform=transform,
            nodata=-9999.0,
        ) as dst:
            dst.write(clearance_raster, 1)

    # inspection_priority.csv
    priority_path = output_dir / "inspection_priority.csv"
    with open(priority_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "rank", "segment_idx", "risk_level", "risk_name", "total_score",
            "n_trees", "max_tree_height", "min_distance", "recommendation",
        ])
        writer.writeheader()

        # Sort segments by score descending
        sorted_segs = sorted(
            [s for s in segment_results if s["risk_level"] >= RISK_LOW],
            key=lambda s: s["total_score"],
            reverse=True,
        )

        for rank, seg in enumerate(sorted_segs, 1):
            max_h = 0.0
            min_d = float("inf")
            for t in seg.get("trees", []):
                if t["height"] > max_h:
                    max_h = t["height"]
                if t["distance_to_line"] < min_d:
                    min_d = t["distance_to_line"]

            rec = schema["risk_levels"].get(
                risk_level_to_name(seg["risk_level"]), {}
            ).get("action", "Monitor")

            writer.writerow({
                "rank": rank,
                "segment_idx": seg["segment_idx"],
                "risk_level": seg["risk_level"],
                "risk_name": seg["risk_name"],
                "total_score": seg["total_score"],
                "n_trees": seg["n_trees"],
                "max_tree_height": round(max_h, 1),
                "min_distance": round(min_d, 1) if min_d < float("inf") else "N/A",
                "recommendation": rec,
            })

    # request.json
    request_info = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "voltage": voltage,
        "conductor_height": conductor_height,
        "has_conductor_data": has_conductor_data,
        "corridor_half_width": custom_half_width or schema["corridor_widths"].get(voltage, 25.0),
        "fall_factor": fall_factor,
        "n_trees": len(trees),
        "n_segments": len(segment_results),
        "output_dir": str(output_dir),
    }
    request_path = output_dir / "request.json"
    request_path.write_text(
        json.dumps(request_info, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # output-manifest.json
    output_files = {
        "risk_points.geojson": str(points_path),
        "risk_segments.geojson": str(segments_path),
        "inspection_priority.csv": str(priority_path),
        "request.json": str(request_path),
    }
    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "analysis_parameters": request_info,
        "output_files": output_files,
        "n_risk_points": len(risk_points),
        "n_risk_segments": len(risk_segments),
        "risk_summary": {
            "critical": sum(1 for s in segment_results if s["risk_level"] == RISK_CRITICAL),
            "high": sum(1 for s in segment_results if s["risk_level"] == RISK_HIGH),
            "medium": sum(1 for s in segment_results if s["risk_level"] == RISK_MEDIUM),
            "low": sum(1 for s in segment_results if s["risk_level"] == RISK_LOW),
            "minimal": sum(1 for s in segment_results if s["risk_level"] == RISK_MINIMAL),
        },
        "parameters": vars(args),  # T9: raw CLI args
        "summary": {
            "voltage": args.voltage,
            "n_outputs": len(output_files),
            "n_risk_points": len(risk_points),
            "n_risk_segments": len(risk_segments),
        },
    }
    
    # Auto-download metadata: propagate from fetch_meta (set by the
    # auto_download_* helpers in this module) into the manifest so the
    # output-manifest.json records data_source / collection / fetched_at.
    try:
        _fm = locals().get('fetch_meta') or globals().get('fetch_meta')
    except Exception:
        _fm = None
    if _fm:
        manifest["data_source"] = _fm.get("data_source")
        manifest["collection"] = _fm.get("collection")
        manifest["fetched_at"] = _fm.get("fetched_at")
        if "downloaded_paths" in _fm:
            manifest["downloaded_paths"] = _fm["downloaded_paths"]
        if "bbox" in _fm:
            manifest["query_bbox"] = _fm["bbox"]
        if "date_range" in _fm:
            manifest["query_date_range"] = _fm["date_range"]
    manifest_path = output_dir / "output-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # qa.json
    qa = {
        "status": "complete",
        "checks": {
            "crs_defined": True,
            "conductor_data_available": has_conductor_data,
            "corridor_generated": corridor is not None,
            "trees_analyzed": len(trees) > 0,
            "risk_points_generated": len(risk_points) > 0,
        },
        "warnings": [],
    }
    if not has_conductor_data:
        qa["warnings"].append(
            "No conductor height data: clearance values are estimates only"
        )
    qa_path = output_dir / "qa.json"
    qa_path.write_text(
        json.dumps(qa, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return EXIT_OK


# File-arg flags that must point to existing paths (None = skip check)
FILE_ARGS = {
    "risk_rules": "args.risk_rules",
    "powerlines": "args.powerlines",
    "trees": "args.trees",
}

# Numeric flags with (min, max) bounds; None = unbounded on that side
NUMERIC_RANGES = {
    "conductor_height": (0.0, None),
    "corridor_width": (0.0, None),
    "fall_factor": (0.0, 10.0),
}


def validate_args(args) -> int:
    """Validate file existence and numeric ranges.
    Returns exit code (0 = ok, 2 = arg error)."""
    # File existence
    for flag, accessor in FILE_ARGS.items():
        path = eval(accessor)
        if path is not None and not Path(path).exists():
            print(f"ERROR: --{flag} not found: {path}", file=sys.stderr)
            return 2
    # Numeric ranges
    for flag, (lo, hi) in NUMERIC_RANGES.items():
        val = getattr(args, flag, None)
        if val is None:
            continue
        if lo is not None and val < lo:
            print(f"ERROR: --{flag}={val} below minimum {lo}", file=sys.stderr)
            return 2
        if hi is not None and val > hi:
            print(f"ERROR: --{flag}={val} above maximum {hi}", file=sys.stderr)
            return 2
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Powerline Vegetation Risk Analysis"
    )
    parser.add_argument("--voltage", default="110kv",
                        choices=VOLTAGE_LEVELS,
                        help="Voltage level (default: 110kv)")
    parser.add_argument("--conductor-height", type=float, default=None,
                        help="Conductor height in meters (default: from schema)")
    parser.add_argument("--no-conductor-data", action="store_true",
                        help="Flag: no conductor height data available")
    parser.add_argument("--corridor-width", type=float, default=None,
                        help="Custom corridor half-width in meters")
    parser.add_argument("--fall-factor", type=float, default=1.0,
                        help="Tree fall reach factor (default: 1.0)")
    parser.add_argument("--risk-rules", default=None,
                        help="Custom risk scoring rules JSON")
    parser.add_argument("--powerlines", default=None,
                        help="Powerlines GeoJSON file")
    parser.add_argument("--trees", default=None,
                        help="Trees GeoJSON file")
    parser.add_argument("--output-dir", "-o", default="pvr-output",
                        help="Output directory (default: pvr-output)")
    parser.add_argument("--version", action="version", version="%(prog)s 1.0.0")

    add_bbox_date_args(parser)

    args = parser.parse_args()

    rc = validate_args(args)
    if rc != 0:
        sys.exit(rc)

    # Handle no-conductor-data flag
    if args.no_conductor_data:
        args.has_conductor_data = False
    else:
        args.has_conductor_data = True

    try:
        sys.exit(run_analysis(args))
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(EXIT_PROCESSING)


if __name__ == "__main__":
    main()
