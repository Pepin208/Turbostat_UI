#!/usr/bin/env python3
"""
turbostat_ui.py — Live CPU metrics dashboard usando turbostat + Rich TUI
Uso: sudo python turbostat_ui.py [--interval N]
"""

import subprocess, sys, time, argparse, shutil, select, termios, tty, os
from typing import Optional

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
except ImportError:
    print("Install rich: sudo pacman -S python-rich")
    sys.exit(1)

# Forzar color aunque sudo/TERM no lo detecte bien
console = Console(force_terminal=True, force_jupyter=False, color_system="truecolor")

C_HEADER = "bold #C8D8E8"
C_LABEL  = "#7A9CBF"
C_VALUE  = "bold white"
C_HOT    = "#FF6B6B"
C_WARM   = "#FFD166"
C_COOL   = "#06D6A0"
C_DIM    = "#4A6080"
C_GFX    = "#BB86FC"
C_PKG    = "#FFB347"
C_IDLE   = "#56CFE1"
C_BORDER = "#2A4A6A"

import collections
busy_history = collections.deque(maxlen=30)

TURBOSTAT_COLS = []
show_menu = False
menu_cursor = 0
column_state = {}

def discover_columns() -> list:
    try:
        res = subprocess.run(["turbostat", "-n", "1", "--quiet"], capture_output=True, text=True)
        lines = res.stdout.strip().splitlines()
        for line in lines:
            if line.startswith("CPU"):
                header = line.split()
                if "CPU" in header:
                    header.remove("CPU")
                header.insert(0, "CPU")
                return header
    except Exception:
        pass
    # Fallback default
    return ["CPU","Busy%","Bzy_MHz","CPU%c1","CPU%c7","CoreTmp","CoreThr","PkgTmp","PkgWatt","SysWatt"]

def check_turbostat():
    if not shutil.which("turbostat"):
        console.print("[bold red]✗ turbostat not found.[/]")
        sys.exit(1)

def fv(d: dict, key: str, default=0.0) -> float:
    try: return float(d.get(key) or default)
    except: return default

def parse_block(raw: str):
    """Devuelve (summary_dict, [cpu_dicts])"""
    lines = [l for l in raw.splitlines() if l.strip()]
    header = None
    summary, cpus = None, []
    # Rellenar columnas faltantes usando la fila anterior (HT comparte datos)
    prev = {}
    for line in lines:
        parts = line.split("\t") if "\t" in line else line.split()
        if not parts: continue
        if parts[0] == "CPU":
            header = parts
            continue
        if header is None: continue
        row = {}
        for i, col in enumerate(header):
            row[col] = parts[i] if i < len(parts) else None
        # Heredar valores del core físico para hilos HT
        for col in header:
            if row.get(col) is None:
                row[col] = prev.get(col)
        prev = row
        if row.get("CPU") == "-":
            summary = row
        else:
            cpus.append(row)

    # HT Grouping (Feature 3): Group by identical non-zero CoreTmp
    tmp_groups = {}
    next_grp_id = 0
    for r in cpus:
        tmp_val = fv(r, "CoreTmp")
        if tmp_val > 0:
            if tmp_val not in tmp_groups:
                tmp_groups[tmp_val] = next_grp_id
                next_grp_id += 1
            r["_GRP"] = tmp_groups[tmp_val]
        else:
            r["_GRP"] = -1

    cpus.sort(key=lambda r: int(r.get("CPU", 0) or 0))
    return summary, cpus

def temp_color(t: float) -> str:
    if t >= 90: return C_HOT
    if t >= 70: return C_WARM
    return C_COOL

def busy_color(b: float) -> str:
    if b >= 80: return C_HOT
    if b >= 40: return C_WARM
    return C_COOL

