"""A compact "what does it sound like" vector per track, computed locally.

No service, model or download: ffmpeg decodes a bounded excerpt to mono
22.05 kHz float PCM on a pipe, and numpy turns it into FEATURE_COUNT numbers
in the spirit of the bliss analyser - tempo and pulse, loudness and dynamics,
brightness and noisiness, a key-relative chroma profile with key and mode,
and MFCC timbre statistics. The raw values are stored (app.audio_analysis);
app.audio_similarity scales them with library-wide statistics at query
time, so adding tracks never means recomputing stored vectors.

The excerpt: three 30-second windows centred at 20 %, 50 % and 80 % of the
track, decoded by ONE ffmpeg process (three seeking inputs merged into a
three-channel stream, one channel per window). A track too short for that
(or of unknown length) is decoded from its start, at most 90 seconds. So a
track costs at most 90 seconds of decoding and analysis however long it is,
and memory stays bounded (a few MB of PCM) even for an hour-long file.

Vector layout, version ANALYSIS_VERSION (little-endian float32, FEATURE_COUNT
values, 180 bytes; the names are FEATURE_NAMES):

    0  tempo_bpm           estimated beats per minute (40-240)
    1  tempo_strength      height of the tempo's autocorrelation peak, 0-1
    2  onset_strength      mean spectral flux of the log-mel spectrum (dB)
    3  loudness_db         RMS level of the non-silent frames (dBFS)
    4  loudness_std_db     spread of the frame levels (dB)
    5  dynamic_range_db    95th minus 10th percentile of frame levels (dB)
    6  centroid_hz         mean spectral centroid ("brightness")
    7  centroid_std_hz     its spread
    8  rolloff_hz          mean 85 % spectral roll-off
    9  flatness_db         mean spectral flatness (dB; 0 = white noise)
    10 flatness_std_db     its spread
    11 zcr                 mean zero-crossing rate (crossings per sample)
    12 zcr_std             its spread
    13 key                 tonic pitch class, 0 = C ... 11 = B
    14 mode                1 major, 0 minor
    15 key_strength        correlation with the best key profile, -1..1
    16 mode_score          best major minus best minor correlation
    17-28 chroma_rel_0..11 mean chroma, rotated so the tonic is bin 0, sums to 1
    29-40 mfcc_1..12       mean MFCCs 1-12 (40 mel bands, 0-11025 Hz)
    41-44 mfcc_std_1..4    spread of MFCCs 1-4
"""

from __future__ import annotations

import os

# numpy's bundled OpenBLAS starts a thread per core for every matrix product
# by default; here the products are small and run in background work on a
# NAS, where one thread is the right amount. Only effective before numpy is
# first imported, which is in this module (the app has no other numpy user);
# an operator's own setting wins.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import logging  # noqa: E402 - after the BLAS thread setting, on purpose
import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from functools import cache  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from app.transcode import transcode_timeout  # noqa: E402
from app.video_meta import INPUT_PROTOCOLS  # noqa: E402

logger = logging.getLogger(__name__)

# Bump when the features or their computation change: rows of an older
# version are analysed again by the background pass.
ANALYSIS_VERSION = 1

SAMPLE_RATE = 22050
WINDOW_SECONDS = 30.0
WINDOW_CENTRES = (0.2, 0.5, 0.8)
# Below this length (or with no known length) the track is decoded from the
# start instead of in three windows, up to MAX_DECODE_SECONDS.
MIN_WINDOWED_SECONDS = len(WINDOW_CENTRES) * WINDOW_SECONDS + 20
MAX_DECODE_SECONDS = len(WINDOW_CENTRES) * WINDOW_SECONDS
# Less non-silent audio than this is "too short" to describe.
MIN_AUDIBLE_SECONDS = 8.0
SILENCE_DB = -60.0

N_FFT = 2048
HOP = 512
FRAME_RATE = SAMPLE_RATE / HOP
N_MELS = 40
N_MFCC = 12
N_MFCC_STD = 4
TEMPO_MIN_BPM = 40.0
TEMPO_MAX_BPM = 240.0
# Bytes read from ffmpeg's stdout per call.
READ_CHUNK_BYTES = 256 * 1024

