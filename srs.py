"""A minimal SIPREC recording server (SRS) that publishes each recorded
call leg into a LiveKit room as its own audio track.

    SBC (SRC) --SIP INVITE + SDP + rs-metadata--> this server
    SBC (SRC) --RTP, one stream per call leg----> this server --> LiveKit room

Teaching code: UDP SIP only, G.711 only, no jitter buffer, no auth.
"""

import asyncio
import email
import os
import re
import secrets
import struct
import xml.etree.ElementTree as ET

from livekit import api, rtc

SIP_PORT = int(os.environ.get("SIP_PORT", "5060"))
PUBLIC_IP = os.environ.get("PUBLIC_IP", "127.0.0.1")
RTP_PORTS = iter(range(int(os.environ.get("RTP_PORT_START", "20000")), 30000, 2))
LIVEKIT_URL = os.environ["LIVEKIT_URL"]
NS = {"r": "urn:ietf:params:xml:ns:recording:1"}


# ---------- 1. G.711 decoding (the codecs SBCs almost always fork) ----------

def _ulaw(b: int) -> int:
    b = ~b & 0xFF
    t = (((b & 0x0F) << 3) + 0x84) << ((b & 0x70) >> 4)
    return (0x84 - t) if b & 0x80 else (t - 0x84)


def _alaw(b: int) -> int:
    b ^= 0x55
    t = (b & 0x0F) << 4
    seg = (b & 0x70) >> 4
    t = t + 8 if seg == 0 else ((t + 0x108) << (seg - 1))
    return t if b & 0x80 else -t


DECODE = {0: [_ulaw(i) for i in range(256)], 8: [_alaw(i) for i in range(256)]}


# ---------- 2. Reading the INVITE: SDP streams + who is speaking ----------

def parse_invite_body(content_type: str, body: bytes):
    """Return ([{label, port, ip, pts}], {label: speaker name})."""
    msg = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    parts = msg.get_payload() if msg.is_multipart() else [msg]
    sdp = next(p.get_payload(decode=True).decode() for p in parts if p.get_content_type() == "application/sdp")
    meta = next((p.get_payload(decode=True) for p in parts if p.get_content_type() == "application/rs-metadata"), None)

    streams, ip = [], None
    for line in sdp.splitlines():
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
        elif line.startswith("m=audio"):
            f = line.split()
            streams.append({"port": int(f[1]), "pts": [int(x) for x in f[3:]], "ip": ip, "label": None})
        elif line.startswith("a=label:") and streams:
            streams[-1]["label"] = line.split(":", 1)[1].strip()

    speakers = {}
    if meta:
        root = ET.fromstring(meta)
        names = {}
        for p in root.findall("r:participant", NS):
            n = p.find("r:nameID", NS)
            name = n.findtext("r:name", default=n.get("aor"), namespaces=NS)
            names[p.get("participant_id")] = name
        stream_label = {s.get("stream_id"): s.findtext("r:label", namespaces=NS) for s in root.findall("r:stream", NS)}
        for a in root.findall("r:participantstreamassoc", NS):
            for sent in a.findall("r:send", NS):
                speakers[stream_label.get(sent.text)] = names.get(a.get("participant_id"))
    return streams, speakers


# ---------- 3. RTP in, LiveKit audio out ----------

class LegToTrack(asyncio.DatagramProtocol):
    """Receives one RTP stream (one call leg) and feeds a LiveKit AudioSource."""

    def __init__(self, source: rtc.AudioSource):
        self.source = source
        self.packets = 0
        self.frames: asyncio.Queue = asyncio.Queue()
        self.pump = asyncio.ensure_future(self._pump())

    async def _pump(self):  # keep frames in order
        while True:
            await self.source.capture_frame(await self.frames.get())

    def datagram_received(self, data, addr):
        if len(data) < 12:
            return
        cc, pt = data[0] & 0x0F, data[1] & 0x7F
        offset = 12 + 4 * cc
        if data[0] & 0x10:  # header extension
            offset += 4 + 4 * struct.unpack("!H", data[offset + 2 : offset + 4])[0]
        table = DECODE.get(pt)
        if table is None:  # comfort noise, DTMF events, unknown codecs
            return
        payload = data[offset:]
        pcm = struct.pack(f"<{len(payload)}h", *(table[b] for b in payload))
        frame = rtc.AudioFrame(pcm, 8000, 1, len(payload))
        self.packets += 1
        self.frames.put_nowait(frame)


