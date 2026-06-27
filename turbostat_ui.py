#!/usr/bin/env python3
"""
turbostat_ui.py — Live CPU metrics dashboard usando turbostat + Rich TUI
Uso: sudo python turbostat_ui.py [--interval N]
"""

import subprocess, sys, time, argparse, shutil
from typing import Optional

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
except ImportError:
    print("Instalá rich: sudo pacman -S python-rich")
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

TURBOSTAT_COLS = [
    "CPU","Busy%","Bzy_MHz","CPU%c1","CPU%c7",
    "CoreTmp","CoreThr","PkgTmp","GFX%rc6","GFXMHz",
    "PkgWatt","GFXWatt","SysWatt"
]

def check_turbostat():
    if not shutil.which("turbostat"):
        console.print("[bold red]✗ turbostat no encontrado.[/]")
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

    rows = [
        Text.assemble(
            ("  BUSY  ", C_LABEL), (f"{busy:5.1f}%  ", busy_color(busy)),
            bar(busy, 100),
            ("   FREQ  ", C_LABEL), (f"{bzy_mhz:>6.0f} MHz", C_VALUE),
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

def make_cpu_table(cpu_rows: list) -> Table:
    tbl = Table(
        box=box.SIMPLE_HEAD,
        header_style=C_HEADER,
        border_style=C_BORDER,
        show_edge=False,
        pad_edge=False,
        expand=True,
    )
    tbl.add_column("CPU",    style=C_LABEL, width=4,  justify="right")
    tbl.add_column("MHz",    style=C_VALUE, width=6,  justify="right")
    tbl.add_column("Busy %", width=22)
    tbl.add_column("CoreT",  width=7,  justify="right")
    tbl.add_column("C1 %",   style=C_IDLE, width=7,  justify="right")
    tbl.add_column("C7 %",   style=C_IDLE, width=7,  justify="right")
    tbl.add_column("Thr",    style=C_WARM, width=4,  justify="center")

    for r in cpu_rows:
        busy = fv(r,"Busy%"); mhz = fv(r,"Bzy_MHz")
        ctmp = fv(r,"CoreTmp"); c1 = fv(r,"CPU%c1"); c7 = fv(r,"CPU%c7")
        thr  = r.get("CoreThr") or "0"

        busy_cell = Text.assemble(bar(busy, 100, 14), " ", (f"{busy:5.1f}%", busy_color(busy)))
        tbl.add_row(
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
    ts = time.strftime("%H:%M:%S")
    title = Text.assemble(
        ("╸ TURBOSTAT MONITOR   ", C_HEADER),
        (ts, C_DIM), (f"  interval={interval}s", C_DIM),
    )
    top = make_summary_panel(summary) if summary else Panel("[dim]Esperando…[/dim]", border_style=C_BORDER)
    bot = Panel(
        make_cpu_table(cpu_rows) if cpu_rows else Text("Sin datos por CPU aún", style=C_DIM),
        title=f"[{C_HEADER}] PER-CPU [/{C_HEADER}]",
        border_style=C_BORDER, padding=(0,1),
    )
    hint = Text("  Ctrl+C para salir", style=C_DIM, justify="right")
    return Group(title, top, bot, hint)

def run_turbostat(interval):
    cmd = ["turbostat", "--interval", str(interval), "--quiet",
           "--show", ",".join(TURBOSTAT_COLS)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    block = []
    for line in proc.stdout:
        s = line.strip()
        if s.startswith("CPU"):
            if block:
                yield "\n".join(block)
            block = [s]
        elif block:
            block.append(s)
    if block:
        yield "\n".join(block)
    proc.wait()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", "-i", type=int, default=1)
    args = parser.parse_args()
    check_turbostat()

    summary, cpu_rows = None, []
    try:
        with Live(build_ui(summary, cpu_rows, args.interval),
                  refresh_per_second=4, screen=True, console=console) as live:
            for chunk in run_turbostat(args.interval):
                summary, cpu_rows = parse_block(chunk)
                live.update(build_ui(summary, cpu_rows, args.interval))
    except KeyboardInterrupt:
        console.print(f"\n[{C_LABEL}]Hasta luego.[/{C_LABEL}]")

if __name__ == "__main__":
    main()
