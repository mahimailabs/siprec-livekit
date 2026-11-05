"""Joins the LiveKit room and checks each published track carries the expected tone."""
import asyncio, os, sys
import numpy as np
from livekit import api, rtc

async def main(room_name, secs):
    tok = api.AccessToken().with_identity("listener").with_grants(api.VideoGrants(room_join=True, room=room_name)).to_jwt()
    room = rtc.Room(); results = {}
    async def read(track, name):
        buf = []
        async for ev in rtc.AudioStream(track, sample_rate=16000, num_channels=1):
            buf.append(np.frombuffer(ev.frame.data, dtype=np.int16))
            results[name] = buf
    @room.on("track_subscribed")
    def on_sub(track, pub, part):
        print("subscribed:", pub.name, "from", part.identity); asyncio.ensure_future(read(track, pub.name))
    await room.connect(os.environ["LIVEKIT_URL"], tok)
    await asyncio.sleep(secs)
    for name, buf in results.items():
        a = np.concatenate(buf).astype(float); a = a[len(a)//4:]
        spec = np.abs(np.fft.rfft(a)); hz = np.fft.rfftfreq(len(a), 1/16000)[spec.argmax()]
        print(f"track '{name}': {len(a)/16000:.1f}s audio, rms {np.sqrt((a**2).mean()):.0f}, peak at {hz:.0f} Hz")
    await room.disconnect()

asyncio.run(main(sys.argv[1], float(sys.argv[2])))
