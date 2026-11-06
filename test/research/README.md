# SSCP Pacing Gain Research

This module evaluates SSCP pacing behavior across fixed pacing gain multipliers under both Congestion Avoidance (CA) and Slow Start (SS).

## Overview

The pacing gain multipliers are centered around the theoretical critical values:
- **Congestion Avoidance**: Critical gain $g = \frac{3}{2}\ln(3) \approx 1.6479$ (`g164_8`)
- **Slow Start**: Critical gain $g = 2\ln(2) \approx 1.3863$ (`g138_6`)

For each mode, 22 representative gain points are tested.

## Building

```bash
cmake -S test -B build
cmake --build build --target sscp_research
```

## Running Experiments

### Single Gain Run
```bash
python test/research/run_sweep.py --mode ca --gain-id g164_8 --out results/g164_8
```

### Full Grid Sweep
```bash
python test/research/run_sweep.py --mode ca --all --out results/ca
```

Outputs:
- `network_events.csv`: Raw packet events (send, ack, seq, length, timestamp).
- `summary.json`: Transfer metrics (bytes, duration, throughput, max cwnd).
- `summary.csv`: Aggregated sweep results.
- `xpl/a2b_tsg.xpl`: Time-Sequence Graph in standard xplot format.

## Trace Visualization

Traces can be viewed directly in `jPlot` or any compatible xplot viewer:

```bash
java -jar jPlot.jar results/ca/g164_8/xpl/a2b_tsg.xpl
```

## Simulation Model

The theoretical discrete packet simulation model is located at `test/models/pacing.py`:

```bash
python test/models/pacing.py --mode ca --gain-grid ca-critical --out sim_results
```
