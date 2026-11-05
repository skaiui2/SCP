import argparse
from pathlib import Path


def slice_xpl(src, dst, max_mb=None, max_sec=None):
    max_bytes = int(max_mb * 1024 * 1024) if max_mb and max_mb > 0 else None
    active_color = None
    pending_color = None

    with open(src, 'r', encoding='ascii') as in_f, open(dst, 'w', encoding='ascii', newline='\n') as out_f:
        lines = iter(in_f)
        for line in lines:
            text = line.strip()
            if not text:
                continue

            if text in ('title', 'xlabel', 'ylabel'):
                out_f.write(text + '\n' + next(lines))
                continue

            if text in ('double double', 'aspect_ratio'):
                out_f.write(text + '\n')
                continue

            if text in ('white', 'green', 'yellow', 'red', 'blue', 'purple', 'gray20'):
                pending_color = text
                continue

            if text == 'go':
                break

            parts = text.split()
            cmd = parts[0]

            if cmd in ('dot', 'darrow', 'uarrow') and len(parts) >= 3:
                x, y = float(parts[1]), float(parts[2])
                if max_sec and x > max_sec:
                    break
                if max_bytes and y > max_bytes:
                    continue

            elif cmd in ('line', 'dline') and len(parts) >= 5:
                x1, y1, x2, y2 = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                if max_sec and min(x1, x2) > max_sec:
                    break
                if max_bytes and min(y1, y2) > max_bytes:
                    continue

            else:
                continue

            if pending_color != active_color:
                out_f.write(pending_color + '\n')
                active_color = pending_color

            out_f.write(text + '\n')

            if max_bytes and pending_color == 'green' and cmd in ('line', 'dline') and max(y1, y2) >= max_bytes:
                break

        out_f.write('go\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('src', type=Path)
    parser.add_argument('-o', '--out', type=Path)
    parser.add_argument('--max-mb', type=float, default=10)
    parser.add_argument('--max-sec', type=float)
    args = parser.parse_args()

    dst = args.out
    if dst is None:
        tag = f'{args.max_sec:g}s' if args.max_sec else f'{args.max_mb:g}mb'
        dst = args.src.with_name(f'{args.src.stem}_{tag}.xpl')

    slice_xpl(args.src, dst, args.max_mb if not args.max_sec else None, args.max_sec)
    print(f'Saved: {dst}')