FEATURE_NAMES: tuple[str, ...] = (
    "tempo_bpm",
    "tempo_strength",
    "onset_strength",
    "loudness_db",
    "loudness_std_db",
    "dynamic_range_db",
    "centroid_hz",
    "centroid_std_hz",
    "rolloff_hz",
    "flatness_db",
    "flatness_std_db",
    "zcr",
    "zcr_std",
    "key",
    "mode",
    "key_strength",
    "mode_score",
    *(f"chroma_rel_{i}" for i in range(12)),
    *(f"mfcc_{i}" for i in range(1, N_MFCC + 1)),
    *(f"mfcc_std_{i}" for i in range(1, N_MFCC_STD + 1)),
)
FEATURE_COUNT = len(FEATURE_NAMES)
FEATURE_INDEX = {name: i for i, name in enumerate(FEATURE_NAMES)}
VECTOR_DTYPE = np.dtype("<f4")

_NICE = ("nice", "-n", "10") if shutil.which("nice") else ()
# Each window (or the whole-start excerpt) becomes mono float 22.05 kHz; the
# explicit aformat after aresample lets amerge negotiate one format.
_TO_MONO = (
    f"aresample={SAMPLE_RATE},"
    f"aformat=sample_fmts=flt:sample_rates={SAMPLE_RATE}:channel_layouts=mono"
)


@dataclass(frozen=True)
class FeatureVector:
    """One track's raw features (layout in the module docstring)."""

    values: np.ndarray  # float32, shape (FEATURE_COUNT,)
    analysed_seconds: float

    def __getitem__(self, name: str) -> float:
        return float(self.values[FEATURE_INDEX[name]])

    @property
    def tempo_bpm(self) -> float:
        return self["tempo_bpm"]

    @property
    def loudness_db(self) -> float:
        return self["loudness_db"]

    @property
    def brightness_hz(self) -> float:
        return self["centroid_hz"]

    @property
    def key(self) -> int:
        return int(self["key"])

    @property
    def mode(self) -> int:
        return int(self["mode"])

    def to_blob(self) -> bytes:
        return self.values.astype(VECTOR_DTYPE).tobytes()

    @staticmethod
    def values_from_blob(blob: bytes) -> np.ndarray | None:
        """The stored vector, or None when it is not this version's length."""
        if len(blob) != FEATURE_COUNT * VECTOR_DTYPE.itemsize:
            return None
        return np.frombuffer(blob, dtype=VECTOR_DTYPE)


class AnalysisError(Exception):
    """Why a file could not be analysed (stored as the row's reason)."""


# --- decoding ---------------------------------------------------------------


def window_starts(duration_seconds: float | None) -> list[float] | None:
    """Start times of the three windows, or None to decode from the start."""
    if duration_seconds is None or duration_seconds < MIN_WINDOWED_SECONDS:
        return None
    half = WINDOW_SECONDS / 2
    return [
        round(min(max(0.0, c * duration_seconds - half), duration_seconds - WINDOW_SECONDS), 3)
        for c in WINDOW_CENTRES
    ]


def ffmpeg_argv(path: Path, duration_seconds: float | None) -> tuple[list[str], int]:
    """The ffmpeg command and how many channels (windows) its raw float32
    output interleaves. Checked on ffmpeg 7.1 (the trixie image) and 9.0."""
    starts = window_starts(duration_seconds)
    argv = [*_NICE, "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error"]
    if starts is None:
        argv += [*INPUT_PROTOCOLS, "-t", f"{MAX_DECODE_SECONDS:g}", "-i", str(path)]
        graph = f"[0:a:0]{_TO_MONO}[out]"
        channels = 1
    else:
        for start in starts:
            # -ss/-t before -i: seek in the input, decode only the window.
            argv += [
                *INPUT_PROTOCOLS,
                "-ss", f"{start:g}",
                "-t", f"{WINDOW_SECONDS:g}",
                "-i", str(path),
            ]
        parts = [f"[{i}:a:0]{_TO_MONO}[w{i}]" for i in range(len(starts))]
        labels = "".join(f"[w{i}]" for i in range(len(starts)))
        graph = ";".join([*parts, f"{labels}amerge=inputs={len(starts)}[out]"])
        channels = len(starts)
    argv += [
        "-filter_complex", graph,
        "-map", "[out]",
        "-c:a", "pcm_f32le",
        "-f", "f32le",
        "pipe:1",
    ]
    return argv, channels