def bar(value: float, total: float, width: int = 12) -> Text:
    pct    = min(value / total, 1.0) if total else 0
    filled = int(pct * width)
    empty  = width - filled
    color  = busy_color(value) if total == 100 else C_GFX
    return Text.assemble((f"{'█'*filled}", color), (f"{'░'*empty}", C_DIM))

def make_summary_panel(s: dict) -> Panel:
    pkg_tmp  = fv(s,"PkgTmp")
    core_tmp = fv(s,"CoreTmp")
    busy     = fv(s,"Busy%")
    bzy_mhz  = fv(s,"Bzy_MHz")
    pkg_w    = fv(s,"PkgWatt")
    gfx_w    = fv(s,"GFXWatt")
    sys_w    = fv(s,"SysWatt")
    gfx_rc6  = fv(s,"GFX%rc6")
    gfx_mhz  = fv(s,"GFXMHz")
    c1       = fv(s,"CPU%c1")
    c7       = fv(s,"CPU%c7")

    blocks = "▁▂▃▄▅▆▇█"
    max_b = max(busy_history) if busy_history else 0
    spark_chars = []
    for val in busy_history:
        idx = int((val / max_b) * 7) if max_b > 0 else 0
        spark_chars.append(blocks[idx])
    sparkline = "".join(spark_chars)

    rows = [
        Text.assemble(
            ("  BUSY  ", C_LABEL), (f"{busy:5.1f}%  ", busy_color(busy)),
            bar(busy, 100),
            ("   FREQ  ", C_LABEL), (f"{bzy_mhz:>6.0f} MHz", C_VALUE),
        ),
        Text.assemble(
            ("  HIST  ", C_LABEL), (f"{sparkline:<30} ", C_VALUE),
            ("PEAK ", C_LABEL), (f"{max_b:5.1f}%", C_HOT),
        ),
        Text.assemble(
            ("  PKG   ", C_LABEL), (f"{pkg_tmp:5.0f} °C  ", temp_color(pkg_tmp)),
            bar(pkg_tmp, 105, 12),
            ("   CORE  ", C_LABEL), (f"{core_tmp:5.0f} °C", temp_color(core_tmp)),
        ),
        Text.assemble(
            ("  PKG   ", C_LABEL), (f"{pkg_w:6.2f} W  ", C_PKG),
            ("GFX  ", C_LABEL),    (f"{gfx_w:5.2f} W  ", C_GFX),
            ("SYS  ", C_LABEL),    (f"{sys_w:5.2f} W",   C_VALUE),
        ),
        Text.assemble(
            ("  GFX   ", C_LABEL), (f"{gfx_mhz:>5.0f} MHz  ", C_GFX),
            ("RC6  ", C_LABEL),    (f"{gfx_rc6:5.1f}%  ", C_GFX),
            bar(gfx_rc6, 100, 10),
        ),
        Text.assemble(
            ("  C1    ", C_LABEL), (f"{c1:5.1f}%  ", C_IDLE),
            bar(c1, 100, 10),
            ("   C7  ", C_LABEL),  (f"{c7:5.1f}%  ", C_IDLE),
            bar(c7, 100, 10),
        ),
    ]

    from rich.console import Group
    return Panel(
        Group(*rows),
        title=f"[{C_HEADER}] ⚡ PACKAGE SUMMARY [/{C_HEADER}]",
        border_style=C_BORDER,
        padding=(0, 1),
    )

def make_menu_panel() -> Panel:
    from rich.console import Group
    rows = []
    for i, col in enumerate(TURBOSTAT_COLS):
        is_active = column_state.get(col, False)
        indicator = "●" if is_active else "○"

        style = C_VALUE if i == menu_cursor else C_DIM
        if col == "CPU":
            style = C_HOT if i == menu_cursor else C_LABEL

        prefix = "> " if i == menu_cursor else "  "
        rows.append(Text(f"{prefix}{indicator} {col}", style=style))

    return Panel(
        Group(*rows),
        title=f"[{C_HEADER}] ⚙ MENU [/{C_HEADER}]",
        border_style=C_BORDER,
        padding=(1, 4),
        expand=False
    )

