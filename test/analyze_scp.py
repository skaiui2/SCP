import sys
import ujson as json
import matplotlib.pyplot as plt
import pandas as pd

FIELDS = [
    "t", "seq", "ack", "sack", "len", "wnd", "flags",
    "snd_una", "snd_seq_q", "snd_nxt", "rcv_nxt",
    "snd_wnd", "rcv_wnd",
    "snd_q", "rcv_q",
    "cwnd", "ssthresh", "flight",
    "srtt", "rto", "dup_acks", "sb_cc",
    "packet_bytes", "packet_count",
    "cong_q",
    "p", "d", "z"
]

DOWNSAMPLE = 50

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
            row = {f: obj.get(f, 0) for f in FIELDS}
            rows.append(row)
            if len(rows) >= chunksize:
                yield pd.DataFrame(rows)
                rows.clear()
    if rows:
        yield pd.DataFrame(rows)

def plot_curve_json(logfile, field, title, filename):
    plt.figure(figsize=(12, 5))
    for df in stream_to_df(logfile):
        y = df[field]
        if field == "cong_q":
            y = y / 65535.0
        plt.plot(df["t"], y, linewidth=0.5)
    plt.title(title)
    plt.xlabel("t")
    plt.ylabel(field)
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(filename)
    plt.close()

def scatter_json(logfile, x, y, title, filename, step=200):
    plt.figure(figsize=(12, 5))
    for df in stream_to_df(logfile):
        df = df.iloc[::step]
        yy = df[y]
        if y == "cong_q":
            yy = yy / 65535.0
        plt.scatter(df[x], yy, s=2)
    plt.title(title)
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

    FLOW_FIELDS = ["snd_wnd", "rcv_wnd", "snd_q", "rcv_q", "sb_cc"]
    CONGEST_FIELDS = ["cwnd", "ssthresh", "flight", "dup_acks"]
    RTT_FIELDS = ["srtt", "rto"]
    CORE_FIELDS = ["seq", "ack", "snd_una", "snd_nxt", "rcv_nxt"]
    THROUGHPUT_FIELDS = ["packet_bytes", "packet_count"]
    CONG_PROB_FIELDS = ["cong_q"]
    DEBUG_FIELDS = ["p", "d", "z"]

    for f in CORE_FIELDS:
        plot_curve_json(logfile, f, f"{f}", f"{f}.png")

    for f in FLOW_FIELDS:
        plot_curve_json(logfile, f, f"{f}", f"{f}.png")

    for f in CONGEST_FIELDS:
        plot_curve_json(logfile, f, f"{f}", f"{f}.png")

    for f in RTT_FIELDS:
        plot_curve_json(logfile, f, f"{f}", f"{f}.png")

    for f in THROUGHPUT_FIELDS:
        plot_curve_json(logfile, f, f"{f}", f"{f}.png")

    for f in CONG_PROB_FIELDS:
        plot_curve_json(logfile, f, f"{f}", f"{f}.png")

    for f in DEBUG_FIELDS:
        plot_curve_json(logfile, f, f"{f}", f"{f}.png")

    scatter_json(logfile, "t", "seq",
                 "retransmission scatter",
                 "retx.png")

    scatter_json(logfile, "srtt", "cong_q",
                 "srtt vs cong_q",
                 "srtt_cong.png")

if __name__ == "__main__":
    main()