def decode_excerpt(
    path: Path,
    duration_seconds: float | None,
    stop: threading.Event | None = None,
) -> list[np.ndarray]:
    """The excerpt's windows as mono float32 arrays at SAMPLE_RATE. Reads
    the pipe in chunks into a buffer sized for the excerpt, so memory never
    grows with the file. Raises AnalysisError."""
    argv, channels = ffmpeg_argv(path, duration_seconds)
    seconds = MAX_DECODE_SECONDS if channels == 1 else WINDOW_SECONDS
    capacity = int(seconds * SAMPLE_RATE * channels * VECTOR_DTYPE.itemsize)
    # A little slack: -t cuts at a frame boundary.
    buffer = bytearray(capacity + 4 * SAMPLE_RATE * channels)
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv list, no shell; paths come from the library, never a command
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AnalysisError(f"ffmpeg not runnable: {exc.__class__.__name__}") from exc
    assert proc.stdout is not None  # stdout=PIPE above
    # Seeking three times into a slow disk, then decoding at most 90 s: the
    # transcode timeout for that much audio is generous on any machine.
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(transcode_timeout(seconds), expire)
    timer.daemon = True
    timer.start()
    filled = 0
    stopped = full = eof = False
    try:
        view = memoryview(buffer)
        while not full:
            if stop is not None and stop.is_set():
                stopped = True
                break
            got = proc.stdout.readinto(view[filled : filled + READ_CHUNK_BYTES])  # type: ignore[attr-defined]
            if not got:
                eof = True
                break
            filled += got
            full = filled >= len(buffer)
    finally:
        timer.cancel()
        if not eof:
            # Stopped, more than the excerpt can hold, or an error here:
            # ffmpeg must not run on with nobody reading.
            proc.kill()
        proc.stdout.close()
        returncode = proc.wait()
    if stopped:
        raise AnalysisError("stopped")
    if timed_out.is_set():
        raise AnalysisError("ffmpeg timed out")
    if returncode != 0 and not full:
        raise AnalysisError(f"ffmpeg exited {returncode}")
    frame = channels * VECTOR_DTYPE.itemsize
    usable = min(filled, capacity) // frame * frame
    samples = np.frombuffer(bytes(buffer[:usable]), dtype=VECTOR_DTYPE)
    if channels == 1:
        return [samples]
    interleaved = samples.reshape(-1, channels)
    return [np.ascontiguousarray(interleaved[:, i]) for i in range(channels)]


# --- analysis ----------------------------------------------------------------


def _hz_to_mel(hz: np.ndarray) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


@cache
def _tables() -> dict[str, np.ndarray]:
    """Fixed matrices, built once: the analysis window, bin frequencies, the
    mel filterbank, the DCT for MFCCs and the chroma bin map."""
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    edges = _mel_to_hz(np.linspace(_hz_to_mel(np.array(0.0)), _hz_to_mel(np.array(SAMPLE_RATE / 2)), N_MELS + 2))
    mel = np.zeros((len(freqs), N_MELS))
    for m in range(N_MELS):
        lo, centre, hi = edges[m], edges[m + 1], edges[m + 2]
        rising = (freqs - lo) / (centre - lo)
        falling = (hi - freqs) / (hi - centre)
        mel[:, m] = np.maximum(0.0, np.minimum(rising, falling)) * (2.0 / (hi - lo))
    n = np.arange(N_MELS)
    dct = np.cos(np.pi / N_MELS * (n[:, None] + 0.5) * np.arange(1, N_MFCC + 1)[None, :])
    dct *= np.sqrt(2.0 / N_MELS)
    # Chroma from 65 Hz (C2) to 5 kHz: below it a 10.8 Hz bin cannot tell
    # semitones apart, above it is mostly overtones and noise.
    chroma = np.zeros((len(freqs), 12))
    band = (freqs >= 65.0) & (freqs <= 5000.0)
    midi = 69 + 12 * np.log2(freqs[band] / 440.0)
    chroma[np.flatnonzero(band), np.round(midi).astype(int) % 12] = 1.0
    return {
        "window": np.hanning(N_FFT).astype(np.float32),
        "freqs": freqs.astype(np.float32),
        "mel": mel.astype(np.float32),
        "dct": dct.astype(np.float32),
        "chroma": chroma.astype(np.float32),
    }


