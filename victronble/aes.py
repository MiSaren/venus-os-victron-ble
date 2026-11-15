"""AES-128-CTR for Victron instant readout records.

The counter block is the 2-byte nonce from the advertisement (as sent, least
significant byte first) followed by 14 zero bytes. All current records fit in
one AES block. CTR mode is symmetric, so the same function also encrypts
(used by the tests).

OpenSSL's libcrypto is used through ctypes: it is always present on Venus OS
(dbus-ble-sensors and Python's ssl module link against it). The 'cryptography'
package is used as a fallback where it is installed.
"""

import ctypes
import ctypes.util

KEY_LEN = 16


def _load_libcrypto():
    names = ["libcrypto.so.3"]
    found = ctypes.util.find_library("crypto")
    if found:
        names.append(found)
    for name in names:
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_CIPHER_CTX_new.argtypes = []
        lib.EVP_CIPHER_CTX_free.restype = None
        lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
        lib.EVP_aes_128_ctr.restype = ctypes.c_void_p
        lib.EVP_aes_128_ctr.argtypes = []
        lib.EVP_DecryptInit_ex.restype = ctypes.c_int
        lib.EVP_DecryptInit_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        lib.EVP_DecryptUpdate.restype = ctypes.c_int
        lib.EVP_DecryptUpdate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        return lib
    return None


_lib = _load_libcrypto()


def _ctr_openssl(key, iv, data):
    ctx = _lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise MemoryError("EVP_CIPHER_CTX_new failed")
    try:
        if _lib.EVP_DecryptInit_ex(ctx, _lib.EVP_aes_128_ctr(), None, key, iv) != 1:
            raise ValueError("EVP_DecryptInit_ex failed")
        out = ctypes.create_string_buffer(len(data) + KEY_LEN)
        outlen = ctypes.c_int(0)
        if _lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(outlen), data, len(data)) != 1:
            raise ValueError("EVP_DecryptUpdate failed")
        return out.raw[: outlen.value]
    finally:
        _lib.EVP_CIPHER_CTX_free(ctx)


def _ctr_cryptography(key, iv, data):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


_ctr = _ctr_openssl if _lib is not None else _ctr_cryptography


def ctr(key, nonce, data):
    """Decrypt (or encrypt) a record payload with the 16-byte key and 2-byte nonce."""
    if len(key) != KEY_LEN:
        raise ValueError("key must be 16 bytes")
    if len(nonce) != 2:
        raise ValueError("nonce must be 2 bytes")
    if not data:
        return b""
    return _ctr(bytes(key), bytes(nonce) + bytes(14), bytes(data))


def backend():
    return "openssl" if _lib is not None else "cryptography"


def parse_key(text):
    """'0011..ff' (32 hex digits; spaces, ':' and '-' allowed) -> 16 bytes, or None."""
    if text is None:
        return None
    text = str(text).strip().replace(" ", "").replace(":", "").replace("-", "")
    if len(text) != 2 * KEY_LEN:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None
