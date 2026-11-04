# Offline test for narration levelling in cut_video.py: three synthetic "voice" clips at very different levels
# (about 30 dB apart, with sharp peaks like real speech) are mixed into a short video by join_parts, the same way
# a render does. In the video each clip must come out at the same loudness, and nothing may clip.
# Needs ffmpeg on PATH. Nothing is sent anywhere.
# Run: python tests/final_cut/test_narration_level.py
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from proof_and_demo import cut_video  # noqa: E402

TOTAL_S = 7.0
CLIP_S = 1.8
STARTS = [0.2, 2.5, 4.8]
AMPLITUDES = [0.025, 0.15, 0.6]  # about 30 dB apart in loudness, all within the +24 dB gain limit
TOLERANCE_DB = 1.5


def ffmpeg(args):
    subprocess.run(["ffmpeg", "-y", "-v", "error"] + args, check=True)


def make_clip(amplitude, path):
    # A 220 Hz tone pulsing like syllables, with a short loud click every 0.4 s: peaks well above its loudness.
    syllables = f"{amplitude}*sin(2*PI*220*t)*(0.5+0.5*sin(2*PI*4*t))"
    clicks = f"{amplitude}*2*sin(2*PI*1500*t)*lt(mod(t,0.4),0.01)"
    ffmpeg(["-f", "lavfi", "-i", f"aevalsrc=exprs='min(max({syllables}+{clicks},-1),1)':s=44100:d={CLIP_S}", path])


def window(path, start, end):
    return cut_video.loudness_and_peak(path, f"atrim={start}:{end}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        # One silent video part, as render_cut would make: black picture, silent stereo audio at the cut's rate.
        part = os.path.join(tmp, "part.mkv")
        ffmpeg(["-f", "lavfi", "-i", f"color=black:s=320x180:r=30:d={TOTAL_S}",
                "-f", "lavfi", "-i", f"anullsrc=r={cut_video.SAMPLE_RATE}:cl=stereo", "-t", str(TOTAL_S),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", part])
        narration = []
        for i, (start, amplitude) in enumerate(zip(STARTS, AMPLITUDES)):
            path = os.path.join(tmp, f"clip{i}.wav")
            make_clip(amplitude, path)
            lufs, peak, gain = cut_video.narration_gain(path)
            print(f"clip {i}: {lufs:.1f} LUFS, peak {peak:.1f} dBTP, gain {gain:+.1f} dB")
            narration.append({"file": path, "out_start_s": start, "duration_s": CLIP_S, "gain_db": gain})

        before = [cut_video.loudness_and_peak(c["file"])[0] for c in narration]
        assert max(before) - min(before) > 25, before  # the clips really do start far apart

        out = os.path.join(tmp, "out.mp4")
        cut_video.join_parts([part], narration, None, TOTAL_S, tmp, out)
        after = [window(out, c["out_start_s"], c["out_start_s"] + CLIP_S) for c in narration]
        for i, (lufs, peak) in enumerate(after):
            print(f"in the video, clip {i}: {lufs:.1f} LUFS, peak {peak:.1f} dBTP")
        levels = [lufs for lufs, _ in after]
        assert max(levels) - min(levels) <= TOLERANCE_DB, levels
        assert all(abs(level - cut_video.NARRATION_LUFS) <= TOLERANCE_DB for level in levels), levels
        assert all(peak < 0 for _, peak in after), after  # the limiter keeps every peak under full scale
        print(f"ok  levelling: {max(before) - min(before):.1f} dB apart before, {max(levels) - min(levels):.1f} dB "
              f"apart in the video, all within {TOLERANCE_DB} dB of {cut_video.NARRATION_LUFS} LUFS, no clipping")

        # Silence can't be measured (-70 LUFS): it is left alone, not raised by the maximum gain.
        silent = os.path.join(tmp, "silent.wav")
        ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1", silent])
        assert cut_video.narration_gain(silent)[2] == 0.0
        print("ok  a silent clip gets no gain")
    print("All tests passed.")


if __name__ == "__main__":
    main()