# Krumhansl-Kessler key profiles (major, minor), tonic first.
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _frames(samples: np.ndarray) -> np.ndarray:
    count = 1 + (len(samples) - N_FFT) // HOP
    return np.lib.stride_tricks.as_strided(
        samples,
        shape=(count, N_FFT),
        strides=(samples.strides[0] * HOP, samples.strides[0]),
        writeable=False,
    )


@dataclass
class _WindowStats:
    level_db: np.ndarray  # per frame
    zcr: np.ndarray
    centroid: np.ndarray
    rolloff: np.ndarray
    flatness_db: np.ndarray
    mfcc: np.ndarray  # frames x N_MFCC
    chroma: np.ndarray  # 12, summed
    onset: np.ndarray  # onset strength envelope (all frames)
    audible: np.ndarray  # bool per frame


def _analyse_window(samples: np.ndarray) -> _WindowStats | None:
    if len(samples) < N_FFT * 4:
        return None
    t = _tables()
    frames = _frames(samples)
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float32), axis=1)) + 1e-9
    level_db = 20.0 * np.log10(rms)
    signs = np.signbit(frames)
    zcr = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)
    spectrum = np.fft.rfft(frames * t["window"], axis=1)
    power = (spectrum.real**2 + spectrum.imag**2).astype(np.float32) + 1e-12
    magnitude = np.sqrt(power)
    mag_sum = magnitude.sum(axis=1)
    centroid = magnitude @ t["freqs"] / mag_sum
    cumulative = np.cumsum(magnitude, axis=1)
    rolloff_bin = np.argmax(cumulative >= 0.85 * mag_sum[:, None], axis=1)
    rolloff = t["freqs"][rolloff_bin]
    flatness_db = 10.0 * (np.mean(np.log10(power), axis=1) - np.log10(np.mean(power, axis=1)))
    mel_db = 10.0 * np.log10(power @ t["mel"] + 1e-10)
    mfcc = mel_db @ t["dct"]
    # Onset strength: half-wave rectified rise of the log-mel spectrum,
    # floored 80 dB below the window's peak so silence adds no noise.
    floored = np.maximum(mel_db, mel_db.max() - 80.0)
    onset = np.concatenate([[0.0], np.maximum(0.0, np.diff(floored, axis=0)).mean(axis=1)])
    audible = level_db > SILENCE_DB
    chroma_frames = power[audible] @ t["chroma"]
    chroma_frames /= chroma_frames.sum(axis=1, keepdims=True) + 1e-12
    return _WindowStats(
        level_db=level_db,
        zcr=zcr,
        centroid=centroid,
        rolloff=rolloff,
        flatness_db=flatness_db,
        mfcc=mfcc,
        chroma=chroma_frames.sum(axis=0),
        onset=onset.astype(np.float32),
        audible=audible,
    )


def _autocorrelation(envelope: np.ndarray, max_lag: int) -> np.ndarray | None:
    """Normalised autocorrelation of a mean-removed envelope (lag 0 = 1)."""
    if len(envelope) < 2 * max_lag:
        return None
    x = envelope - envelope.mean()
    size = 1 << int(np.ceil(np.log2(2 * len(x))))
    spectrum = np.fft.rfft(x, size)
    acf = np.fft.irfft(spectrum.real**2 + spectrum.imag**2, size)[: max_lag + 1]
    if acf[0] <= 0:
        return None
    return acf / acf[0]


