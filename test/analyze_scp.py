import sys
try:
    import ujson as json
except ImportError:
    import json

import matplotlib.pyplot as plt
import pandas as pd

FIELDS = [
    "t","seq","ack","sack","len","wnd","flags",
    "snd_una","snd_seq_q","snd_nxt","rcv_nxt",
    "snd_wnd","rcv_wnd",
    "snd_q","rcv_q",
    "cwnd","flight",
    "srtt","rto","dup_acks","sb_cc",
    "packet_bytes","packet_count",
    "cong_q","cong_q_ema","p","p_ema","d","z"
]

DOWNSAMPLE = 1

def stream_to_df(path, chunksize=200000):
    rows = []
    cnt = 0
    with open(path) as f:
        for line in f:
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except:
                continue
            cnt += 1
            if cnt % DOWNSAMPLE != 0:
                continue
            rows.append({f: obj.get(f, 0) for f in FIELDS})
            if len(rows) >= chunksize:
                yield pd.DataFrame(rows)
                rows.clear()
    if rows:
        yield pd.DataFrame(rows)

def plot_field(logfile, field):
    plt.figure(figsize=(12,5))
    for df in stream_to_df(logfile):
        y = df[field]
        if field == "cong_q" or field == "cong_q_ema":
            y = y / 65535.0
        plt.plot(df["t"], y, linewidth=0.5)
    plt.title(field)
    plt.xlabel("t")
    plt.ylabel(field)
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(f"{field}.png")
    plt.close()

def scatter(logfile, x, y, filename, step=200):
    plt.figure(figsize=(12,5))
    for df in stream_to_df(logfile):
        df = df.iloc[::step]
        yy = df[y]
        if y == "cong_q" or y == "cong_q_ema":
            yy = yy / 65535.0
        plt.scatter(df[x], yy, s=2)
    plt.title(f"{x} vs {y}")
    plt.xlabel(x)
    plt.ylabel(y)
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(filename)
    plt.close()

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_scp.py <logfile>")
        return

    logfile = sys.argv[1]

    for f in FIELDS:
        if f != "t":
            plot_field(logfile, f)

    scatter(logfile, "t", "seq", "retx.png")
    scatter(logfile, "srtt", "cong_q", "srtt_cong.png")
    scatter(logfile, "srtt", "cong_q_ema", "srtt_cong_ema.png")

if __name__ == "__main__":
    main()
