"""miniMazinga - Multi-channel live HTML dashboard.

Reads DE10Nano data from the UDP event stream.

Serves local web pages showing:

  Status page:
    - each as a virtual LED, lit when above threshold
      (top 2x4 grid)

    - arrows between top and bottom LED rows, lit when a coincidence is fired
    
  Data page:
    - each channel's accumulated sum, only incremented on events above
      threshold (bottom 2x4 grid)
      
    - accumulated coincidences (top & bottom 2x4 grid)
    
    - Start/Stop/Reset accumulation controls, Export to file button, and an elapsed-time timer.

Usage:
    python mazinga_webpage.py \

Then open http://127.0.0.1:8000/ in a browser.
"""

import argparse
import json
import socket
import threading
import time
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UDP_IP = "127.0.0.1"
UDP_PORT = 8890
BUF_SIZE = 65535

EVENT_START = 0xfa4af1ca
de10nano_START = 0xbaba1a9a
de10nano_END = 0x0bedface

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

top_channels_global = []   # set by main()
bottom_channels_global = []  # set by main()


def decode_payload(word):
    """Decode the raw data from the de10nano."""
    channels = [(word >> i) & 1 for i in range(8)]
    return channels


def udp_loop(de10nano_index, channels, ip, port, stop_event):
    """Parse the UDP event stream for one de10nano, tracking `channels`."""
    global event_id_counter, accum_count

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((ip, port))
    sock.settimeout(0.2)

    in_event = False
    in_de10nano = False
    words_read = 0
    de10nano_read = 0
    de10nano_words = []

    print(f"Listening on {ip}:{port} for de10nano {de10nano_index}, channels {channels}...")

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
                    in_de10nano = False
                    de10nano_read = 0
                    de10nano_words.clear()
                    continue

                if not in_event:
                    continue

                if w == de10nano_START:
                    de10nano_read += 1
                    if de10nano_read == de10nano_index + 1:
                        in_de10nano = True
                        de10nano_words.clear()
                        continue

                if w == de10nano_END and in_de10nano and de10nano_read == de10nano_index + 1:
                    raw_channels = decode_payload(de10nano_words[10])
                    ch = raw_channels[:N_CHANNELS]

                    with state_lock:
                        event_id_counter += 1
                        touched = False
                        for c in channels:
                            value = ch[c] if c < len(ch) else None
                            latest_values[c] = value
                            is_above = bool(value)
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

                    in_de10nano = False
                    de10nano_words.clear()
                    continue

                if in_de10nano and de10nano_read == de10nano_index + 1:
                    de10nano_words.append(w)

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


def export_to_file(channels, de10nano_index):
    with state_lock:
        elapsed_sec = current_elapsed()
        m = int(elapsed_sec // 60)
        s = elapsed_sec % 60
        elapsed_str = f"{m:02d}:{s:04.1f}"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            "========================================",
            "  miniMazinga - Exported Run Statistics ",
            "========================================",
            f"Timestamp:      {timestamp}",
            f"de10nano Index:  {de10nano_index}",
            f"Elapsed Time:   {elapsed_str} ({elapsed_sec:.2f} s)",
            f"Total Events:   {accum_count}",
            "----------------------------------------",
            "Accumulated Sums (Above Threshold):",
        ]
        for c in channels:
            lines.append(f"  Channel {c:4d}: {accum_sum.get(c, 0)}")

        lines.append("----------------------------------------")
        lines.append("Accumulated Coincidences (Top & Bottom):")
        for t in top_channels_global:
            for b in bottom_channels_global:
                count = coinc_count.get((t, b), 0)
                lines.append(f"  T{t} & B{b}: {count}")
        lines.append("========================================\n")

    filename = f"export_de10nano{de10nano_index}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
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

