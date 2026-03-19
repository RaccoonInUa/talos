# src/services/sdr_monitor.py

from __future__ import annotations

from uuid import uuid4
from typing import Callable, Optional, cast

import numpy as np
from numpy.typing import NDArray

from src.core.bus import TalosQueue
from src.core.service import BaseService
from src.core.types import CfarEvent, ProcessingConfig, SdrConfig, TalosConfig, WaterfallFrame, utc_now
from src.hal.sdr import SdrDriver


class SdrMonitor(BaseService):
    """
    SDR Monitor Service (Soft Real-Time DSP)

    ROLE IN PIPELINE:
    - Reads IQ frames from HAL (SdrDriver)
    - Runs FFT -> Power Spectrum (dB)
    - Applies 1D CFAR (moving average noise floor + threshold)
    - Emits CfarEvent into IPC bus (TalosQueue)

    CANONICAL CONTRACT: "All or Nothing" frames
    - HAL returns either:
        (A) array with EXACT length == fft_size -> valid DSP frame
        (B) EMPTY array (len == 0)             -> Graceful Skip
      Any mismatch in size is treated as invalid and skipped.

    WHY THIS MATTERS:
    - Partial / zero-padded tails into FFT create discontinuity artifacts.
    - CFAR will interpret artifacts as signals -> false positives.
    - So we enforce strict frame size equality.

    TYPING NOTE (Pylance strict):
    - SciPy stubs are often missing or incomplete in many environments.
    - To keep the code "canonically typed" without adding extra dependencies,
      we:
        1) Avoid direct SciPy imports where Pylance complains (hann/convolve1d).
        2) Use NumPy for the Hann window (fast, correct, and perfectly typed).
        3) Implement CFAR noise-floor convolution in NumPy (O(N*K), but K is small).
           This removes Pylance "unknown" issues entirely.
      If later you want SciPy speed, we can restore convolve1d behind a typed wrapper.
    """

    _LOG_SKIP_EVERY_N = 500
    _LOG_EVENT_EVERY_N = 200
    _LOG_WATERFALL_DROP_EVERY_N = 200
    _WATERFALL_NUM_BINS = 256
    _WATERFALL_DB_MIN = -120.0
    _WATERFALL_DB_MAX = 0.0

    def __init__(
        self,
        sdr_config: SdrConfig,
        dsp_config: ProcessingConfig,
        output_queue: TalosQueue[CfarEvent],
        global_config: TalosConfig,
        waterfall_queue: TalosQueue[WaterfallFrame] | None = None,
    ):
        super().__init__(name="sdr_monitor", loop_sleep_s=0.0)

        self.sdr_config = sdr_config
        self.dsp_config = dsp_config
        self.queue = output_queue
        self.waterfall_queue = waterfall_queue
        self.global_config = global_config

        self.driver: Optional[SdrDriver] = None

        # Cached Hann window (float32)
        self._window: Optional[NDArray[np.float32]] = None

        # Observability counters (local process)
        self._frames_ok: int = 0
        self._frames_skipped: int = 0
        self._events_emitted: int = 0
        self._waterfall_frames_emitted: int = 0
        self._waterfall_frames_dropped: int = 0
        self._frame_seq: int = 0

        # Cache values used in hot path
        self._fft_size: int = int(self.dsp_config.fft_size)
        self._freq_step_hz: float = float(self.sdr_config.sample_rate_hz / self._fft_size)
        self._waterfall_num_bins: int = min(self._WATERFALL_NUM_BINS, self._fft_size)
        self._waterfall_bin_hz: float = float(
            self.sdr_config.sample_rate_hz / self._waterfall_num_bins
        )

        # CFAR kernel cached (float32) to avoid reallocations
        self._cfar_kernel: Optional[NDArray[np.float32]] = None

        # NOTE: Ideally move these into ProcessingConfig in next iteration.
        self._cfar_guard_cells: int = 4
        self._cfar_training_cells: int = 8
        
        # CFAR / clustering metrics
        self._cfar_detections: int = 0
        self._clusters_emitted: int = 0

        # Pre-bound method pointer for noise floor estimation (hot path micro-optimization)
        self._noise_floor_fn: Optional[
            Callable[[NDArray[np.float64], NDArray[np.float32]], NDArray[np.float64]]
        ] = None

    def setup(self) -> None:
        """
        Called once before the service loop starts.

        We do:
        - instantiate + connect SDR driver (or emulator)
        - create and cache Hann window for FFT
        - build and cache CFAR kernel
        - bind noise floor function pointer
        """
        self.logger.info("Initializing DSP Engine: FFT=%d", self._fft_size)

        self.driver = SdrDriver(self.sdr_config, global_config=self.global_config)
        self.driver.connect()

        # Hann window:
        # We intentionally use NumPy here to avoid SciPy stub issues in strict Pylance.
        # np.hanning returns float64; we cast once to float32 for cheaper hot-path multiply.
        win64 = np.hanning(self._fft_size).astype(np.float32, copy=False)
        self._window = cast(NDArray[np.float32], win64)

        self._cfar_kernel = self._build_cfar_kernel(
            guard_cells=self._cfar_guard_cells,
            training_cells=self._cfar_training_cells,
        )

        # Bind the implementation used for noise floor:
        # - Pure NumPy "valid + padding" convolution with tiny kernel.
        # - Deterministic, fully typed, no SciPy stubs needed.
        self._noise_floor_fn = self._noise_floor_numpy

        self.logger.info(
            "DSP Ready: SR=%.2f MS/s, Center=%.2f MHz, bin=%.1f Hz, WF=%d bins (%.1f Hz/bin), CFAR: train=%d guard=%d thr=%.2f dB",
            self.sdr_config.sample_rate_hz / 1e6,
            self.sdr_config.center_freq_hz / 1e6,
            self._freq_step_hz,
            self._waterfall_num_bins,
            self._waterfall_bin_hz,
            self._cfar_training_cells,
            self._cfar_guard_cells,
            self.dsp_config.cfar_threshold_db,
        )

    def execute(self) -> None:
        """
        One iteration of the service loop.

        Contract:
        - Only process frames that are EXACT length == fft_size.
        - Otherwise: Graceful Skip (return).
        """
        if (
            not self.driver
            or self._window is None
            or self._cfar_kernel is None
            or self._noise_floor_fn is None
        ):
            self._frames_skipped += 1
            return

        samples = self.driver.read_samples(self._fft_size)

        # STRICT CONTRACT CHECK (All-or-Nothing)
        if len(samples) != self._fft_size:
            self._frames_skipped += 1
            if self._frames_skipped % self._LOG_SKIP_EVERY_N == 1:
                self.logger.warning(
                    "Skipping frame (invalid length). Got=%d Expected=%d Skipped=%d OK=%d",
                    len(samples),
                    self._fft_size,
                    self._frames_skipped,
                    self._frames_ok,
                )
            return

        self._frames_ok += 1
        self._frame_seq += 1
        if self._frames_ok % 5000 == 1:
            compression = (
                float(self._cfar_detections) / self._clusters_emitted
                if self._clusters_emitted > 0
                else 0.0
            )

            self.logger.info(
                "DSP metrics | frames=%d skipped=%d cfar=%d clusters=%d events=%d wf_ok=%d wf_drop=%d compression=%.1f",
                self._frames_ok,
                self._frames_skipped,
                self._cfar_detections,
                self._clusters_emitted,
                self._events_emitted,
                self._waterfall_frames_emitted,
                self._waterfall_frames_dropped,
                compression,
            )

        # DSP: Window -> FFT -> Power(dB)
        windowed = samples * self._window
        spectrum = np.fft.fft(windowed)
        spectrum = np.fft.fftshift(spectrum)
        power = spectrum.real**2 + spectrum.imag**2
        power_db = 10.0 * np.log10(power + 1e-12)

        # power_db is ndarray[float64] in practice; make it explicit for type-checking.
        power_db_t = power_db

        self._emit_waterfall_frame(power_db_t, self._frame_seq)
        self._run_cfar(power_db_t, self._frame_seq)

    def teardown(self) -> None:
        self.logger.info(
            "Tearing down SdrMonitor... OK=%d Skipped=%d Events=%d WF_OK=%d WF_Drop=%d CFAR=%d Clusters=%d",
            self._frames_ok,
            self._frames_skipped,
            self._events_emitted,
            self._waterfall_frames_emitted,
            self._waterfall_frames_dropped,
            self._cfar_detections,
            self._clusters_emitted,
        )
        if self.driver:
            self.driver.close()

    def _emit_waterfall_frame(
        self,
        power_db: NDArray[np.float64],
        frame_seq: int,
    ) -> None:
        """
        Emit a lightweight visualization frame.

        IMPORTANT:
        - This path is UI-only and must NEVER affect detection quality.
        - CFAR continues to operate on the full-resolution `power_db` array.
        - Waterfall frames are allowed to drop under load (lossy telemetry).
        """
        if self.waterfall_queue is None:
            return

        line = self._compress_power_for_waterfall(power_db)
        frame = WaterfallFrame(
            frame_seq=frame_seq,
            timestamp=utc_now(),
            center_freq_hz=float(self.sdr_config.center_freq_hz),
            bin_hz=self._waterfall_bin_hz,
            line_uint8=line.tobytes(),
        )

        if self.waterfall_queue.push_nowait(frame):
            self._waterfall_frames_emitted += 1
        else:
            self._waterfall_frames_dropped += 1
            if self._waterfall_frames_dropped % self._LOG_WATERFALL_DROP_EVERY_N == 1:
                self.logger.warning(
                    "Waterfall frame dropped: queue full (drops=%d)",
                    self._waterfall_frames_dropped,
                )

    def _compress_power_for_waterfall(
        self,
        power_db: NDArray[np.float64],
    ) -> NDArray[np.uint8]:
        """
        Downsample + normalize + quantize the full-resolution spectrum for UI.

        Detection safety:
        - CFAR uses the original `power_db`.
        - This method is a visualization-only branch.
        """
        num_bins = self._waterfall_num_bins
        if power_db.shape[0] == num_bins:
            reduced = power_db
        else:
            edges = np.linspace(0, power_db.shape[0], num_bins + 1, dtype=np.int32)
            reduced = np.empty(num_bins, dtype=np.float64)
            for i in range(num_bins):
                start = int(edges[i])
                end = int(edges[i + 1])
                if end <= start:
                    end = min(power_db.shape[0], start + 1)
                reduced[i] = float(np.max(power_db[start:end]))

        db_min = self._WATERFALL_DB_MIN
        db_max = self._WATERFALL_DB_MAX
        clipped = np.clip(reduced, db_min, db_max)
        scaled = (clipped - db_min) * (255.0 / (db_max - db_min))
        return scaled.astype(np.uint8, copy=False)

    # --- CFAR ---

    def _build_cfar_kernel(self, guard_cells: int, training_cells: int) -> NDArray[np.float32]:
        """
        Build normalized CFAR kernel:
            [1..1, 0..0, 1..1] / (2 * training_cells)

        Kernel is float32 to reduce memory bandwidth in hot path.
        """
        if guard_cells < 0 or training_cells <= 0:
            raise ValueError("Invalid CFAR parameters: guard_cells>=0, training_cells>0 required")

        kernel_size = (2 * training_cells) + (2 * guard_cells) + 1
        kernel = np.ones(kernel_size, dtype=np.float32)

        start_guard = training_cells
        end_guard = start_guard + (2 * guard_cells) + 1
        kernel[start_guard:end_guard] = 0.0

        kernel /= float(2 * training_cells)
        return kernel

    def _noise_floor_numpy(
        self,
        power_db: NDArray[np.float64],
        kernel: NDArray[np.float32],
    ) -> NDArray[np.float64]:
        """
        Noise floor estimate via 1D convolution with boundary handling "nearest".

        Why this implementation:
        - Fully typed (no SciPy stub problems).
        - Kernel is small (train=8, guard=4 => kernel_size=25), so O(N*K) is acceptable for MVP.
        - Boundary handling matches SciPy mode='nearest' by padding edge values.

        Returns:
            noise_floor: NDArray[np.float64] same shape as power_db
        """
        # Convert kernel to float64 for numeric stability in accumulation
        k = kernel.astype(np.float64, copy=False)

        radius = int((k.shape[0] - 1) // 2)

        # mode='nearest' padding (edge values repeated)
        padded = np.pad(power_db, (radius, radius), mode="edge")

        # Convolution (direct, small kernel)
        # We intentionally keep this simple and predictable.
        out = np.empty_like(power_db, dtype=np.float64)
        for i in range(power_db.shape[0]): #У production потім можна замінити на:scipy.ndimage.convolve1d або numba
            # window is length kernel_size
            w = padded[i : i + k.shape[0]]
            out[i] = float(np.sum(w * k))
        return out

    def _run_cfar(self, power_db: NDArray[np.float64], source_frame_seq: int) -> None:
        """
        CA-CFAR-ish detection:
        - noise_floor = moving average of training cells (excluding guard/CUT)
        - detection if power_db > noise_floor + threshold_db
        - emit strongest detected bin per cluster (1D Non-Maximum Suppression)
        """
        if self._cfar_kernel is None or self._noise_floor_fn is None:
            return

        threshold_db = float(self.dsp_config.cfar_threshold_db)

        noise_floor = self._noise_floor_fn(power_db, self._cfar_kernel)

        detections = power_db > (noise_floor + threshold_db)

        peaks = np.where(detections)[0]
        if len(peaks) == 0:
            return
        self._cfar_detections += len(peaks)
        if len(peaks) > 200:
            self.logger.debug(
                "Large CFAR burst detected: %d bins",
                len(peaks),
            )

        # --- CFAR Peak Clustering (1D Non‑Maximum Suppression) ---
        # Minimum separation between independent signals (~50 kHz)
        bin_hz = self._freq_step_hz
        separation_hz = float(getattr(self.dsp_config, "signal_separation_hz", 50_000))
        min_distance_bins = max(1, int(separation_hz / bin_hz))

        clusters: list[list[int]] = []
        current_cluster: list[int] = [int(peaks[0])]

        for i in range(1, len(peaks)):
            if peaks[i] - peaks[i - 1] <= min_distance_bins:
                current_cluster.append(int(peaks[i]))
            else:
                clusters.append(current_cluster)
                current_cluster = [int(peaks[i])]

        clusters.append(current_cluster)

        # Emit strongest bin per cluster
        for cluster in clusters:
            self._clusters_emitted += 1

            cluster_arr = np.array(cluster, dtype=int)
            best_idx = int(cluster_arr[np.argmax(power_db[cluster_arr])])
            
            cluster_size = len(cluster)
            if cluster_size > 50:
                self.logger.debug(
                    "Wide signal detected: bins=%d (~%.1f kHz)",
                    cluster_size,
                    cluster_size * self._freq_step_hz / 1000,
                )
            self._emit_event(best_idx, cluster_size, power_db, noise_floor, source_frame_seq)

    # --- Event emission ---

    def _emit_event(
        self,
        bin_idx: int,
        cluster_size: int,
        power_db: NDArray[np.float64],
        noise_floor: NDArray[np.float64],
        source_frame_seq: int,
    ) -> None:
        """
        Convert detected FFT bin -> absolute frequency and create CfarEvent.
        """
        freq_offset_hz = (float(bin_idx) - (self._fft_size / 2.0)) * self._freq_step_hz
        abs_freq_hz = float(self.sdr_config.center_freq_hz + freq_offset_hz)

        bw_hz = float(cluster_size * self._freq_step_hz)

        p_db = float(power_db[bin_idx])
        nf_db = float(noise_floor[bin_idx])
        snr_db = float(p_db - nf_db)

        event = CfarEvent(
            event_id=uuid4(),
            timestamp=utc_now(),
            source_frame_seq=source_frame_seq,
            center_freq_hz=abs_freq_hz,
            bandwidth_hz=bw_hz,
            power_db=p_db,
            snr_db=snr_db,
            noise_floor_db=nf_db,
            duration_s=None,
            duty_cycle=None,
        )

        ok = self.queue.push_nowait(event)
        if ok:
            self._events_emitted += 1
            if self._events_emitted % self._LOG_EVENT_EVERY_N == 1:
                self.logger.info(
                    "CFAR event emitted. F=%.3f MHz SNR=%.1f dB Power=%.1f dB NF=%.1f dB Events=%d",
                    abs_freq_hz / 1e6,
                    snr_db,
                    p_db,
                    nf_db,
                    self._events_emitted,
                )
        else:
            # Queue full: drop is acceptable (soft real-time). TalosQueue tracks drops.
            if self._events_emitted % 500 == 0:
                self.logger.warning("Event dropped: output queue full")
            pass