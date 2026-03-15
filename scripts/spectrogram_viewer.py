# scripts/spectrogram_viewer.py
#RFEnvironmentSimulator -> IQ frames -> FFT -> waterfall/spectrogram
#python scripts/spectrogram_viewer.py


from __future__ import annotations

import sys
from pathlib import Path
import matplotlib

try:
    matplotlib.use("MacOSX")  # ensures a GUI backend on macOS terminals
except Exception:  
    matplotlib.use("Agg")

import matplotlib.pyplot as plt  # type: ignore
import numpy as np



# Ensure project root is importable when running as a script:
#   python scripts/spectrogram_viewer.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.sim.rf_environment import RFEnvironmentConfig, RFEnvironmentSimulator


def main() -> None:
    # Viewer / signal parameters
    center_freq_hz = 433e6
    sample_rate_hz = 2_000_000.0
    fft_size = 1024
    fps = 20
   

    # Simulator
    sim = RFEnvironmentSimulator(RFEnvironmentConfig(seed=1337))

    # Collect waterfall rows
    history = 200  # number of visible waterfall rows

    # Frequency axis
    freqs_hz = np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / sample_rate_hz))
    freqs_mhz = (center_freq_hz + freqs_hz) / 1e6

    # Waterfall buffer
    waterfall = np.zeros((history, fft_size), dtype=np.float32)

    plt.figure(figsize=(12, 6))  # type: ignore
    im = plt.imshow(  # type: ignore
        waterfall,
        aspect="auto",
        origin="lower",
        cmap="inferno",
        extent=(float(freqs_mhz[0]), float(freqs_mhz[-1]), 0.0, float(history)),
        vmin=-120,
        vmax=20,
    )

    plt.colorbar(label="Power (dB)")  # type: ignore
    plt.xlabel("Frequency (MHz)")  # type: ignore
    plt.ylabel("Time")  # type: ignore
    plt.title("TALOS Live SDR Waterfall")  # type: ignore

    plt.ion() # type: ignore

    while True:
        iq = sim.next_iq_frame(
            num_samples=fft_size,
            sample_rate_hz=sample_rate_hz,
            center_freq_hz=center_freq_hz,
        )

        window = np.hanning(len(iq))
        spectrum = np.fft.fftshift(np.fft.fft(iq * window, n=fft_size))
        power_db = 20.0 * np.log10(np.abs(spectrum) + 1e-12)

        # scroll waterfall
        waterfall = np.roll(waterfall, -1, axis=0)
        waterfall[-1, :] = power_db

        im.set_data(waterfall)

        plt.pause(1.0 / fps)   # ~20 FPS

if __name__ == "__main__":
    main()