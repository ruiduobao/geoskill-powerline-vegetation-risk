# geoskill-powerline-vegetation-risk

> Identify vegetation encroachment, rapid growth, and tree fall risks along powerline corridors. Generate prioritized inspection points. Use when screening vegetation near powerlines, assessing tree fall hazards, prioritizing line inspection routes, or generating evidence for maintenance.

See [SKILL.md](SKILL.md) for full usage, inputs and outputs.

## Quick start

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

## License

MIT
