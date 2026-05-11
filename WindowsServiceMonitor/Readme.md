# WindowsServiceMonitor

A desktop application for monitoring Windows service resource usage in real time. View CPU and memory consumption for one or multiple services at once, with live graphs and historical data.

---

## Features

- **Single Service Monitor** — Select any Windows service and track its CPU and memory usage live with scrolling graphs showing the last 60 minutes of data.
- **Multi Watch** — Monitor up to 6 services simultaneously in a table view. Click any row to see detailed graphs and stats for that service.
- **Task Manager-scale CPU** — CPU percentages are normalized the same way Windows Task Manager displays them, so the numbers match what you're used to seeing.
- **Memory modes** — Switch between Private Memory and Working Set to match whichever metric matters to you.
- **Process details** — See PID, process name, executable path, service logon account, binary path, thread count, and process uptime.
- **Averages** — Running average CPU and memory are calculated across the entire watch window.
- **Customizable refresh rate** — Set the polling interval from 1 to 60 seconds.
- **Color themes** — Choose from six accent colors (Neon Blue, Green, Purple, Neon Yellow, Orange, Red) on a dark background.

---

## Requirements

- Windows 10 or 11
- Python 3.10+
- The following Python packages:

```
psutil
matplotlib
```

Install them with:

```bash
pip install psutil matplotlib
```

---

## Usage

Run the script directly:

```bash
python WindowsServiceMonitor.py
```

> **Note:** Some services require elevated permissions to read process information. Run as Administrator if you see missing CPU/memory data for certain services.

---

## How It Works

1. Launch the app — it automatically lists all Windows services sorted by status (running first).
2. **Single Service tab** — Pick a service from the dropdown and click **Start Monitoring**. CPU and memory graphs update on each polling interval.
3. **Multi Watch tab** — Assign services to up to 6 slots using the dropdowns, then click **Start Multi Watch**. The table updates live; click any row to see its graphs below.
4. Click **Stop** at any time to pause monitoring without losing graph history.
5. Use **Clear Graphs** / **Clear Multi** to reset the history and start fresh.

---

## Screenshots

_Add screenshots here_

---

## License

MIT License — free to use, modify, and distribute.
