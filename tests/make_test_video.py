"""Builds a synthetic ~6-minute 'documentary' (espeak-ng narration + animated
FFmpeg visuals, 1920x1080) for end-to-end testing when no real video is at hand.
    python tests/make_test_video.py out.mp4
"""
import subprocess, sys, json, wave, hashlib
from pathlib import Path

SECTIONS = [
 ("Intro", "gradients=s=1920x1080:r=25:c0=#1a1208:c1=#6b4a1e:speed=0.03",
  "Welcome back to the channel. In today's video, we travel across five centuries of strange and dramatic history. Before we begin, make sure to subscribe and turn on the notification bell."),
 ("Constantinople 1453", "life=s=480x270:mold=10:r=25:ratio=0.1:death_color=#1b0f05:life_color=#d9a441,scale=1920:1080:flags=neighbor",
  "In the spring of 1453, the greatest city in the Christian world was surrounded. Constantinople had stood for more than a thousand years. Its triple walls had turned back Persians, Arabs and Bulgars. But the young Ottoman sultan, Mehmed the Second, had brought something new. Giant bronze cannons, built by a Hungarian engineer named Orban, hurled stone balls weighing hundreds of kilograms against the ancient walls. For fifty three days the defenders repaired the damage every night. Mehmed even dragged his ships over land, on greased logs, into the Golden Horn, behind the great chain that protected the harbor. On the twenty ninth of May, the Ottoman army broke through. The last emperor, Constantine the Eleventh, died fighting in the streets. The Byzantine Empire, the final remnant of Rome, was gone forever. And to this day, historians use that morning to mark the end of the Middle Ages."),
 ("The Great Emu War", "cellauto=s=480x270:rule=110:r=25:random_fill_ratio=0.5,scale=1920:1080:flags=neighbor,negate",
  "Have you ever heard of a war that a country lost to birds? In 1932, farmers in Western Australia faced a strange invasion. Around twenty thousand emus had migrated onto their wheat fields, destroying the crops. The government sent soldiers, armed with two Lewis machine guns and ten thousand rounds of ammunition. It did not go to plan. The emus scattered into small groups, ran at fifty kilometers per hour, and seemed almost impossible to hit. In one ambush, the soldiers waited for a thousand birds to approach, but the gun jammed after killing only a handful. After weeks of effort and thousands of bullets, the military withdrew. The commander, Major Meredith, admitted the birds could face machine guns with the invulnerability of tanks. The emus had won. Australia never sent the army against them again."),
 ("Cleopatra and the Pyramids", "gradients=s=1920x1080:r=25:c0=#2a1a0a:c1=#c08a3e:c2=#3a2410:speed=0.02:nb_colors=3",
  "Here is a fact that breaks our sense of time. Cleopatra, the last active ruler of Ptolemaic Egypt, lived closer in time to the Moon landing than to the building of the Great Pyramid of Giza. The pyramid was finished around 2560 BC. Cleopatra died in 30 BC, more than two thousand five hundred years later. To Cleopatra, the pyramids were already ancient monuments, as old to her as the Roman Empire is to us. The Egyptian civilization lasted so long that its own people visited its ruins as tourists. That is why ancient Egypt remains one of the longest lasting civilizations in human history."),
 ("Defenestration of Prague", "life=s=320x180:r=25:ratio=0.2:death_color=#08131f:life_color=#7fb0d9,scale=1920:1080:flags=neighbor",
  "On the twenty third of May, 1618, a group of angry Protestant nobles marched into Prague Castle. They confronted two Catholic officials of the Habsburg emperor, accused them of violating religious freedom, and threw them out of a window. The officials fell about twenty one meters. Incredibly, both survived. Catholics claimed angels had caught them. Protestants said they had landed in a pile of manure. Either way, the insult could not be forgiven. The incident sparked a rebellion in Bohemia, and the rebellion grew into the Thirty Years War, one of the deadliest conflicts in European history. Millions died before the Peace of Westphalia finally ended the war in 1648, and changed the map of Europe forever."),
 ("Outro", "gradients=s=1920x1080:r=25:c0=#101820:c1=#405060:speed=0.03",
  "Thanks for watching this episode. If you enjoyed it, like the video, comment below with the story that surprised you most, and subscribe for the next video. See you next time."),
]

def run(c): subprocess.run(c, check=True, capture_output=True)

def main(out):
    out = Path(out).resolve(); work = out.parent / "_build"; work.mkdir(exist_ok=True)
    parts = []
    for k, (title, src, text) in enumerate(SECTIONS):
        wav = work / f"n{k}.wav"
        run(["espeak-ng", "-v", "en-us", "-s", "148", "-g", "6", "-w", str(wav), text])
        with wave.open(str(wav)) as w: dur = w.getnframes() / w.getframerate()
        seg = work / f"s{k}_{hashlib.md5((src+text).encode()).hexdigest()[:8]}.mp4"; total = dur + 2.2  # 1.2s lead-in silence + 1.0s tail
        vf = (f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.25:t=fill,"
              f"drawtext=text='{title}':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=h*0.80:box=1:boxcolor=black@0.5:boxborderw=20,"
              f"drawtext=text='%{{pts\\:hms}}':fontcolor=white@0.6:fontsize=28:x=40:y=40")
        if seg.exists(): parts.append(seg); continue
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", src, "-i", str(wav),
             "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.004:r=48000",
             "-filter_complex", f"[0:v]{vf},format=yuv420p,fps=25[v];[1:a]adelay=1200|1200,apad,aresample=48000[n];[n][2:a]amix=inputs=2:duration=first:normalize=0[a]",
             "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-ar", "48000", "-ac", "2", str(seg.with_suffix(".tmp.mp4"))])
        seg.with_suffix(".tmp.mp4").rename(seg)
        parts.append(seg)
    lst = work / "list.txt"; lst.write_text("".join(f"file '{p}'\n" for p in parts))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)])
    print("wrote", out)

if __name__ == "__main__": main(sys.argv[1] if len(sys.argv) > 1 else "test_documentary.mp4")
