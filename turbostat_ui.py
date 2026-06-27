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
current_interval = 1

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
    busy     = fv(s,"Busy%")
    bzy_mhz  = fv(s,"Bzy_MHz")

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
    ]

    # Dynamic Temperatures
    tmp_parts = []
    if "PkgTmp" in s or "PkgTmp" in TURBOSTAT_COLS:
        pkg_tmp = fv(s,"PkgTmp")
        tmp_parts.extend([("  PKG   ", C_LABEL), (f"{pkg_tmp:5.0f} °C  ", temp_color(pkg_tmp)), bar(pkg_tmp, 105, 12)])
    if "CoreTmp" in s or "CoreTmp" in TURBOSTAT_COLS:
        core_tmp = fv(s,"CoreTmp")
        tmp_parts.extend([("   CORE  ", C_LABEL), (f"{core_tmp:5.0f} °C", temp_color(core_tmp))])
    if tmp_parts:
        rows.append(Text.assemble(*tmp_parts))

    # Dynamic Watts
    watt_parts = []
    if "PkgWatt" in s or "PkgWatt" in TURBOSTAT_COLS:
        watt_parts.extend([("  PKG   ", C_LABEL), (f"{fv(s,'PkgWatt'):6.2f} W  ", C_PKG)])
    if "GFXWatt" in s or "GFXWatt" in TURBOSTAT_COLS:
        watt_parts.extend([("GFX  ", C_LABEL),    (f"{fv(s,'GFXWatt'):5.2f} W  ", C_GFX)])
    if "SysWatt" in s or "SysWatt" in TURBOSTAT_COLS:
        watt_parts.extend([("SYS  ", C_LABEL),    (f"{fv(s,'SysWatt'):5.2f} W",   C_VALUE)])
    if watt_parts:
        rows.append(Text.assemble(*watt_parts))

    # Dynamic GFX
    gfx_parts = []
    if "GFXMHz" in s or "GFXMHz" in TURBOSTAT_COLS:
        gfx_parts.extend([("  GFX   ", C_LABEL), (f"{fv(s,'GFXMHz'):>5.0f} MHz  ", C_GFX)])
    if "GFX%rc6" in s or "GFX%rc6" in TURBOSTAT_COLS:
        gfx_rc6 = fv(s,"GFX%rc6")
        gfx_parts.extend([("RC6  ", C_LABEL),    (f"{gfx_rc6:5.1f}%  ", C_GFX), bar(gfx_rc6, 100, 10)])
    if gfx_parts:
        rows.append(Text.assemble(*gfx_parts))

    # Dynamic C-States
    c_parts = []
    if "CPU%c1" in s or "CPU%c1" in TURBOSTAT_COLS:
        c1 = fv(s,"CPU%c1")
        c_parts.extend([("  C1    ", C_LABEL), (f"{c1:5.1f}%  ", C_IDLE), bar(c1, 100, 10)])
    if "CPU%c7" in s or "CPU%c7" in TURBOSTAT_COLS:
        c7 = fv(s,"CPU%c7")
        c_parts.extend([("   C7  ", C_LABEL),  (f"{c7:5.1f}%  ", C_IDLE), bar(c7, 100, 10)])
    if c_parts:
        rows.append(Text.assemble(*c_parts))

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

    # Synthetic Interval Item
    style_int = C_VALUE if menu_cursor == 0 else C_DIM
    prefix_int = "> " if menu_cursor == 0 else "  "
    rows.append(Text(f"{prefix_int}Interval: {current_interval}s", style=style_int))

    for i, col in enumerate(TURBOSTAT_COLS):
        idx = i + 1
        is_active = column_state.get(col, False)
        indicator = "●" if is_active else "○"

        style = C_VALUE if idx == menu_cursor else C_DIM
        if col == "CPU":
            style = C_HOT if idx == menu_cursor else C_LABEL

        prefix = "> " if idx == menu_cursor else "  "
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
    tbl.add_column("GRP", width=3, justify="center")
    tbl.add_column("CPU", style=C_LABEL, width=4, justify="right")

    active_cols = []
    for col in TURBOSTAT_COLS:
        if column_state.get(col, True) and col != "CPU":
            active_cols.append(col)
            if col == "Busy%":
                tbl.add_column("Busy %", width=22)
            else:
                tbl.add_column(col, justify="right")

    max_busy = max((fv(r, "Busy%") for r in cpu_rows), default=0)

    for r in cpu_rows:
        grp_id = r.get("_GRP", -1)
        if grp_id == -1:
            grp_cell = Text(" ", style=C_DIM)
        else:
            grp_style = C_VALUE if grp_id % 2 == 0 else C_DIM
            grp_cell = Text("║", style=grp_style)

        row_list = [grp_cell, str(r.get("CPU", "?"))]

        for col in active_cols:
            if col == "Busy%":
                busy = fv(r, "Busy%")
                busy_parts = [bar(busy, 100, 14), " ", (f"{busy:5.1f}%", busy_color(busy))]
                if busy > 0 and busy == max_busy:
                    busy_parts.append((" ◀", C_HOT))
                row_list.append(Text.assemble(*busy_parts))
            else:
                val_str = str(r.get(col, "0"))
                try:
                    val = float(val_str)
                    if "Tmp" in col:
                        row_list.append(Text(f"{val:.0f} °C", style=temp_color(val)))
                    elif "MHz" in col:
                        row_list.append(f"{val:.0f}")
                    elif "%" in col:
                        row_list.append(f"{val:.1f}")
                    elif col == "CoreThr":
                        row_list.append(Text("·", style=C_DIM) if val == 0 else val_str)
                    else:
                        row_list.append(val_str)
                except ValueError:
                    row_list.append(val_str)

        tbl.add_row(*row_list)
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
    global show_menu, menu_cursor, TURBOSTAT_COLS, column_state, current_interval
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", "-i", type=int, default=1)
    args = parser.parse_args()
    check_turbostat()

    current_interval = args.interval
    TURBOSTAT_COLS = discover_columns()
    column_state = {col: True for col in TURBOSTAT_COLS}

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    current_summary, current_cpu_rows = None, []
    proc = spawn_turbostat(current_interval)
    stream = run_turbostat(proc)
    needs_restart = False

    try:
        tty.setcbreak(fd)
        with Live(build_ui(current_summary, current_cpu_rows, current_interval),
                  screen=True, console=console, auto_refresh=False) as live:
            while True:
                ui_needs_update = False
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
                        ui_needs_update = True
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
                                        ui_needs_update = True
                                    elif key == 'B': # Down
                                        menu_cursor = min(len(TURBOSTAT_COLS), menu_cursor + 1)
                                        ui_needs_update = True
                                    elif key == 'C': # Right
                                        if menu_cursor == 0:
                                            current_interval += 1
                                            needs_restart = True
                                            ui_needs_update = True
                                    elif key == 'D': # Left
                                        if menu_cursor == 0:
                                            current_interval = max(1, current_interval - 1)
                                            needs_restart = True
                                            ui_needs_update = True
                        else:
                            show_menu = False
                            ui_needs_update = True
                    elif ch in (' ', '\n', '\r'):
                        if show_menu and menu_cursor > 0:
                            col = TURBOSTAT_COLS[menu_cursor - 1]
                            if col != "CPU":
                                column_state[col] = not column_state[col]
                                needs_restart = True
                                ui_needs_update = True

                if needs_restart:
                    proc.terminate()
                    proc.wait()
                    proc = spawn_turbostat(current_interval)
                    stream = run_turbostat(proc)
                    needs_restart = False

                try:
                    chunk = next(stream)
                    if chunk is not None:
                        new_summary, new_cpu_rows = parse_block(chunk)
                        if new_summary is not None:
                            current_summary, current_cpu_rows = new_summary, new_cpu_rows
                            busy_history.append(fv(current_summary, "Busy%"))
                            ui_needs_update = True
                except StopIteration:
                    if proc.poll() is not None:
                        if needs_restart is False:
                            proc = spawn_turbostat(current_interval)
                            stream = run_turbostat(proc)
                        else:
                            break
                    pass

                if ui_needs_update:
                    live.update(build_ui(current_summary, current_cpu_rows, current_interval))
                    live.refresh()
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
