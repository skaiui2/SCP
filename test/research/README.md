# SSCP Slow-Start Pacing Research

This module evaluates SSCP pacing behavior across fixed pacing gain multipliers under Slow Start (SS).

## Overview

The slow-start pacing gain multipliers are centered around the theoretical critical value:
- **Slow Start Critical Gain**: $g = 2\ln(2) \approx 1.3863$ (`g138_6`)

24 representative gain points are tested to analyze queue buildup and pacing intervals.

## Building

```bash
cmake -S test -B build
cmake --build build --target sscp_research
```

## Running Experiments

### Single Gain Run
```bash
python test/research/run_sweep.py --mode ss --gain-id g138_6 --out results/g138_6
```

### Full Grid Sweep
```bash
python test/research/run_sweep.py --mode ss --all --out results/ss
```

Outputs:
- `network_events.csv`: Raw packet events (send, ack, seq, length, timestamp).
- `summary.json`: Transfer metrics (bytes, duration, throughput, max cwnd).
- `summary.csv`: Aggregated sweep results.
- `xpl/a2b_tsg.xpl`: Time-Sequence Graph in standard xplot format.

## Trace Visualization

Traces can be viewed directly in `jPlot` or any compatible xplot viewer:

```bash
java -jar jPlot.jar results/ss/g138_6/xpl/a2b_tsg.xpl
```

## Simulation Model

The theoretical discrete packet simulation model is located at `test/models/pacing.py`:

```bash
python test/models/pacing.py --mode ss --out sim_results
```