def build_leds_html(channels):
    """Separate page: 2x4 grid of status LEDs (lit when above threshold)."""
    def make_cells(chs, name):
        return "\n".join(
            f'''
        <div class="cell" id="cell-{c}">
          <h3>{name} {c}</h3>
          <div class="led" id="led-{c}"></div>
        </div>'''
            for c in chs
        )

    top_cells = make_cells(channels[0:4], "Top")
    bottom_cells = make_cells(channels[4:8], "Bottom")
    channel_list_js = json.dumps(channels)
    top_channels_js = json.dumps(channels[0:4])
    bottom_channels_js = json.dumps(channels[4:8])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>miniMazinga Status LEDs</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background-color: #0f172a;
    background-image: url('background.jpg');
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    background-attachment: fixed;
    color: #f8fafc;
    margin: 0;
    padding: 12px;
  }}
  h1 {{
    font-size: 16px;
    margin: 0 0 4px 4px;
    color: #f1f5f9;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.8);
  }}
  a {{ color: #38bdf8; font-size: 12px; margin-left: 4px; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    grid-template-rows: 1fr;
    gap: 6px;
    max-width: 800px;
  }}
  .wrap {{
    position: relative;
    max-width: 800px;
    margin-top: 8px;
  }}
  .spacer {{
    height: 500px;
  }}
  .arrows {{
    position: absolute;
    left: 0;
    top: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    overflow: visible;
  }}
  .arrows line {{
    stroke: #475569;
    stroke-width: 2.5;
    opacity: 0.6;
  }}
  .arrows line.on {{
    stroke: #ef4444;
    stroke-width: 2.5;
    opacity: 1;
    filter: drop-shadow(0 0 4px rgba(239, 68, 68, 0.9));
  }}
  .cell {{
    background: rgba(30, 41, 59, 0.85);
    backdrop-filter: blur(4px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 6px;
    padding: 6px;
    min-width: 0;
    text-align: center;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
  }}
  .cell h3 {{
    margin: 0 0 2px 0;
    font-size: 11px;
    font-weight: 600;
    color: #94a3b8;
  }}
  .led {{
    width: 24px;
    height: 24px;
    margin: 2px auto;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 30%, #450a0a, #180202);
    border: 1.5px solid #0f172a;
    box-shadow: inset 0 0 4px rgba(0,0,0,0.8);
    transition: background 0.08s linear, box-shadow 0.08s linear;
  }}
  .led.on {{
    background: radial-gradient(circle at 35% 30%, #fca5a5, #ef4444);
    border-color: #991b1b;
    box-shadow: 0 0 10px 2px rgba(239, 68, 68, 0.9), inset 0 0 4px rgba(255,255,255,0.4);
  }}
  .thresh {{
    font-size: 9px;
    color: #94a3b8;
  }}
</style>
</head>
<body>
<h1>miniMazinga Status LEDs</h1>
<a href="/">&larr; Back to main dashboard</a>
<div class="wrap" id="wrap">
  <div class="grid">
{top_cells}
  </div>
  <div class="spacer"></div>
  <div class="grid">
{bottom_cells}
  </div>
  <svg class="arrows" id="arrows" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <marker id="head-off" viewBox="0 0 10 10" refX="9" refY="5"
              markerWidth="6" markerHeight="6" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" fill="#475569"/>
      </marker>
      <marker id="head-on" viewBox="0 0 10 10" refX="9" refY="5"
              markerWidth="5" markerHeight="5" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" fill="#ef4444"/>
      </marker>
    </defs>
  </svg>
</div>

<script>
const channels = {channel_list_js};
const topChannels = {top_channels_js};
const bottomChannels = {bottom_channels_js};
const SVGNS = 'http://www.w3.org/2000/svg';
const arrowLines = {{}};  // key "t-b" -> <line>

function drawArrows() {{
  const wrap = document.getElementById('wrap').getBoundingClientRect();
  const svg = document.getElementById('arrows');
  svg.querySelectorAll('line').forEach(l => l.remove());
  const SPREAD = 10;  // px between arrow endpoints on one cell edge
  topChannels.forEach((t, i) => {{
    const tr = document.getElementById('cell-' + t).getBoundingClientRect();
    bottomChannels.forEach((b, j) => {{
      const br = document.getElementById('cell-' + b).getBoundingClientRect();
      const x1 = tr.left + tr.width / 2 + (j - 1.5) * SPREAD - wrap.left;
      const y1 = tr.bottom - wrap.top;
      const x2 = br.left + br.width / 2 + (i - 1.5) * SPREAD - wrap.left;
      const y2 = br.top - wrap.top - 2;
      const line = document.createElementNS(SVGNS, 'line');
      line.setAttribute('x1', x1);
      line.setAttribute('y1', y1);
      line.setAttribute('x2', x2);
      line.setAttribute('y2', y2);
      line.setAttribute('marker-end', 'url(#head-off)');
      svg.appendChild(line);
      arrowLines[t + '-' + b] = line;
    }});
  }});
}}

function setArrow(line, on) {{
  if (on) {{
    if (!line.classList.contains('on')) {{
      line.classList.add('on');
      line.setAttribute('marker-end', 'url(#head-on)');
      line.parentNode.appendChild(line);  // bring lit arrow to front
    }}
  }} else if (line.classList.contains('on')) {{
    line.classList.remove('on');
    line.setAttribute('marker-end', 'url(#head-off)');
  }}
}}

drawArrows();
window.addEventListener('resize', drawArrows);

async function poll() {{
  try {{
    const res = await fetch('/data.json');
    const data = await res.json();
    for (const c of channels) {{
      const entry = data.channels[c];
      const ledEl = document.getElementById('led-' + c);
      if (entry && entry.above) {{
        ledEl.classList.add('on');
      }} else {{
        ledEl.classList.remove('on');
      }}
    }}
    for (const t of topChannels) {{
      for (const b of bottomChannels) {{
        const topOn = data.channels[t] && data.channels[t].above;
        const bottomOn = data.channels[b] && data.channels[b].above;
        setArrow(arrowLines[t + '-' + b], !!(topOn && bottomOn));
      }}
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


def build_html(channels, de10nano_index):
    """4x4 coincidence grid, 2x4 sums. Status LEDs live on /leds."""
    top_channels = channels[0:4]
    bottom_channels = channels[4:8]

    coinc_cells = "\n".join(
        f'''
        <div class="cell coinc-cell">
          <h3>T{t} &amp; B{b}</h3>
          <div class="led led-small" id="coinc-{t}-{b}"></div>
        </div>'''
        for t in top_channels
        for b in bottom_channels
    )

    distance_between_rows = 15
    bar_width = 4
    # Compute the angle corresponding to the bars coincidences
    coinc_angles = {}

    for i, t in enumerate(top_channels):
        for j, b in enumerate(bottom_channels):
            dx = (i - j) * bar_width
            coinc_angles[(t, b)] = math.degrees(math.atan2(dx, distance_between_rows))

    coinc_sum_cells = "\n".join(
        f'''
        <div class="cell coinc-cell">
          <h3>T{t} &amp; B{b}</h3>
          <div class="value sum" id="coincsum-{t}-{b}">&mdash;</div>
          <div class="angle">{coinc_angles[(t, b)]:.1f}&deg;</div>
        </div>'''
        for t in top_channels
        for b in bottom_channels
    )

    sum_cells_top = "\n".join(
        f'''
        <div class="cell">
          <h3>Top {t}</h3>
          <div class="value sum" id="sum-{t}">&mdash;</div>
        </div>'''
        for t in top_channels
    )

    sum_cells_bottom = "\n".join(
        f'''
        <div class="cell">
          <h3>Bottom {b}</h3>
          <div class="value sum" id="sum-{b}">&mdash;</div>
        </div>'''
        for b in bottom_channels
    )

    channel_list_js = json.dumps(channels)
    top_channels_js = json.dumps(top_channels)
    bottom_channels_js = json.dumps(bottom_channels)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>miniMazinga Viewer</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background-color: #0f172a;
    background-image: url('background.jpg');
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    background-attachment: fixed;
    color: #f8fafc;
    margin: 0;
    padding: 12px;
  }}
  h1 {{
    font-size: 16px;
    margin: 0 0 4px 4px;
    color: #f1f5f9;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.8);
  }}
  h2 {{
    font-size: 12px;
    margin: 12px 0 6px 4px;
    color: #cbd5e1;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.8);
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    grid-template-rows: repeat(2, 1fr);
    gap: 6px;
    max-width: 800px;
  }}
  .grid.coinc {{
    grid-template-rows: repeat(4, 1fr);
  }}
  .cell {{
    background: rgba(30, 41, 59, 0.85);
    backdrop-filter: blur(4px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 6px;
    padding: 6px;
    min-width: 0;
    text-align: center;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
  }}
  .coinc-cell {{
    padding: 4px;
  }}
  .cell h3 {{
    margin: 0 0 2px 0;
    font-size: 11px;
    font-weight: 600;
    color: #94a3b8;
  }}
  .coinc-cell h3 {{
    font-size: 10px;
    margin-bottom: 2px;
  }}
  .value {{
    font-size: 20px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: #38bdf8;
  }}
  .value.sum {{
    color: #34d399;
  }}
  .led {{
    width: 24px;
    height: 24px;
    margin: 2px auto;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 30%, #450a0a, #180202);
    border: 1.5px solid #0f172a;
    box-shadow: inset 0 0 4px rgba(0,0,0,0.8);
    transition: background 0.08s linear, box-shadow 0.08s linear;
  }}
  .led.led-small {{
    width: 16px;
    height: 16px;
    margin: 2px auto;
  }}
  .led.on {{
    background: radial-gradient(circle at 35% 30%, #fca5a5, #ef4444);
    border-color: #991b1b;
    box-shadow: 0 0 10px 2px rgba(239, 68, 68, 0.9), inset 0 0 4px rgba(255,255,255,0.4);
  }}
  .thresh {{
    font-size: 9px;
    color: #94a3b8;
  }}
  .controls {{
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 10px;
    margin: 12px 0;
    flex-wrap: wrap;
    background: rgba(15, 23, 42, 0.75);
    backdrop-filter: blur(4px);
    padding: 8px 12px;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.1);
    max-width: 800px;
  }}
  button {{
    font-size: 11px;
    font-weight: 600;
    padding: 6px 12px;
    border-radius: 4px;
    border: 1px solid #475569;
    background: #334155;
    color: #f8fafc;
    cursor: pointer;
  }}
  button:hover {{
    background: #475569;
  }}
  #startBtn {{
    background: #15803d;
    color: #ffffff;
    border-color: #166534;
  }}
  #startBtn:hover {{
    background: #16a34a;
  }}
  #stopBtn {{
    background: #b91c1c;
    color: #ffffff;
    border-color: #991b1b;
  }}
  #stopBtn:hover {{
    background: #dc2626;
  }}
  #resetBtn {{
    background: #475569;
    color: #ffffff;
    border-color: #334155;
  }}
  #resetBtn:hover {{
    background: #64748b;
  }}
  #exportBtn {{
    background: #1d4ed8;
    color: #ffffff;
    border-color: #1e40af;
  }}
  #exportBtn:hover {{
    background: #2563eb;
  }}
  .status {{
    font-size: 11px;
    color: #cbd5e1;
    margin-left: 4px;
  }}
  .timer {{
    font-size: 16px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: #cbd5e1;
  }}
  .running {{
    color: #4ade80;
  }}