def make_cpu_table(cpu_rows: list) -> Table:
    tbl = Table(
        box=box.SIMPLE_HEAD,
        header_style=C_HEADER,
        border_style=C_BORDER,
        show_edge=False,
        pad_edge=False,
        expand=True,
    )
    tbl.add_column("GRP",    width=3,  justify="center")
    tbl.add_column("CPU",    style=C_LABEL, width=4,  justify="right")
    tbl.add_column("MHz",    style=C_VALUE, width=6,  justify="right")
    tbl.add_column("Busy %", width=22)
    tbl.add_column("CoreT",  width=7,  justify="right")
    tbl.add_column("C1 %",   style=C_IDLE, width=7,  justify="right")
    tbl.add_column("C7 %",   style=C_IDLE, width=7,  justify="right")
    tbl.add_column("Thr",    style=C_WARM, width=4,  justify="center")

    max_busy = max((fv(r, "Busy%") for r in cpu_rows), default=0)

    for r in cpu_rows:
        busy = fv(r,"Busy%"); mhz = fv(r,"Bzy_MHz")
        ctmp = fv(r,"CoreTmp"); c1 = fv(r,"CPU%c1"); c7 = fv(r,"CPU%c7")
        thr  = r.get("CoreThr") or "0"

        grp_id = r.get("_GRP", -1)
        if grp_id == -1:
            grp_cell = Text(" ", style=C_DIM)
        else:
            grp_style = C_VALUE if grp_id % 2 == 0 else C_DIM
            grp_cell = Text("║", style=grp_style)

        busy_parts = [bar(busy, 100, 14), " ", (f"{busy:5.1f}%", busy_color(busy))]
        if busy > 0 and busy == max_busy:
            busy_parts.append((" ◀", C_HOT))

        busy_cell = Text.assemble(*busy_parts)

        tbl.add_row(
            grp_cell,
            r.get("CPU","?"),
            f"{mhz:.0f}",
            busy_cell,
            Text(f"{ctmp:.0f} °C", style=temp_color(ctmp)),
            f"{c1:.1f}",
            f"{c7:.1f}",
            Text("·", style=C_DIM) if thr == "0" else thr,
        )
    return tbl

def build_ui(summary, cpu_rows, interval):
    from rich.console import Group
    from rich.align import Align
    ts = time.strftime("%H:%M:%S")
    title = Text.assemble(
        ("╸ TURBOSTAT MONITOR   ", C_HEADER),
        (ts, C_DIM), (f"  interval={interval}s", C_DIM),
        ("   [M] Menu   [Q] Quit", C_VALUE)
    )
    top = make_summary_panel(summary) if summary else Panel("[dim]Waiting...[/dim]", border_style=C_BORDER)

    if show_menu:
        bot = Align.center(make_menu_panel())
    else:
        bot = Panel(
            make_cpu_table(cpu_rows) if cpu_rows else Text("No per-CPU data available yet", style=C_DIM),
            title=f"[{C_HEADER}] PER-CPU [/{C_HEADER}]",
            border_style=C_BORDER, padding=(0,1),
        )

    hint = Text("  Ctrl+C to exit", style=C_DIM, justify="right")
    return Group(title, top, bot, hint)

