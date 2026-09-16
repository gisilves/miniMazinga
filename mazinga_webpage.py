"""FTA SCD Microstrip Raw Data Reader - Multi-channel live HTML dashboard.

Reads ONE quadder from the UDP event stream, and tracks a configurable list
of 8 individual channels within it, each with its own threshold.
Serves a local web page showing:
  - each channel's latest value as a virtual LED, lit when above threshold
    (top 2x4 grid)
  - each channel's accumulated sum, only incremented on events above
    threshold (bottom 2x4 grid)
plus Start/Stop/Reset accumulation controls, Export to file button, and an elapsed-time timer.
Polled over HTTP - no PyQt / matplotlib needed.

Usage:
    python scd_multi_channel_viewer.py \
        --quadder 0 \
        --channels 10,42,100,200,300,500,900,1500 \
        --thresholds 500,500,500,500,500,500,500,500

Then open http://127.0.0.1:8000/ in a browser.
"""

import argparse
import json
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UDP_IP = "127.0.0.1"
UDP_PORT = 8890
BUF_SIZE = 65535

EVENT_START = 0xfa4af1ca
QUADDER_START = 0xbaba1a9a
QUADDER_END = 0x0bedface

N_CHANNELS = 1792

# ----------------------------------------------------------------------
# Shared state, guarded by a lock (written by the UDP thread, read/reset
# by the HTTP server thread).
# ----------------------------------------------------------------------
state_lock = threading.Lock()
latest_values = {}     # {channel_index: value}
above_threshold = {}    # {channel_index: bool}
accum_sum = {}           # {channel_index: running sum}
accum_count = 0          # number of events folded into accum_sum (any channel)
coinc_count = {}          # {(top_channel, bottom_channel): count of coincident above-threshold events}
accumulating = False     # whether new events are being added to accum_sum
accum_start_time = None  # epoch time of current run start/resume, or None
accum_elapsed = 0.0      # accumulated elapsed seconds from completed runs
event_id_counter = 0

thresholds = {}          # {channel_index: threshold value}, set by main()
top_channels_global = []   # set by main()
bottom_channels_global = []  # set by main()


def reorder(v):
    """Reorder ADC channels from multiplexer in the correct sequence."""
    reordered = [0] * len(v)
    j = 0
    order = [12, 13, 10, 11, 8, 9, 6, 7, 4, 5, 2, 3, 0, 1]

    for ch in range(128):
        for adc in order:
            reordered[adc * 128 + ch] = v[j]
            j += 1

    return reordered


def decode_quadder(words):
    """Decode the raw data from the quadder."""
    channels = []
    for w in words:
        ch_low = (w & 0xFFFF)
        ch_high = ((w >> 16) & 0xFFFF)
        channels.append(ch_low)
        channels.append(ch_high)
    return channels


def udp_loop(quadder_index, channels, ip, port, stop_event):
    """Parse the UDP event stream for one quadder, tracking `channels`."""
    global event_id_counter, accum_count

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((ip, port))
    sock.settimeout(0.2)

    in_event = False
    in_quadder = False
    words_read = 0
    quadder_read = 0
    quadder_words = []

    print(f"Listening on {ip}:{port} for quadder {quadder_index}, channels {channels}...")

    try:
        while not stop_event.is_set():
            try:
                data, _ = sock.recvfrom(BUF_SIZE)
            except socket.timeout:
                continue

            n = len(data) // 4
            for i in range(n):
                w = int.from_bytes(data[4 * i:4 * i + 4], "little")
                words_read += 1

                if w == EVENT_START:
                    in_event = True
                    in_quadder = False
                    quadder_read = 0
                    quadder_words.clear()
                    continue

                if not in_event:
                    continue

                if w == QUADDER_START:
                    quadder_read += 1
                    if quadder_read == quadder_index + 1:
                        in_quadder = True
                        quadder_words.clear()
                        continue

                if w == QUADDER_END and in_quadder and quadder_read == quadder_index + 1:
                    raw_channels = reorder(decode_quadder(quadder_words[8:]))
                    ch = raw_channels[:N_CHANNELS]

                    with state_lock:
                        event_id_counter += 1
                        touched = False
                        for c in channels:
                            value = ch[c] if c < len(ch) else None
                            latest_values[c] = value
                            is_above = value is not None and value > thresholds[c]
                            above_threshold[c] = is_above

                            if accumulating and is_above:
                                accum_sum[c] += 1
                                touched = True
                        if accumulating and touched:
                            accum_count += 1
                        if accumulating:
                            for t in top_channels_global:
                                for b in bottom_channels_global:
                                    if above_threshold.get(t) and above_threshold.get(b):
                                        key = (t, b)
                                        coinc_count[key] = coinc_count.get(key, 0) + 1

                    in_quadder = False
                    quadder_words.clear()
                    continue

                if in_quadder and quadder_read == quadder_index + 1:
                    quadder_words.append(w)

    finally:
        sock.close()