</style>
</head>
<body>
<h1>miniMazinga Viewer</h1>

<a href="/leds" target="_blank" style="color:#38bdf8;font-size:12px;margin-left:4px;">Open Status LEDs page &rarr;</a>

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

<h2>Accumulated sum (<span id="countLabel">0</span> events)</h2>
<div class="grid">
{sum_cells_top}
{sum_cells_bottom}
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
      const sumEl = document.getElementById('sum-' + c);

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
    de10nano_index = 0    # set by main()

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = build_html(self.channels, self.de10nano_index).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif self.path == "/leds":
                body = build_leds_html(self.channels).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif self.path == "/background.jpg":
                try:
                    with open("background.jpg", "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except FileNotFoundError:
                    self.send_response(404)
                    self.end_headers()

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
            filename = export_to_file(self.channels, self.de10nano_index)
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
    global accum_sum, top_channels_global, bottom_channels_global, coinc_count

    parser = argparse.ArgumentParser(
        description="Live dashboard: LEDs above threshold + accumulated sum, for 8 channels."
    )
    parser.add_argument("--de10nano", type=int, default=0, help="de10nano index to read (0-7)")
    parser.add_argument("--channels", type=str, default="0,1,2,3,4,5,6,7",
                        help="Comma-separated list of 8 channel indices (0-1791), in display order.")
    parser.add_argument("--ip", type=str, default=UDP_IP, help="UDP bind IP")
    parser.add_argument("--port", type=int, default=UDP_PORT, help="UDP bind port")
    parser.add_argument("--http-port", type=int, default=8000, help="Local web server port")
    args = parser.parse_args()

    channels = [int(x.strip()) for x in args.channels.split(",")]
    if len(channels) != 8:
        raise SystemExit(f"Expected exactly 8 channels, got {len(channels)}: {channels}")

    accum_sum = {c: 0 for c in channels}
    top_channels_global = channels[0:4]
    bottom_channels_global = channels[4:8]
    coinc_count = {
        (t, b): 0 for t in top_channels_global for b in bottom_channels_global
    }

    Handler.channels = channels
    Handler.de10nano_index = args.de10nano

    stop_event = threading.Event()
    udp_thread = threading.Thread(
        target=udp_loop, args=(args.de10nano, channels, args.ip, args.port, stop_event), daemon=True
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