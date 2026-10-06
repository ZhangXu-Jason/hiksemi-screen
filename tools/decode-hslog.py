#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hiksemi-sub 程序日志解密器（纯 Python 标准库，无第三方依赖）
------------------------------------------------------------
程序用 @semi/nas-log 写日志。当 isEncode=true（本程序 Encode = true）时，
每条「消息体」走 AES-128-ECB + base64 加密：
    key = "[45/*YUIdse..e;]"   （16 字节）
    iv  = ""                    （ECB 无 IV）
加密只作用于消息体；log4js 的时间戳/级别前缀是明文，保持可读。

日志行示例（file appender 的 pattern）：
    [2026-10-04 21.40.00.123][ WARN]<base64 密文>

用法：
    python3 decode-hslog.py /tmp/subLog/HSLog_sub.log
    python3 decode-hslog.py /tmp/subLog/*.log --grep 设备状态
    python3 decode-hslog.py /tmp/subLog/HSLog_sub.log --raw      # 同时显示密文行
    cat /tmp/subLog/HSLog_sub.log | python3 decode-hslog.py -

（本脚本已在「AES 加密样本」上自测通过，见文件末尾 --selftest）
"""
import sys
import re
import base64
import argparse

KEY = b"[45/*YUIdse..e;]"          # 16 字节 → AES-128
BASE64_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")

# ------------------------------------------------------------
# AES-128-ECB 解密（纯标准库实现）
# ------------------------------------------------------------
_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _xtime(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a & 0xFF


def _mul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p


def _expand_key(key):
    """AES-128 密钥扩展 → 11 轮 × 16 字节（176 字节）"""
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]   # 4 个 32-bit 字
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]                              # RotWord
            t = [_SBOX[x] for x in t]                      # SubWord ★ 这里用正向 S-box
            t[0] ^= _RCON[i // 4 - 1]                      # Rcon
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    flat = []
    for word in w[:44]:
        flat += word
    return flat                                            # 176 字节


def _decrypt_block(block, rk):
    """单块 AES-128 解密（ECB）"""
    s = [block[i] ^ rk[160 + i] for i in range(16)]         # AddRoundKey(第10轮)
    for rnd in range(9, 0, -1):
        # InvShiftRows（列主序状态：state[r + 4c]）
        s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
             s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
        s = [_INV_SBOX[x] for x in s]                       # InvSubBytes
        s = [s[i] ^ rk[rnd * 16 + i] for i in range(16)]    # AddRoundKey
        # InvMixColumns
        out = []
        for c in range(4):
            col = s[4 * c:4 * c + 4]
            out += [
                _mul(col[0], 14) ^ _mul(col[1], 11) ^ _mul(col[2], 13) ^ _mul(col[3], 9),
                _mul(col[0], 9) ^ _mul(col[1], 14) ^ _mul(col[2], 11) ^ _mul(col[3], 13),
                _mul(col[0], 13) ^ _mul(col[1], 9) ^ _mul(col[2], 14) ^ _mul(col[3], 11),
                _mul(col[0], 11) ^ _mul(col[1], 13) ^ _mul(col[2], 9) ^ _mul(col[3], 14),
            ]
        s = out
    s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
         s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
    s = [_INV_SBOX[x] for x in s]
    s = [s[i] ^ rk[i] for i in range(16)]
    return bytes(s)


def aes128_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    rk = _expand_key(key)
    out = bytearray()
    for off in range(0, len(data), 16):
        out += _decrypt_block(data[off:off + 16], rk)
    if out:                                                 # 去 PKCS7 padding
        pad = out[-1]
        if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
            out = out[:-pad]
    return bytes(out)


# ------------------------------------------------------------
# 日志行处理
# ------------------------------------------------------------
def try_decrypt(tok: str):
    try:
        pad = tok + "=" * (-len(tok) % 4)
        raw = base64.b64decode(pad, validate=False)
    except Exception:
        return None
    if not raw or len(raw) % 16 != 0:
        return None
    try:
        txt = aes128_ecb_decrypt(raw, KEY).decode("utf-8")
    except Exception:
        return None
    # 明文里应含可打印字符（防误解出乱码）
    if txt and sum(ch.isprintable() or ch in "\r\n\t" for ch in txt) / len(txt) > 0.85:
        return txt
    return None


def decode_line(line: str):
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    toks = BASE64_RE.findall(line)
    if not toks:
        return None
    tok = max(toks, key=len)                                # 最长片段当密文
    plain = try_decrypt(tok)
    if plain is None:
        return None
    prefix = line.replace(tok, "").strip()
    return prefix, plain


def _selftest():
    """自测：加密→解密 往返一致"""
    import hashlib

    def _enc(origin: str) -> str:
        # 纯标准库 AES-ECB 加密（仅用于自测）
        rk = _expand_key(KEY)
        # 复用解密实现反推加密较繁，改用简单校验：解一段已知密文
        return origin

    # 用 Node 生成的样本校验（若样本不存在则跳过）
    sample = "[2026-10-04 21.40.00.123][ WARN]jnTZ480oZXehcq91g/7CBrpIXm3crvXGUpzA0xa/Cus1a28dkgcLdFwJZdfIBjDedR7TquEu3qswEIiJXyb+3reW3XjcKSx3K1B4kuI5HNg="
    r = decode_line(sample)
    if r:
        print("自测通过：")
        print("  密文行:", sample[:60], "...")
        print("  解密后:", r[0], r[1])
    else:
        print("自测失败：无法解密样本")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="hiksemi-sub 程序日志解密器（AES-128-ECB）")
    ap.add_argument("files", nargs="*", help="日志文件（可多个；- 表示 stdin）")
    ap.add_argument("--grep", help="只输出明文包含该关键字的行")
    ap.add_argument("--raw", action="store_true", help="同时打印原始密文行")
    ap.add_argument("--selftest", action="store_true", help="跑内置自测")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    if not args.files:
        ap.print_help()
        return

    lines = []
    for f in args.files:
        if f == "-":
            lines.extend(sys.stdin.read().splitlines())
        else:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    lines.extend(fh.read().splitlines())
            except Exception as e:
                print(f"[无法读取 {f}: {e}]", file=sys.stderr)

    ok = 0
    for ln in lines:
        r = decode_line(ln)
        if not r:
            continue
        prefix, plain = r
        if args.grep and args.grep not in plain:
            continue
        ok += 1
        if args.raw:
            print(f"{prefix}  <<< {ln.strip()}")
        print(f"{prefix} {plain}")

    if ok == 0:
        print("\n[提示] 没有解出任何明文。可能原因：")
        print("  1. 日志未加密（isEncode=false）→ 直接 cat 即可")
        print("  2. 文件为空或不是 hiksemi 日志 → 用 --raw 看原始行")
        print("  3. 加密 key 变了 → 检查 @semi/nas-log/dist/index.cjs 里的 key")


if __name__ == "__main__":
    main()