def _peak_near(acf: np.ndarray, lag: float, tolerance: float = 0.08) -> tuple[float, float] | None:
    """The local maximum of `acf` within ±tolerance of `lag`, refined by a
    parabola through its neighbours: (lag, height)."""
    lo, hi = int(np.floor(lag * (1 - tolerance))), int(np.ceil(lag * (1 + tolerance)))
    lo, hi = max(lo, 1), min(hi, len(acf) - 2)
    if hi <= lo:
        return None
    i = lo + int(np.argmax(acf[lo : hi + 1]))
    a, b, c = acf[i - 1], acf[i], acf[i + 1]
    denom = a - 2 * b + c
    offset = 0.5 * (a - c) / denom if denom < 0 else 0.0
    return i + float(np.clip(offset, -0.5, 0.5)), float(b)


def _tempo(onsets: list[np.ndarray]) -> tuple[float, float]:
    """(BPM, strength) from the windows' onset envelopes: the autocorrelation
    peak in 40-240 BPM (backed by its half and double lags) weighted by a
    broad prior around 120 BPM, its lag refined with the peaks at two and
    four times it."""
    max_lag = int(np.ceil(FRAME_RATE * 60.0 / TEMPO_MIN_BPM)) * 4 + 2
    # Onsets are a frame or two wide, and a beat period is rarely a whole
    # number of frames: unsmoothed, a peak whose lag falls between two
    # frames loses half its height to its neighbours and a less fitting
    # multiple (3/2 of the beat, say) can win.
    kernel = np.hanning(7)[1:-1]
    kernel /= kernel.sum()
    acfs = [
        acf
        for env in onsets
        if (acf := _autocorrelation(np.convolve(env, kernel, mode="same"), max_lag)) is not None
    ]
    if not acfs:
        return 0.0, 0.0
    acf = np.mean(acfs, axis=0)
    lags = np.arange(len(acf), dtype=float)
    bpm = np.where(lags > 0, 60.0 * FRAME_RATE / np.maximum(lags, 1e-9), 0.0)
    valid = (bpm >= TEMPO_MIN_BPM) & (bpm <= TEMPO_MAX_BPM)
    prior = np.exp(-0.5 * (np.log2(np.maximum(bpm, 1e-9) / 120.0) / 1.0) ** 2)
    # A beat lag is backed by its half (the off-beat) and its double (the
    # next beat); a lag of 1.5 beats - which off-beat hi-hats make as
    # periodic as the beat itself - has no off-beat of its own.
    positive = np.maximum(acf, 0.0)
    half = np.interp(lags / 2, lags, positive)
    double = np.interp(np.minimum(lags * 2, lags[-1]), lags, positive)
    scored = np.where(valid, (positive + 0.5 * half + 0.5 * double) * prior, -1.0)
    best = int(np.argmax(scored))
    if scored[best] <= 0:
        return 0.0, 0.0
    first = _peak_near(acf, float(best), 0.05)
    if first is None:
        return 0.0, 0.0
    estimates = [first[0]]
    for multiple in (2, 4):
        peak = _peak_near(acf, first[0] * multiple, 0.04)
        if peak is not None and peak[1] > 0.5 * first[1]:
            estimates.append(peak[0] / multiple)
    # The longer lags carry finer resolution; weight them by their multiple.
    weights = [1, 2, 4][: len(estimates)]
    lag = float(np.average(estimates, weights=weights))
    return 60.0 * FRAME_RATE / lag, float(np.clip(first[1], 0.0, 1.0))