class RecordingSession:
    def __init__(self, call_id: str):
        self.call_id = call_id
        self.room = rtc.Room()
        self.transports = []
        self.legs = []

    async def start(self, streams, speakers):
        token = (
            api.AccessToken()  # reads LIVEKIT_API_KEY / LIVEKIT_API_SECRET
            .with_identity("siprec-srs")
            .with_grants(api.VideoGrants(room_join=True, room=f"siprec-{self.call_id}"))
            .to_jwt()
        )
        await self.room.connect(LIVEKIT_URL, token)
        loop = asyncio.get_running_loop()
        answers = []
        for s in streams:
            name = speakers.get(s["label"]) or f"leg-{s['label']}"
            source = rtc.AudioSource(8000, 1)
            track = rtc.LocalAudioTrack.create_audio_track(name, source)
            await self.room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            )
            port = next(RTP_PORTS)
            leg = LegToTrack(source)
            transport, _ = await loop.create_datagram_endpoint(lambda leg=leg: leg, local_addr=("0.0.0.0", port))
            self.transports.append(transport)
            self.legs.append((name, leg))
            pt = next((p for p in s["pts"] if p in DECODE), 0)
            answers.append((port, pt, s["label"]))
            print(f"[{self.call_id}] label {s['label']} -> track '{name}' on RTP port {port}")
        return answers

    async def stop(self):
        for t in self.transports:
            t.close()
        for name, leg in self.legs:
            leg.pump.cancel()
            print(f"[{self.call_id}] '{name}': {leg.packets} RTP packets forwarded")
        await self.room.disconnect()


def answer_sdp(answers) -> str:
    lines = ["v=0", f"o=SRS 1 1 IN IP4 {PUBLIC_IP}", "s=-", f"c=IN IP4 {PUBLIC_IP}", "t=0 0"]
    for port, pt, label in answers:
        codec = "PCMU" if pt == 0 else "PCMA"
        lines += [f"m=audio {port} RTP/AVP {pt}", f"a=rtpmap:{pt} {codec}/8000", "a=recvonly", f"a=label:{label}"]
    return "\r\n".join(lines) + "\r\n"


# ---------- 4. Just enough SIP to accept a recording session ----------

class SipServer(asyncio.DatagramProtocol):
    def __init__(self):
        self.sessions: dict[str, RecordingSession] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        if lines[0].startswith("SIP/2.0"):
            return  # a response; we never send requests in this demo
        method = lines[0].split()[0]
        h = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            h.setdefault(k.strip().lower(), []).append(v.strip())
        asyncio.ensure_future(self.handle(method, h, body, addr))

    def reply(self, addr, h, code, reason, extra=(), body=""):
        to = h["to"][0] if ";tag=" in h["to"][0] else h["to"][0] + ";tag=" + secrets.token_hex(4)
        out = [f"SIP/2.0 {code} {reason}"]
        out += [f"Via: {v}" for v in h["via"]]
        out += [f"From: {h['from'][0]}", f"To: {to}", f"Call-ID: {h['call-id'][0]}", f"CSeq: {h['cseq'][0]}"]
        out += list(extra) + [f"Content-Length: {len(body.encode())}", "", body]
        self.transport.sendto("\r\n".join(out).encode(), addr)

    async def handle(self, method, h, body, addr):
        call_id = re.sub(r"[^A-Za-z0-9]", "", h["call-id"][0])[:24]
        if method == "INVITE":
            if "siprec" not in ",".join(h.get("require", [])):
                return self.reply(addr, h, 420, "Bad Extension", ["Unsupported: siprec"])
            if call_id in self.sessions:  # re-INVITE (hold, transfer): keep the same tracks
                return self.reply(addr, h, 200, "OK", ["Content-Type: application/sdp"],
                                  answer_sdp(self.sessions[call_id].answers))
            self.reply(addr, h, 100, "Trying")
            streams, speakers = parse_invite_body(h["content-type"][0], body)
            session = RecordingSession(call_id)
            session.answers = await session.start(streams, speakers)
            self.sessions[call_id] = session
            self.reply(addr, h, 200, "OK",
                       [f"Contact: <sip:srs@{PUBLIC_IP}:{SIP_PORT}>", "Content-Type: application/sdp"],
                       answer_sdp(session.answers))
        elif method == "BYE":
            self.reply(addr, h, 200, "OK")
            if call_id in self.sessions:
                await self.sessions.pop(call_id).stop()
        elif method == "OPTIONS":  # SBCs ping the recorder to know it is alive
            self.reply(addr, h, 200, "OK")
        # ACK needs no answer


async def main():
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(SipServer, local_addr=("0.0.0.0", SIP_PORT))
    print(f"SIPREC SRS listening on udp/{SIP_PORT}, publishing to {LIVEKIT_URL}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
