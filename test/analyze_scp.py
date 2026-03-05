import ujson as json
import matplotlib.pyplot as plt
import pandas as pd

FIELDS = [
    "t", "seq", "ack", "len", "wnd", "flags",
    "snd_una", "snd_nxt", "snd_sent", "rcv_nxt",
    "snd_wnd", "rcv_wnd",
    "snd_q", "rcv_q",
    "cwnd", "ssthresh", "flight",
    "srtt", "rto", "dup_acks", "sb_cc",
    "packet_bytes", "packet_count"
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

            if cnt % 100000 == 0:
                print(f"[{path}] parsed {cnt} lines...")

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
    print(f"Plotting {filename} ...")

    plt.figure(figsize=(12, 5))

    for df in stream_to_df(logfile):
        plt.plot(df["t"], df[field], linewidth=0.5)

    plt.title(title)
    plt.xlabel("t (ms)")
    plt.ylabel(field)
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(filename)
    plt.close()

    print(f"Saved {filename}")


def scatter_json(logfile, x, y, title, filename, step=200):
    print(f"Plotting {filename} ...")

    plt.figure(figsize=(12, 5))

    for df in stream_to_df(logfile):
        df = df.iloc[::step]
        plt.scatter(df[x], df[y], s=2)

    plt.title(title)
    plt.xlabel(x)
    plt.ylabel(y)
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(filename)
    plt.close()

    print(f"Saved {filename}")


def main():
    FLOW_FIELDS = ["snd_wnd", "rcv_wnd", "snd_q", "rcv_q", "sb_cc"]
    CONGEST_FIELDS = ["cwnd", "ssthresh", "flight", "dup_acks"]
    RTT_FIELDS = ["srtt", "rto"]
    CORE_FIELDS = ["seq", "ack", "snd_una", "snd_nxt", "rcv_nxt"]

    THROUGHPUT_FIELDS = ["packet_bytes", "packet_count"]

    for prefix in ["A", "B"]:
        logfile = f"node{prefix}.log"

        print(f"\n=== Processing {logfile} ===")

        for f in CORE_FIELDS:
            plot_curve_json(logfile, f, f"{prefix} {f}", f"{prefix}_{f}.png")

        for f in FLOW_FIELDS:
            plot_curve_json(logfile, f, f"{prefix} {f}", f"{prefix}_{f}.png")

        for f in CONGEST_FIELDS:
            plot_curve_json(logfile, f, f"{prefix} {f}", f"{prefix}_{f}.png")

        for f in RTT_FIELDS:
            plot_curve_json(logfile, f, f"{prefix} {f}", f"{prefix}_{f}.png")

        for f in THROUGHPUT_FIELDS:
            plot_curve_json(logfile, f, f"{prefix} {f}", f"{prefix}_{f}.png")

        scatter_json(logfile, "t", "seq",
                     f"{prefix} retransmission scatter",
                     f"{prefix}_retx.png")

    print("\nAll done.")


if __name__ == "__main__":
    main()