# ----------------------------------------------------------------------
# Accumulation control (called from the HTTP server thread)
# ----------------------------------------------------------------------

def start_accumulation():
    global accumulating, accum_start_time
    with state_lock:
        if not accumulating:
            accumulating = True
            accum_start_time = time.time()


def stop_accumulation():
    global accumulating, accum_start_time, accum_elapsed
    with state_lock:
        if accumulating:
            accum_elapsed += time.time() - accum_start_time
            accum_start_time = None
            accumulating = False


def reset_accumulation(channels):
    global accumulating, accum_start_time, accum_elapsed, accum_count, accum_sum, coinc_count
    with state_lock:
        accumulating = False
        accum_start_time = None
        accum_elapsed = 0.0
        accum_count = 0
        accum_sum = {c: 0 for c in channels}
        coinc_count = {
            (t, b): 0 for t in top_channels_global for b in bottom_channels_global
        }


def export_to_file(channels, quadder_index):
    with state_lock:
        elapsed_sec = current_elapsed()
        m = int(elapsed_sec // 60)
        s = elapsed_sec % 60
        elapsed_str = f"{m:02d}:{s:04.1f}"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            "========================================",
            "  SCD Viewer - Exported Run Statistics ",
            "========================================",
            f"Timestamp:      {timestamp}",
            f"Quadder Index:  {quadder_index}",
            f"Elapsed Time:   {elapsed_str} ({elapsed_sec:.2f} s)",
            f"Total Events:   {accum_count}",
            "----------------------------------------",
            "Accumulated Sums (Above Threshold):",
        ]
        for c in channels:
            lines.append(f"  Channel {c:4d}: {accum_sum.get(c, 0)} (Threshold: {thresholds.get(c, 0)})")

        lines.append("----------------------------------------")
        lines.append("Accumulated Coincidences (Top & Bottom):")
        for t in top_channels_global:
            for b in bottom_channels_global:
                count = coinc_count.get((t, b), 0)
                lines.append(f"  T{t} & B{b}: {count}")
        lines.append("========================================\n")

    filename = f"scd_export_quadder{quadder_index}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Exported run statistics to {filename}")
    return filename


def current_elapsed():
    """Must be called while holding state_lock."""
    if accumulating and accum_start_time is not None:
        return accum_elapsed + (time.time() - accum_start_time)
    return accum_elapsed


# ----------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------