def compute_features(windows: list[np.ndarray]) -> FeatureVector | None:
    """The feature vector of decoded mono windows, or None when there is
    too little audible sound. Pure numpy: deterministic for the same input."""
    chunk = int(WINDOW_SECONDS * SAMPLE_RATE)
    pieces = [
        np.asarray(w[i : i + chunk], dtype=np.float32)
        for w in windows
        for i in range(0, len(w), chunk)
    ]
    stats = [s for s in (_analyse_window(p) for p in pieces) if s is not None]
    if not stats:
        return None
    audible = np.concatenate([s.audible for s in stats])
    if audible.sum() < MIN_AUDIBLE_SECONDS * FRAME_RATE:
        return None

    def pick(name: str) -> np.ndarray:
        return np.concatenate([getattr(s, name) for s in stats])[audible]

    level_db = pick("level_db")
    centroid, rolloff = pick("centroid"), pick("rolloff")
    flatness_db, zcr = pick("flatness_db"), pick("zcr")
    mfcc = np.concatenate([s.mfcc for s in stats])[audible]
    tempo_bpm, tempo_strength = _tempo([s.onset for s in stats])
    onset_strength = float(np.concatenate([s.onset for s in stats])[audible].mean())

    chroma = np.sum([s.chroma for s in stats], axis=0)
    chroma = chroma / chroma.sum() if chroma.sum() > 0 else np.full(12, 1 / 12)
    scores = np.array(
        [
            [np.corrcoef(chroma, np.roll(profile, k))[0, 1] for k in range(12)]
            for profile in (_MAJOR, _MINOR)
        ]
    )
    scores = np.nan_to_num(scores)
    mode_index, key = np.unravel_index(int(np.argmax(scores)), scores.shape)
    key_strength = float(scores[mode_index, key])
    mode_score = float(scores[0].max() - scores[1].max())
    chroma_rel = np.roll(chroma, -int(key))

    energy = np.mean(10.0 ** (level_db / 10.0))
    values = np.array(
        [
            tempo_bpm,
            tempo_strength,
            onset_strength,
            10.0 * np.log10(energy),
            level_db.std(),
            np.percentile(level_db, 95) - np.percentile(level_db, 10),
            centroid.mean(),
            centroid.std(),
            rolloff.mean(),
            flatness_db.mean(),
            flatness_db.std(),
            zcr.mean(),
            zcr.std(),
            float(key),
            1.0 if mode_index == 0 else 0.0,
            key_strength,
            mode_score,
            *chroma_rel,
            *mfcc.mean(axis=0),
            *mfcc[:, :N_MFCC_STD].std(axis=0),
        ],
        dtype=np.float64,
    )
    if not np.all(np.isfinite(values)):
        return None
    analysed = sum(len(w) for w in windows) / SAMPLE_RATE
    return FeatureVector(values=values.astype(VECTOR_DTYPE), analysed_seconds=round(analysed, 2))


def analyse_file(
    path: Path,
    duration_seconds: float | None = None,
    stop: threading.Event | None = None,
) -> FeatureVector:
    """Like extract_features, but raises AnalysisError with the reason."""
    windows = decode_excerpt(path, duration_seconds, stop)
    if len(windows) > 1 and min(len(w) for w in windows) < MIN_AUDIBLE_SECONDS * SAMPLE_RATE:
        # The tagged length was longer than the audio: the late windows
        # seeked past the end. Read from the start instead.
        windows = decode_excerpt(path, None, stop)
    if sum(len(w) for w in windows) == 0:
        raise AnalysisError("no audio decoded")
    try:
        features = compute_features(windows)
    except (ValueError, FloatingPointError, MemoryError) as exc:
        raise AnalysisError(f"analysis failed: {exc.__class__.__name__}") from exc
    if features is None:
        raise AnalysisError("too short or silent")
    return features


def extract_features(
    path: Path,
    duration_seconds: float | None = None,
    stop: threading.Event | None = None,
) -> FeatureVector | None:
    """The track's feature vector, or None when it cannot be decoded, is too
    short or silent (never raises)."""
    try:
        return analyse_file(path, duration_seconds, stop)
    except AnalysisError as exc:
        logger.debug("Audio analysis of %s failed: %s", path, exc)
        return None
    except Exception:
        logger.exception("Audio analysis of %s failed", path)
        return None
