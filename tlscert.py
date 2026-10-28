"""遠隔の HTTPS に使う証明書を、標準ライブラリだけで作る (exe に cryptography などを入れていないので)。

スマホには「WinTabDock の認証局」(CA) を一度だけ入れてもらい、待ち受けるアドレスの証明書はそこから作る
(アドレスが変わっても入れ直さずに済む)。認証局は、名前の制約で家の中と Tailscale のアドレス
(10/8・172.16/12・192.168/16・100.64/10・127/8) と `wintabdock.invalid` にしか効かないようにする。
鍵が漏れても、ふつうのサイト (銀行など) の偽物は作れない。鍵は ECDSA P-256、署名は SHA-256。
"""

import base64
import datetime
import hashlib
import ipaddress
import secrets

# P-256 (secp256r1) の定数
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)

# 名前の制約で許すアドレス (家の中・Tailscale・この PC)
PERMITTED_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8")
PERMITTED_DNS = "wintabdock.invalid"  # DNS の名前は使わない (制約に 1 つも書かないと、どの名前でも通ってしまう)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0]:
        if (p[1] + q[1]) % _P == 0:
            return None
        lam = (3 * p[0] * p[0] + _A) * pow(2 * p[1], -1, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _mul(k: int, p=_G):
    out = None
    while k:
        if k & 1:
            out = _add(out, p)
        p = _add(p, p)
        k >>= 1
    return out


def _sign(d: int, data: bytes) -> bytes:
    z = int.from_bytes(hashlib.sha256(data).digest(), "big")
    while True:
        k = secrets.randbelow(_N - 1) + 1
        r = _mul(k)[0] % _N
        s = pow(k, -1, _N) * (z + r * d) % _N
        if r and s:
            return _seq(_int(r), _int(s))


# DER の部品
def _tlv(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        size = bytes([n])
    else:
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        size = bytes([0x80 | len(b)]) + b
    return bytes([tag]) + size + body


def _seq(*parts: bytes) -> bytes:
    return _tlv(0x30, b"".join(parts))


def _int(v: int) -> bytes:
    return _tlv(0x02, v.to_bytes(v.bit_length() // 8 + 1, "big"))


def _oid(dotted: str) -> bytes:
    nums = [int(x) for x in dotted.split(".")]
    body = bytes([nums[0] * 40 + nums[1]])
    for v in nums[2:]:
        chunk = [v & 0x7F]
        v >>= 7
        while v:
            chunk.append(0x80 | (v & 0x7F))
            v >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, body)


def _bits(b: bytes) -> bytes:
    return _tlv(0x03, b"\x00" + b)


def _octets(b: bytes) -> bytes:
    return _tlv(0x04, b)


def _name(cn: str) -> bytes:
    return _seq(_tlv(0x31, _seq(_oid("2.5.4.3"), _tlv(0x0C, cn.encode("utf-8")))))


def _time(t: datetime.datetime) -> bytes:
    return _tlv(0x17, t.strftime("%y%m%d%H%M%SZ").encode())


def _ext(oid: str, critical: bool, value: bytes) -> bytes:
    return _seq(_oid(oid), *([_tlv(0x01, b"\xff")] if critical else []), _octets(value))


_ECDSA_SHA256 = _seq(_oid("1.2.840.10045.4.3.2"))


def _point(d: int) -> bytes:
    x, y = _mul(d)
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def _spki(pub: bytes) -> bytes:
    return _seq(_seq(_oid("1.2.840.10045.2.1"), _oid("1.2.840.10045.3.1.7")), _bits(pub))


def _pem(kind: str, der: bytes) -> str:
    b = base64.b64encode(der).decode()
    return f"-----BEGIN {kind}-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + f"\n-----END {kind}-----\n"


def _unpem(text: str) -> bytes:
    return base64.b64decode("".join(ln for ln in text.splitlines() if ln and not ln.startswith("-----")))


def new_key() -> int:
    return secrets.randbelow(_N - 1) + 1


def key_pem(d: int) -> str:
    """SEC1 の EC 秘密鍵 (Python の ssl が読める形)。"""
    der = _seq(_int(1), _octets(d.to_bytes(32, "big")), _tlv(0xA0, _oid("1.2.840.10045.3.1.7")),
               _tlv(0xA1, _bits(_point(d))))
    return _pem("EC PRIVATE KEY", der)


def key_from_pem(text: str) -> int:
    der = _unpem(text)
    # SEQUENCE { INTEGER 1, OCTET STRING (32 バイト) ... } の 32 バイトを取り出す
    i = der.index(b"\x04\x20", 2)
    return int.from_bytes(der[i + 2:i + 34], "big")


def _cert(subject: str, issuer: str, pub: bytes, sign_key: int, days: int, exts: list[bytes]) -> bytes:
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    tbs = _seq(
        _tlv(0xA0, _int(2)),  # v3
        _int(int.from_bytes(secrets.token_bytes(15), "big") | 1 << 112),  # 正の数 (先頭を 0 にしない)
        _ECDSA_SHA256,
        _name(issuer),
        _seq(_time(now - datetime.timedelta(days=1)), _time(now + datetime.timedelta(days=days))),
        _name(subject),
        _spki(pub),
        _tlv(0xA3, _seq(*exts)),
    )
    return _seq(tbs, _ECDSA_SHA256, _bits(_sign(sign_key, tbs)))


def _key_id(pub: bytes) -> bytes:
    return hashlib.sha1(pub).digest()


def make_ca(name: str) -> tuple[str, str]:
    """認証局の (証明書の PEM, 鍵の PEM)。10 年使う。"""
    d = new_key()
    pub = _point(d)
    permitted = [_tlv(0x87, ipaddress.ip_network(n).network_address.packed + ipaddress.ip_network(n).netmask.packed)
                 for n in PERMITTED_NETS]
    permitted.append(_tlv(0x82, PERMITTED_DNS.encode()))
    exts = [
        _ext("2.5.29.19", True, _seq(_tlv(0x01, b"\xff"), _int(0))),  # CA で、下に CA を作らせない
        _ext("2.5.29.15", True, _bits(b"\x06")),  # keyCertSign・cRLSign
        _ext("2.5.29.14", False, _octets(_key_id(pub))),
        _ext("2.5.29.30", True, _seq(_tlv(0xA0, b"".join(_seq(p) for p in permitted)))),  # 許す名前
    ]
    return _pem("CERTIFICATE", _cert(name, name, pub, d, 3650, exts)), key_pem(d)


def make_server(ca_pem: str, ca_key_pem: str, ips: list[str]) -> tuple[str, str]:
    """待ち受けるアドレスの (証明書の PEM, 鍵の PEM)。iOS の決まりに合わせて 825 日より短くする。"""
    ca_der = _unpem(ca_pem)
    ca_d = key_from_pem(ca_key_pem)
    ca_name = cert_subject_cn(ca_der)
    d = new_key()
    pub = _point(d)
    san = b"".join(_tlv(0x87, ipaddress.ip_address(ip).packed) for ip in ips)
    exts = [
        _ext("2.5.29.19", True, _seq()),  # CA ではない
        _ext("2.5.29.15", True, _bits(b"\x80")),  # digitalSignature
        _ext("2.5.29.37", False, _seq(_oid("1.3.6.1.5.5.7.3.1"))),  # serverAuth
        _ext("2.5.29.17", False, _seq(san)),
        _ext("2.5.29.14", False, _octets(_key_id(pub))),
        _ext("2.5.29.35", False, _seq(_tlv(0x80, _key_id(_point(ca_d))))),
    ]
    return _pem("CERTIFICATE", _cert(f"WinTabDock {ips[0]}", ca_name, pub, ca_d, 397, exts)), key_pem(d)


def cert_subject_cn(der: bytes) -> str:
    """証明書の subject の CN (自分で作った証明書だけを読む。issuer の次の subject を探す)。"""
    oid = _oid("2.5.4.3")
    names = []
    i = 0
    while (i := der.find(oid, i)) >= 0:
        j = i + len(oid)
        size = der[j + 1]
        names.append(der[j + 2:j + 2 + size].decode("utf-8"))
        i = j
    return names[1]


def fingerprint(pem: str) -> str:
    """SHA-256 の指紋 (スマホの設定に出る値と見比べる)。"""
    h = hashlib.sha256(_unpem(pem)).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def der(pem: str) -> bytes:
    return _unpem(pem)