def build_html(channels, quadder_index):
    """Top 2x4 grid of LEDs (threshold), 4x4 coincidence grid, bottom 2x4 sums."""
    top_channels = channels[0:4]
    bottom_channels = channels[4:8]

    led_cells = "\n".join(
        f'''
        <div class="cell">
          <h3>Channel {c}</h3>
          <div class="led" id="led-{c}"></div>
          <div class="thresh">thr &gt; {thresholds[c]}</div>
        </div>'''
        for c in channels
    )

    coinc_cells = "\n".join(
        f'''
        <div class="cell coinc-cell">
          <h3>T{t} &amp; B{b}</h3>
          <div class="led led-small" id="coinc-{t}-{b}"></div>
        </div>'''
        for t in top_channels
        for b in bottom_channels
    )

    coinc_sum_cells = "\n".join(
        f'''
        <div class="cell coinc-cell">
          <h3>T{t} &amp; B{b}</h3>
          <div class="value sum" id="coincsum-{t}-{b}">&mdash;</div>
        </div>'''
        for t in top_channels
        for b in bottom_channels
    )

    sum_cells = "\n".join(
        f'''
        <div class="cell">
          <h3>Channel {c}</h3>
          <div class="value sum" id="sum-{c}">&mdash;</div>
        </div>'''
        for c in channels
    )

    channel_list_js = json.dumps(channels)
    top_channels_js = json.dumps(top_channels)
    bottom_channels_js = json.dumps(bottom_channels)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>SCD Multi-Channel Viewer</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f6f7f8;
    color: #1f2933;
    margin: 0;
    padding: 16px;
  }}
  h1 {{
    font-size: 18px;
    margin: 0 0 4px 4px;
  }}
  h2 {{
    font-size: 14px;
    margin: 20px 0 8px 4px;
    color: #374151;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    grid-template-rows: repeat(2, 1fr);
    gap: 12px;
  }}
  .grid.coinc {{
    grid-template-rows: repeat(4, 1fr);
  }}
  .coinc-cell {{
    padding: 10px;
  }}
  .coinc-cell h3 {{
    font-size: 11px;
    margin-bottom: 6px;
  }}
  .led.led-small {{
    width: 30px;
    height: 30px;
    margin: 2px auto 4px auto;
  }}
  .cell {{
    background: white;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    padding: 16px;
    min-width: 0;
    text-align: center;
  }}
  .cell h3 {{
    margin: 0 0 8px 0;
    font-size: 13px;
    font-weight: 600;
    color: #6b7280;
  }}
  .value {{
    font-size: 36px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: #2563eb;
  }}
  .value.sum {{
    color: #059669;
  }}
  .led {{
    width: 48px;
    height: 48px;
    margin: 4px auto 6px auto;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 30%, #4b1113, #2a0a0b);
    border: 2px solid #1f0607;
    box-shadow: inset 0 0 6px rgba(0,0,0,0.6);
    transition: background 0.08s linear, box-shadow 0.08s linear;
  }}
  .led.on {{
    background: radial-gradient(circle at 35% 30%, #ff6b6b, #d61f26);
    border-color: #7a0f12;
    box-shadow: 0 0 14px 4px rgba(255,40,40,0.85), inset 0 0 6px rgba(255,255,255,0.3);
  }}
  .thresh {{
    font-size: 11px;
    color: #9ca3af;
  }}
  .controls {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 16px;
    margin: 16px auto;
    flex-wrap: wrap;
  }}
  button {{
    font-size: 13px;
    font-weight: 600;
    padding: 8px 16px;
    border-radius: 6px;
    border: 1px solid #cbd5e1;
    background: #e5e7eb;
    cursor: pointer;
  }}
  button:hover {{
    background: #dbeafe;
  }}
  #startBtn {{
    background: #16a34a;
    color: white;
    border-color: #15803d;
  }}
  #stopBtn {{
    background: #dc2626;
    color: white;
    border-color: #b91c1c;
  }}
  #resetBtn {{
    background: #6b7280;
    color: white;
    border-color: #4b5563;
  }}
  #exportBtn {{
    background: #2563eb;
    color: white;
    border-color: #1d4ed8;
  }}
  .status {{
    font-size: 13px;
    color: #6b7280;
    margin-left: 8px;
  }}
  .timer {{
    font-size: 20px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }}
  .running {{
    color: #16a34a;
  }}
</style>
</head>
<body>
<h1>SCD Multi-Channel Viewer &mdash; Quadder {quadder_index}</h1>

<h2>Above threshold (LED)</h2>
<div class="grid">
{led_cells}
</div>

<h2>Coincidence: top (T) AND bottom (B)</h2>
<div class="grid coinc">
{coinc_cells}
</div>

<div class="controls">
  <button id="startBtn" onclick="callControl('/start')">Start accumulation</button>
  <button id="stopBtn" onclick="callControl('/stop')">Stop accumulation</button>
  <button id="resetBtn" onclick="callControl('/reset')">Reset accumulation</button>
  <button id="exportBtn" onclick="callControl('/export')">Export to TXT</button>
  <span class="status" id="status">stopped</span>
  <span class="timer" id="timer">00:00.0</span>
</div>

<h2>Accumulated sum, above-threshold events only (<span id="countLabel">0</span> events)</h2>
<div class="grid">
{sum_cells}
</div>

<h2>Accumulated coincidences (T &amp; B)</h2>
<div class="grid coinc">
{coinc_sum_cells}
</div>

<script>
const channels = {channel_list_js};
const topChannels = {top_channels_js};
const bottomChannels = {bottom_channels_js};

async function callControl(path) {{
  try {{
    const res = await fetch(path, {{ method: 'POST' }});
    if (path === '/export' && res.ok) {{
      const data = await res.json();
      alert('Data exported successfully to:\\n' + data.filename);
    }}
  }} catch (e) {{
    console.error(e);
  }}
}}

function formatElapsed(sec) {{
  const m = Math.floor(sec / 60);
  const s = (sec % 60).toFixed(1);
  return String(m).padStart(2, '0') + ':' + String(s).padStart(4, '0');
}}

