"""Pretends to be an SBC (the SRC): sends a SIPREC INVITE, streams two legs of RTP, hangs up."""
import math, re, socket, struct, sys, time

SRS = ("127.0.0.1", 5060)
META = """<?xml version="1.0" encoding="UTF-8"?>
<recording xmlns="urn:ietf:params:xml:ns:recording:1">
  <datamode>complete</datamode>
  <session session_id="s1"><sipSessionID>call-42</sipSessionID></session>
  <participant participant_id="p1"><nameID aor="sip:+14165550100@example.com"><name>John Doe</name></nameID></participant>
  <participant participant_id="p2"><nameID aor="sip:kai@example.com"><name>Kai</name></nameID></participant>
  <stream stream_id="st1" session_id="s1"><label>1</label></stream>
  <stream stream_id="st2" session_id="s1"><label>2</label></stream>
  <participantstreamassoc participant_id="p1"><send>st1</send></participantstreamassoc>
  <participantstreamassoc participant_id="p2"><send>st2</send></participantstreamassoc>
</recording>"""
SDP = """v=0\r
o=SBC 1 1 IN IP4 127.0.0.1\r
s=-\r
c=IN IP4 127.0.0.1\r
t=0 0\r
m=audio 40000 RTP/AVP 0 8\r
a=sendonly\r
a=label:1\r
m=audio 40002 RTP/AVP 0 8\r
a=sendonly\r
a=label:2\r
"""
body = ("--b\r\nContent-Type: application/sdp\r\n\r\n" + SDP +
        "\r\n--b\r\nContent-Type: application/rs-metadata\r\nContent-Disposition: recording-session\r\n\r\n" + META + "\r\n--b--\r\n")

def req(method, cseq, extra="", body="", to_tag=""):
    return (f"{method} sip:srs@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:5070;branch=z9hG4bK{cseq}{method}\r\n"
            f"From: <sip:sbc@example.com>;tag=sbc1\r\nTo: <sip:srs@127.0.0.1>{to_tag}\r\nCall-ID: call-42@sbc\r\n"
            f"CSeq: {cseq} {method}\r\nMax-Forwards: 70\r\nContact: <sip:sbc@127.0.0.1:5070>;+sip.src\r\n{extra}"
            f"Content-Length: {len(body.encode())}\r\n\r\n{body}").encode()

def ulaw(x):  # 16-bit linear -> mu-law
    s = 0x80 if x < 0 else 0; x = min(abs(x), 32635) + 0x84
    e = min(7, x.bit_length() - 8)
    return ~(s | (e << 4) | ((x >> (e + 3)) & 0x0F)) & 0xFF

sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sip.bind(("127.0.0.1", 5070)); sip.settimeout(20)
sip.sendto(req("INVITE", 1, "Require: siprec\r\nAccept: application/sdp, application/rs-metadata\r\nContent-Type: multipart/mixed;boundary=b\r\n", body), SRS)
while True:
    resp = sip.recv(65535).decode(); print(resp.splitlines()[0])
    if resp.startswith("SIP/2.0 200"): break
to_tag = re.search(r"^To:.*?(;tag=\S+)", resp, re.M).group(1)
ports = [int(p) for p in re.findall(r"m=audio (\d+)", resp)]
print("SRS answered with RTP ports", ports)
sip.sendto(req("ACK", 1, to_tag=to_tag), SRS)

rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
freqs = [440, 880]  # John Doe leg, Kai leg
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6
start = time.time()
for n in range(int(secs * 50)):
    for leg, (port, f) in enumerate(zip(ports, freqs)):
        pcm = [int(9000 * math.sin(2 * math.pi * f * (n * 160 + i) / 8000)) for i in range(160)]
        hdr = struct.pack("!BBHII", 0x80, 0, n & 0xFFFF, n * 160, 1000 + leg)
        rtp.sendto(hdr + bytes(ulaw(x) for x in pcm), ("127.0.0.1", port))
    time.sleep(max(0, start + (n + 1) * 0.02 - time.time()))
sip.sendto(req("BYE", 2, to_tag=to_tag), SRS)
print(sip.recv(65535).decode().splitlines()[0])