def spawn_turbostat(interval):
    active_cols = [col for col in TURBOSTAT_COLS if column_state.get(col, True)]
    # Ensure CPU is always requested
    if "CPU" not in active_cols:
        active_cols.insert(0, "CPU")
    cmd = ["turbostat", "--interval", str(interval), "--quiet",
           "--show", ",".join(active_cols)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    os.set_blocking(proc.stdout.fileno(), False)
    return proc

def run_turbostat(proc):
    block = []
    while True:
        try:
            line = proc.stdout.readline()
        except BlockingIOError:
            line = None

        if line is None or line == "":
            if block:
                yield "\n".join(block)
                block = []
            else:
                yield None

            if proc.poll() is not None:
                # One last attempt to drain
                try:
                    line = proc.stdout.readline()
                    if not line:
                        break
                except BlockingIOError:
                    break
            continue

        s = line.strip()
        if not s:
            continue

        if s.startswith("CPU"):
            if block:
                yield "\n".join(block)
            block = [s]
        elif block:
            block.append(s)

    if block:
        yield "\n".join(block)

def main():
    global show_menu, menu_cursor, TURBOSTAT_COLS, column_state
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", "-i", type=int, default=1)
    args = parser.parse_args()
    check_turbostat()

    TURBOSTAT_COLS = discover_columns()
    column_state = {col: True for col in TURBOSTAT_COLS}

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    current_summary, current_cpu_rows = None, []
    proc = spawn_turbostat(args.interval)
    stream = run_turbostat(proc)
    needs_restart = False

    try:
        tty.setcbreak(fd)
        with Live(build_ui(current_summary, current_cpu_rows, args.interval),
                  refresh_per_second=4, screen=True, console=console) as live:
            while True:
                # Handle Non-Blocking Keyboard Input
                while True:
                    dr, dw, de = select.select([sys.stdin], [], [], 0.0)
                    if not dr:
                        break
                    try:
                        ch_bytes = os.read(sys.stdin.fileno(), 1)
                    except BlockingIOError:
                        break
                    if not ch_bytes:
                        break
                    ch = ch_bytes.decode('utf-8', errors='ignore')
                    if ch in ('q', 'Q'):
                        return
                    elif ch in ('m', 'M'):
                        show_menu = not show_menu
                    elif ch == '\x1b':
                        # Handle escape sequence or plain Esc
                        dr2, dw2, de2 = select.select([sys.stdin], [], [], 0.0)
                        if dr2:
                            try:
                                seq = os.read(sys.stdin.fileno(), 1).decode('utf-8', errors='ignore')
                            except BlockingIOError:
                                seq = ''
                            if seq == '[':
                                dr3, dw3, de3 = select.select([sys.stdin], [], [], 0.0)
                                if dr3:
                                    try:
                                        key = os.read(sys.stdin.fileno(), 1).decode('utf-8', errors='ignore')
                                    except BlockingIOError:
                                        key = ''
                                    if key == 'A': # Up
                                        menu_cursor = max(0, menu_cursor - 1)
                                    elif key == 'B': # Down
                                        menu_cursor = min(len(TURBOSTAT_COLS) - 1, menu_cursor + 1)
                        else:
                            show_menu = False
                    elif ch in (' ', '\n', '\r'):
                        if show_menu:
                            col = TURBOSTAT_COLS[menu_cursor]
                            if col != "CPU":
                                column_state[col] = not column_state[col]
                                needs_restart = True

                if needs_restart:
                    proc.terminate()
                    proc.wait()
                    proc = spawn_turbostat(args.interval)
                    stream = run_turbostat(proc)
                    needs_restart = False

                try:
                    chunk = next(stream)
                    if chunk is not None:
                        new_summary, new_cpu_rows = parse_block(chunk)
                        if new_summary is not None:
                            current_summary, current_cpu_rows = new_summary, new_cpu_rows
                            busy_history.append(fv(current_summary, "Busy%"))
                except StopIteration:
                    if proc.poll() is not None:
                        if needs_restart is False:
                            proc = spawn_turbostat(args.interval)
                            stream = run_turbostat(proc)
                        else:
                            break
                    pass

                live.update(build_ui(current_summary, current_cpu_rows, args.interval))
                time.sleep(0.05)

    except KeyboardInterrupt:
        console.print(f"\n[{C_LABEL}]Goodbye.[/{C_LABEL}]")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        if proc.poll() is None:
            proc.terminate()
            proc.wait()

if __name__ == "__main__":
    main()