async function poll() {{
  try {{
    const res = await fetch('/data.json');
    const data = await res.json();

    for (const c of channels) {{
      const entry = data.channels[c];
      const ledEl = document.getElementById('led-' + c);
      const sumEl = document.getElementById('sum-' + c);

      if (entry && entry.above) {{
        ledEl.classList.add('on');
      }} else {{
        ledEl.classList.remove('on');
      }}

      sumEl.textContent = (entry && entry.sum !== null && entry.sum !== undefined)
        ? entry.sum : '\\u2014';
    }}

    for (const t of topChannels) {{
      for (const b of bottomChannels) {{
        const key = t + '-' + b;
        const coincEl = document.getElementById('coinc-' + key);
        const topOn = data.channels[t] && data.channels[t].above;
        const bottomOn = data.channels[b] && data.channels[b].above;
        if (topOn && bottomOn) {{
          coincEl.classList.add('on');
        }} else {{
          coincEl.classList.remove('on');
        }}

        const coincSumEl = document.getElementById('coincsum-' + key);
        const count = data.coinc_count[key];
        coincSumEl.textContent = (count !== null && count !== undefined) ? count : '\\u2014';
      }}
    }}

    document.getElementById('countLabel').textContent = data.accum_count;
    document.getElementById('timer').textContent = formatElapsed(data.elapsed);

    const statusEl = document.getElementById('status');
    const timerEl = document.getElementById('timer');
    if (data.accumulating) {{
      statusEl.textContent = 'accumulating...';
      timerEl.classList.add('running');
    }} else {{
      statusEl.textContent = 'stopped';
      timerEl.classList.remove('running');
    }}
  }} catch (e) {{
    console.error(e);
  }}
  setTimeout(poll, 200);
}}
poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    channels = []        # set by main()
    quadder_index = 0    # set by main()

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = build_html(self.channels, self.quadder_index).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/data.json":
            with state_lock:
                channels_data = {
                    str(c): {
                        "value": latest_values.get(c),
                        "above": above_threshold.get(c, False),
                        "sum": accum_sum.get(c),
                    }
                    for c in self.channels
                }
                coinc = {
                    f"{t}-{b}": coinc_count.get((t, b), 0)
                    for t in top_channels_global
                    for b in bottom_channels_global
                }
                payload = {
                    "channels": channels_data,
                    "coinc_count": coinc,
                    "accumulating": accumulating,
                    "accum_count": accum_count,
                    "elapsed": current_elapsed(),
                }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/start":
            start_accumulation()
            self._ok()
        elif self.path == "/stop":
            stop_accumulation()
            self._ok()
        elif self.path == "/reset":
            reset_accumulation(self.channels)
            self._ok()
        elif self.path == "/export":
            filename = export_to_file(self.channels, self.quadder_index)
            body = json.dumps({"ok": True, "filename": filename}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _ok(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global accum_sum, thresholds, top_channels_global, bottom_channels_global, coinc_count

    parser = argparse.ArgumentParser(
        description="Live dashboard: LEDs above threshold + accumulated sum, for 8 channels."
    )
    parser.add_argument("--quadder", type=int, default=0, help="Quadder index to read (0-7)")
    parser.add_argument("--channels", type=str, default="0,1,2,3,4,5,6,7",
                        help="Comma-separated list of 8 channel indices (0-1791), in display order.")
    parser.add_argument("--thresholds", type=str, default="1546,1604,1643,1686,1687,1643,1628,1643",
                        help="Comma-separated list of 8 thresholds (one per channel, same order as --channels). "
                             "A channel lights up and is accumulated when its value is strictly above its threshold.")
    parser.add_argument("--ip", type=str, default=UDP_IP, help="UDP bind IP")
    parser.add_argument("--port", type=int, default=UDP_PORT, help="UDP bind port")
    parser.add_argument("--http-port", type=int, default=8000, help="Local web server port")
    args = parser.parse_args()

    channels = [int(x.strip()) for x in args.channels.split(",")]
    if len(channels) != 8:
        raise SystemExit(f"Expected exactly 8 channels, got {len(channels)}: {channels}")

    thresh_values = [int(x.strip()) for x in args.thresholds.split(",")]
    if len(thresh_values) != 8:
        raise SystemExit(f"Expected exactly 8 thresholds, got {len(thresh_values)}: {thresh_values}")

    thresholds = dict(zip(channels, thresh_values))
    accum_sum = {c: 0 for c in channels}
    top_channels_global = channels[0:4]
    bottom_channels_global = channels[4:8]
    coinc_count = {
        (t, b): 0 for t in top_channels_global for b in bottom_channels_global
    }

    Handler.channels = channels
    Handler.quadder_index = args.quadder

    stop_event = threading.Event()
    udp_thread = threading.Thread(
        target=udp_loop, args=(args.quadder, channels, args.ip, args.port, stop_event), daemon=True
    )
    udp_thread.start()

    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    print(f"Dashboard running at http://127.0.0.1:{args.http_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()